#!/usr/bin/env python3
"""codex_session_start.py -- Codex SessionStart hook: load the memory index and RESUME.

Claude Code puts the global MEMORY.md and RESUME.md in context through CLAUDE.md
@imports. Codex has no @import (AGENTS.md is plain text, built once per run), so this
hook does the same job: on startup, resume and after compaction (Codex reports source
"compact"), it returns the Codex store's files as additionalContext.

What it loads, in order (each only if present and non-empty):
  <global store>/MEMORY.md, <global store>/RESUME.md,
  <project store>/MEMORY.md, <project store>/RESUME.md
then the live-context block (bin/live.py: what other sessions just did), which only uses
what is left of the budget after those files. The stores come from
notes_config.stores_for(cwd) in codex mode: a configured scope that holds cwd, else the cwd's git root. Nothing under ~/.claude is ever read; the live
file sits outside both agent homes (see bin/live.py).

Budget. Codex spills hook output over ~2,500 tokens (configurable per handler with
additionalContextLimit) to a temp file and shows the model only a head-and-tail
preview. A silent spill would hide the middle of the index, so the hook caps itself at
agent.codex_boot_bytes: a file that does not fit is replaced by one line naming its
path and size, telling the model to read it. install.sh sets additionalContextLimit
above that cap.

Silent when there is nothing to load (the start-empty case), and never fails the
session: any error exits 0 with no output and leaves a trace in <state_dir>/errors/.

Install: `install.sh --codex` registers it in hooks.json through the launcher it writes,
as `SESSION_NOTES_AGENT=codex <launcher> hook codex_session_start` (SessionStart, matcher
startup|resume|compact, additionalContextLimit 4000).
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
import live  # noqa: E402

HEADER = ("session-notes memory for this Codex session (loaded at session start, from "
          "the Codex store; it does not update mid-session: run $refresh to re-read it).")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def plan_context(cwd, cfg=None):
    """(text, entries) for cwd. text is the additionalContext (what build_context returns);
    entries is one dict per candidate file in the hook's own order: path, label, chars
    (len of the text, the unit the cap counts), bytes, status ("loaded" or "dropped"),
    reason. The live block, when it prints, is the last entry (status "loaded", or
    "truncated" when older entries were cut). boot_budget.py reads this to report what a
    Codex session loads; the packing lives only here."""
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
    parts = []
    entries = []
    if files:
        parts.append(HEADER)
        if stores["project"]:
            parts.append("Project store: %s (root %s)" % (stores["project"], stores["root"]))
    used = sum(len(p) + 2 for p in parts)
    for label, path, text in files:
        block = "## %s %s (%s)\n\n%s" % (label, os.path.basename(path), path, text)
        left = cap - used - 2
        entry = {"path": path, "label": label, "chars": len(text),
                 "bytes": len(text.encode("utf-8"))}
        if used + len(block) + 2 <= cap:
            parts.append(block)
            used += len(block) + 2
            entry.update(status="loaded", reason="")
        else:
            line = ("## %s %s (%s): %d bytes, over the boot budget, NOT loaded. "
                    "Read it before relying on memory." % (label, os.path.basename(path),
                                                            path, len(text.encode("utf-8"))))
            parts.append(line)
            used += len(line) + 2
            entry.update(status="dropped",
                         reason="needs %d chars, %d left of the %d cap" % (
                             len(block) + 2, max(left, 0), cap))
        entries.append(entry)
    try:
        block = live.render(cwd, cfg, budget=cap - used - 2)
    except Exception:
        block = ""
        _trace("live")
    if block:
        parts.append(block)
        trunc = "older entries not shown" in block
        entries.append({"path": live_path(cwd, cfg), "label": "live", "chars": len(block),
                        "bytes": len(block.encode("utf-8")),
                        "status": "truncated" if trunc else "loaded",
                        "reason": "older live entries cut to fit" if trunc else ""})
    return "\n\n".join(parts), entries


def live_path(cwd, cfg):
    try:
        return live.scope_for(cwd, cfg)[2]
    except Exception:
        return ""


def build_context(cwd, cfg=None):
    """The additionalContext text for cwd, or "" when the stores hold nothing."""
    return plan_context(cwd, cfg)[0]


def _trace_dir():
    try:
        return os.path.join(notes_config.load()["paths"]["state_dir"], "errors")
    except Exception:
        return os.path.expanduser("~/.codex/session-notes/state/errors")


def _trace(name="main"):
    try:
        d = _trace_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "codex_session_start.txt"), "a") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()))
            fh.write("[%s] " % name + traceback.format_exc() + "\n")
    except OSError:
        pass


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
        _trace()
    return 0


# build_context output captured from the pre-refactor hook on the fixture in selftest()
# (paths and time stamps normalized); plan_context must keep printing exactly this.
GOLDEN_BEFORE_REFACTOR = 'session-notes memory for this Codex session (loaded at session start, from the Codex store; it does not update mid-session: run $refresh to re-read it).\n\nProject store: <D>/codex/session-notes/projects/SLUG (root <D>/repo)\n\n## global MEMORY.md (<D>/codex/session-notes/global/MEMORY.md)\n\n# Memory Index\n\n- [Prefers tabs](user_tabs.md) - tabs\n\n## global RESUME.md (<D>/codex/session-notes/global/RESUME.md): 5000 bytes, over the boot budget, NOT loaded. Read it before relying on memory.\n\n## project RESUME.md (<D>/codex/session-notes/projects/SLUG/RESUME.md)\n\n# RESUME\n\n- parser fix shipped\n\nLive context (newest first, from <D>/live/l.md; loaded at session start, re-read with live.py show):\n- TS [repo] event number 29 with padding text to take room\n- TS [repo] event number 28 with padding text to take room\n- TS [repo] event number 27 with padding text to take room\n- TS [repo] event number 26 with padding text to take room\n- TS [repo] event number 25 with padding text to take room\n- TS [repo] event number 24 with padding text to take room\n- TS [repo] event number 23 with padding text to take room\n- TS [repo] event number 22 with padding text to take room\n- TS [repo] event number 21 with padding text to take room\n(21 older entries not shown)'


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
            fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                     '[live]\npath = "%s/live/live-context.md"\n'
                     '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = 2000\n'
                     '[[scopes]]\nname = "work-client"\nroot = "%s/client"\nlive = "none"\n'
                     '[[scopes]]\nname = "outer"\nroot = "%s/work"\n'
                     '[[scopes]]\nname = "inner-none"\nroot = "%s/work/client"\nlive = "none"\n'
                     % (d, d, d, d, d, d, d))
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

        # live context rides along inside the cap, after the memory files
        check("no live file: no live block", "Live context" not in text)
        live.add(repo, "merged PR #3: parser fix", cfg)
        text = build_context(repo, cfg)
        check("live line present when there is room", "merged PR #3: parser fix" in text
              and text.index("parser fix shipped") < text.index("Live context"))
        for i in range(30):
            live.add(repo, "event number %d with some padding text to take room" % i, cfg)
        text = build_context(repo, cfg)
        check("large live file: total stays under the cap", len(text) <= 2000)
        check("large live file: newest kept, older counted",
              "event number 29 " in text and "older entries not shown" in text)
        check("memory files keep priority over live", "parser fix shipped" in text)
        client = os.path.join(d, "client")
        os.makedirs(client)
        ctext = build_context(client, cfg)
        check("isolated scope: nothing from the shared file",
              "event number" not in ctext and "Live context" not in ctext)
        gitc = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(gitc + ["init", "-q", client], check=True)
        subprocess.run(gitc + ["-C", client, "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        cwt = os.path.join(d, ".client-worktrees", "feat")
        subprocess.run(gitc + ["-C", client, "worktree", "add", "-q", "-b", "feat", cwt], check=True)
        # repro A: a none-scope repo nested in a shared scope, worktree under the OUTER root
        inner = os.path.join(d, "work", "client")
        os.makedirs(inner)
        subprocess.run(gitc + ["init", "-q", inner], check=True)
        subprocess.run(gitc + ["-C", inner, "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        wa = os.path.join(d, "work", ".client-worktrees", "feat")
        subprocess.run(gitc + ["-C", inner, "worktree", "add", "-q", "-b", "feat", wa], check=True)
        atext = build_context(wa, cfg)
        check("worktree under an enclosing shared scope's root: nothing from the shared file",
              "event number" not in atext and "Live context" not in atext)
        wtext = build_context(cwt, cfg)
        check("isolated scope's worktree: nothing from the shared file",
              "event number" not in wtext and "Live context" not in wtext)
    with tempfile.TemporaryDirectory() as d:  # golden fixture: same files as the capture
        import re
        d = os.path.realpath(d)
        cf = os.path.join(d, "c.toml")
        with open(cf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                     '[live]\npath = "%s/live/l.md"\n'
                     '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = 2000\n' % (d, d, d, d))
        gcfg = notes_config.load(cf)
        grepo = os.path.join(d, "repo")
        os.makedirs(grepo)
        subprocess.run(["git", "init", "-q", grepo], check=True)
        gg = gcfg["paths"]["global_store"]
        gp = notes_config.codex_project_store(grepo, gcfg["agent"]["codex_home"])
        os.makedirs(gg)
        os.makedirs(gp)
        for path, body in ((os.path.join(gg, "MEMORY.md"),
                            "# Memory Index\n\n- [Prefers tabs](user_tabs.md) - tabs\n"),
                           (os.path.join(gg, "RESUME.md"), "x" * 5000),
                           (os.path.join(gp, "RESUME.md"), "# RESUME\n\n- parser fix shipped\n")):
            with open(path, "w") as fh:
                fh.write(body)
        for i in range(30):
            live.add(grepo, "event number %d with padding text to take room" % i, gcfg)
        gtext = build_context(grepo, gcfg)
        norm = re.sub(r"\d{4}-\d{2}-\d{2} [0-9:]{5} [A-Z]+", "TS",
                      gtext.replace(os.path.basename(gp), "SLUG").replace(d, "<D>"))
        check("output byte-identical to the pre-refactor hook on a fixture",
              norm == GOLDEN_BEFORE_REFACTOR)
        ptext, ents = plan_context(grepo, gcfg)
        check("plan_context entries: hook order, dropped file named, live last",
              [(os.path.basename(e["path"]), e["status"]) for e in ents][:3]
              == [("MEMORY.md", "loaded"), ("RESUME.md", "dropped"), ("RESUME.md", "loaded")]
              and ents[-1]["label"] == "live" and ents[-1]["status"] == "truncated"
              and ptext == gtext)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
