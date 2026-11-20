#!/usr/bin/env python3
"""codex_session_start.py -- Codex SessionStart hook: load the memory index and RESUME.

Claude Code puts the global MEMORY.md and RESUME.md in context through CLAUDE.md
@imports. Codex has no @import (AGENTS.md is plain text, built once per run), so this
hook does the same job: on startup, resume and after compaction (Codex reports source
"compact"), it returns the Codex store's files as additionalContext.

What it loads, in order (each only if present and non-empty):
  <global store>/MEMORY.md, <global store>/RESUME.md,
  <project store>/MEMORY.md, <project store>/RESUME.md
where the stores come from notes_config.stores_for(cwd) in codex mode: a configured
scope that holds cwd, else the cwd's git root. Nothing under ~/.claude is ever read.

Budget. Codex spills hook output over ~2,500 tokens (configurable per handler with
additionalContextLimit) to a temp file and shows the model only a head-and-tail
preview. A silent spill would hide the middle of the index, so the hook caps itself at
agent.codex_boot_bytes: a file that does not fit is replaced by one line naming its
path and size, telling the model to read it. install.sh sets additionalContextLimit
above that cap.

Silent when there is nothing to load (the start-empty case), and never fails the
session: any error exits 0 with no output and leaves a trace in <state_dir>/errors/.

Install (hooks.json; install.sh --codex prints it):
  "SessionStart": [{"matcher": "startup|resume|compact", "hooks": [{"type": "command",
    "command": "SESSION_NOTES_AGENT=codex python3 <repo>/hooks/codex_session_start.py",
    "additionalContextLimit": 4000}]}]
"""
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))

# This hook is Codex-only, whatever the caller's environment says.
os.environ["SESSION_NOTES_AGENT"] = "codex"
import notes_config  # noqa: E402

HEADER = ("session-notes memory for this Codex session (loaded at session start, from "
          "the Codex store; it does not update mid-session: run $refresh to re-read it).")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def build_context(cwd, cfg=None):
    """The additionalContext text for cwd, or "" when the stores hold nothing."""
    cfg = cfg or notes_config.load()
    stores = notes_config.stores_for(cwd, cfg)
    cap = cfg["agent"]["codex_boot_bytes"]
    files = []
    for label, store in (("global", stores["global"]), ("project", stores["project"])):
        if not store:
            continue
        for name in ("MEMORY.md", "RESUME.md"):
            path = os.path.join(store, name)
            text = _read(path)
            if text:
                files.append((label, path, text))
    if not files:
        return ""
    parts = [HEADER]
    if stores["project"]:
        parts.append("Project store: %s (root %s)" % (stores["project"], stores["root"]))
    used = sum(len(p) + 2 for p in parts)
    for label, path, text in files:
        block = "## %s %s (%s)\n\n%s" % (label, os.path.basename(path), path, text)
        if used + len(block) + 2 <= cap:
            parts.append(block)
            used += len(block) + 2
        else:
            line = ("## %s %s (%s): %d bytes, over the boot budget, NOT loaded. "
                    "Read it before relying on memory." % (label, os.path.basename(path),
                                                            path, len(text.encode("utf-8"))))
            parts.append(line)
            used += len(line) + 2
    return "\n\n".join(parts)


def _trace_dir():
    try:
        return os.path.join(notes_config.load()["paths"]["state_dir"], "errors")
    except Exception:
        return os.path.expanduser("~/.codex/session-notes/state/errors")


def main():
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}  # not Codex's payload shape: load for the process cwd
        if not isinstance(payload, dict):
            payload = {}
        cwd = payload.get("cwd") or os.getcwd()
        text = build_context(cwd)
        if text:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception:
        try:
            d = _trace_dir()
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "codex_session_start.txt"), "a") as fh:
                fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()))
                fh.write(traceback.format_exc() + "\n")
        except OSError:
            pass
    return 0


def selftest():
    import subprocess
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        cfgf = os.path.join(d, "c.toml")
        with open(cfgf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\n[agent]\ncodex_home = "%s/codex"\n'
                     'codex_boot_bytes = 2000\n' % (d, d))
        cfg = notes_config.load(cfgf)
        repo = os.path.join(d, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        check("empty store: no context", build_context(repo, cfg) == "")

        g = cfg["paths"]["global_store"]
        p = notes_config.codex_project_store(repo, cfg["agent"]["codex_home"])
        os.makedirs(g)
        os.makedirs(p)
        with open(os.path.join(g, "MEMORY.md"), "w") as fh:
            fh.write("# Memory Index\n\n- [Prefers tabs](user_tabs.md) - tabs over spaces\n")
        with open(os.path.join(p, "RESUME.md"), "w") as fh:
            fh.write("# RESUME\n\n- parser fix shipped, tests green\n")
        text = build_context(repo, cfg)
        check("global index loaded", "Prefers tabs" in text)
        check("project RESUME loaded", "parser fix shipped" in text)
        check("names the project store", p in text)
        check("never a Claude path", os.path.join(d, "claude") not in text)

        with open(os.path.join(g, "RESUME.md"), "w") as fh:
            fh.write("x" * 5000)
        text = build_context(repo, cfg)
        check("over-budget file becomes a pointer", "over the boot budget, NOT loaded" in text
              and "xxxxx" not in text)
        check("stays under the cap", len(text) <= 2000 + 400)
        check("later files still load after a pointer", "parser fix shipped" in text)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
