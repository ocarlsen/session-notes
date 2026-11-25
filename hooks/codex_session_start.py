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
import re
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
    return _read_checked(path)[0]


def _read_checked(path):
    """(text, problem): problem is None for a file that is absent or read fine, else why an
    existing file could not be read or decoded (the text is then "")."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip(), None
    except FileNotFoundError:
        return "", None
    except (OSError, UnicodeDecodeError) as exc:
        return "", "%s: cannot be read or decoded" % type(exc).__name__


def plan_context(cwd, cfg=None):
    """(text, entries) for cwd. text is the additionalContext (what build_context returns);
    entries is one dict per candidate file in the hook's own order: path, label, chars
    (len of the text, the unit the cap counts), bytes, status ("loaded" or "dropped"),
    reason. A store file that exists but cannot be read or decoded is "dropped". The live
    block is the last entry: "loaded", "truncated" when older entries were cut, or "dropped"
    when entries exist but none fit. boot_budget.py reads this to report what a
    Codex session loads; the packing lives only here."""
    cfg = cfg or notes_config.load()
    stores = notes_config.stores_for(cwd, cfg)
    cap = cfg["agent"]["codex_boot_bytes"]
    files = []  # (label, path, text, problem) for every store file that exists and is not blank
    for label, store in (("global", stores["global"]), ("project", stores["project"])):
        if not store:
            continue
        for name in ("MEMORY.md", "RESUME.md"):
            path = os.path.join(store, name)
            text, problem = _read_checked(path)
            if text or problem:
                files.append((label, path, text, problem))
    packable = [f for f in files if f[2]]
    parts = []
    entries = []
    if packable:
        parts.append(HEADER)
        if stores["project"]:
            parts.append("Project store: %s (root %s)" % (stores["project"], stores["root"]))
    used = sum(len(p) + 2 for p in parts)
    for label, path, text, problem in files:
        if problem:  # exists but unreadable: never loaded, so say so (the printed text is unchanged)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            entries.append({"path": path, "label": label, "chars": 0, "bytes": size,
                            "status": "dropped", "reason": problem})
            continue
        block = "## %s %s (%s)\n\n%s" % (label, os.path.basename(path), path, text)
        left = cap - used
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
        trunc = re.search(r"\n\(\d+ older entries not shown\)$", block) is not None
        entries.append({"path": live_path(cwd, cfg), "label": "live", "chars": len(block),
                        "bytes": len(block.encode("utf-8")),
                        "status": "truncated" if trunc else "loaded",
                        "reason": "older live entries cut to fit" if trunc else ""})
    else:
        try:
            has_live = bool(live.read_entries(cwd, cfg))
        except Exception:
            has_live = False
        if has_live:
            entries.append({"path": live_path(cwd, cfg), "label": "live", "chars": 0, "bytes": 0,
                            "status": "dropped",
                            "reason": "no room left under the %d cap for even one entry" % cap})
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


def _legacy_build_context(cwd, cfg):
    """A frozen copy of build_context as it was before plan_context existed (git 588394a). The
    selftest runs it against build_context on generated inputs: the refactor must print the
    same bytes. Do not edit it to follow the hook; a deliberate output change updates it."""
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
    if files:
        parts.append(HEADER)
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
    try:
        block = live.render(cwd, cfg, budget=cap - used - 2)
    except Exception:
        block = ""
    if block:
        parts.append(block)
    return "\n\n".join(parts)


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
    # Differential: the pre-refactor packing vs plan_context on generated inputs (different caps,
    # path lengths, file sizes, live entries). Nothing here depends on the temp dir or the TZ.
    import random
    rnd = random.Random(104)
    diffs = 0
    cases = 0
    for depth in (0, 3, 9):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            base = os.path.join(d, *["nest%d" % i for i in range(depth)])
            os.makedirs(base, exist_ok=True)
            cf = os.path.join(d, "c.toml")
            for cap in (1, 120, 400, 900, 2000, 12000):
                with open(cf, "w") as fh:
                    fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                             '[live]\npath = "%s/live/l-%d.md"\n'
                             '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = %d\n'
                             % (d, d, d, cap, d, cap))
                gcfg = notes_config.load(cf)
                grepo = os.path.join(base, "repo%d" % cap)
                os.makedirs(grepo, exist_ok=True)
                subprocess.run(["git", "init", "-q", grepo], check=True)
                gg = gcfg["paths"]["global_store"]
                gp = notes_config.codex_project_store(grepo, gcfg["agent"]["codex_home"])
                os.makedirs(gg, exist_ok=True)
                os.makedirs(gp, exist_ok=True)
                for round_ in range(6):
                    for st in (gg, gp):
                        for name in ("MEMORY.md", "RESUME.md"):
                            kind = rnd.choice(("none", "blank", "small", "big", "multibyte", "bad"))
                            f = os.path.join(st, name)
                            if kind == "none":
                                if os.path.exists(f):
                                    os.remove(f)
                                continue
                            body = {"blank": b"  \n", "small": b"# t\n- a line\n",
                                    "big": b"x" * rnd.randint(50, 3000),
                                    "multibyte": "caf\u00e9 \u4e2d\u6587 ".encode() * rnd.randint(1, 120),
                                    "bad": b"\xff\xfe bad"}[kind]
                            with open(f, "wb") as fh:
                                fh.write(body)
                    for i in range(rnd.randint(0, 12)):
                        live.add(grepo, "event %d %s" % (i, "pad" * rnd.randint(1, 30)), gcfg)
                    cases += 1
                    if build_context(grepo, gcfg) != _legacy_build_context(grepo, gcfg):
                        diffs += 1
    check("plan_context prints exactly what the pre-refactor packing printed (%d generated cases)"
          % cases, diffs == 0 and cases == 108)
    with tempfile.TemporaryDirectory() as d:
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
        for path, body in ((os.path.join(gg, "MEMORY.md"), "# Memory Index\n\n- [x](x.md) - x\n"),
                           (os.path.join(gg, "RESUME.md"), "x" * 5000),
                           (os.path.join(gp, "RESUME.md"), "# RESUME\n\n- parser fix shipped\n")):
            with open(path, "w") as fh:
                fh.write(body)
        for i in range(30):
            live.add(grepo, "event number %d with padding text to take room" % i, gcfg)
        ptext, ents = plan_context(grepo, gcfg)
        check("plan_context entries: hook order, dropped file named, live last, text matches",
              [(os.path.basename(e["path"]), e["status"]) for e in ents][:3]
              == [("MEMORY.md", "loaded"), ("RESUME.md", "dropped"), ("RESUME.md", "loaded")]
              and ents[-1]["label"] == "live" and ents[-1]["status"] == "truncated"
              and ptext == build_context(grepo, gcfg))
        drop = ents[1]
        need = int(re.search(r"needs (\d+) chars", drop["reason"]).group(1))
        left = int(re.search(r"(\d+) left", drop["reason"]).group(1))
        first = "## global MEMORY.md (%s)\n\n%s" % (ents[0]["path"], "# Memory Index\n\n- [x](x.md) - x")
        used_before = (len(HEADER) + 2 + len("Project store: %s (root %s)" % (gp, grepo)) + 2
                       + len(first) + 2)
        check("dropped reason: 'left' is cap minus what is already packed (no off-by-2)",
              left == 2000 - used_before and need > left)
        # N2: a store file that exists but cannot be decoded is reported, and the text is unchanged
        with open(os.path.join(gg, "RESUME.md"), "wb") as fh:
            fh.write(b"\xff\xfe bad")
        btext, bents = plan_context(grepo, gcfg)
        check("undecodable store file: reported dropped with a reason, text unchanged",
              any(e["path"].endswith("global/RESUME.md") and e["status"] == "dropped"
                  and "cannot be read" in e["reason"] for e in bents)
              and btext == _legacy_build_context(grepo, gcfg))
        # N3: a live entry quoting the marker is not a truncation
        os.remove(os.path.join(gg, "RESUME.md"))
        big = dict(gcfg)
        big["agent"] = dict(gcfg["agent"], codex_boot_bytes=12000)
        live.add(grepo, "talks about (3 older entries not shown) in its text", big)
        _t, lents = plan_context(grepo, big)
        check("live entry containing the marker text is not read as truncation",
              lents[-1]["label"] == "live" and lents[-1]["status"] == "loaded")
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
