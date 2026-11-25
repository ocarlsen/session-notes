#!/usr/bin/env python3
"""install.sh --codex on a machine with no Claude Code: it links the commands the Codex rules
name (wt, handoff_session, session_peers, bcast, session_bus, bus_nudge, ...) into --bin-dir,
and writes nothing under ~/.claude.

Everything runs under a temp HOME with a temp bin dir and a stubbed launchctl first on PATH, from
a copy of this checkout (install.sh --codex refuses a linked git worktree, and a copy has no
.git). Nothing here touches the real home, the real launchctl or a real tmux server."""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# What the Codex rules and AGENTS.md tell the agent to run, plus the rest of the Claude --all set.
COMMANDS = ("handoff_session", "session_peers", "bcast", "wt", "session_bus", "bus_nudge",
            "reflect_lock", "append_memory_index", "memory_index_lint", "boot_budget",
            "memory_hygiene_canary", "nightly_check", "launch_agents", "notes_config")
FAILS = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, "" if ok else "  " + detail))
    if not ok:
        FAILS.append(name)


def tree_files(d):
    out = []
    for dp, dn, fn in os.walk(d):
        out += [os.path.join(dp, x) for x in dn + fn]
    return sorted(out)


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="sn-install-codex-"))
    try:
        repo = os.path.join(tmp, "repo")
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        home = os.path.join(tmp, "home")
        stubs = os.path.join(tmp, "stubs")
        binx = os.path.join(tmp, "bin")
        for d in (home, stubs):
            os.makedirs(d)
        calls = os.path.join(tmp, "launchctl-calls.txt")
        with open(os.path.join(stubs, "launchctl"), "w") as fh:
            fh.write('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % calls)
        os.chmod(os.path.join(stubs, "launchctl"), 0o755)
        py = os.path.dirname(shutil.which("python3") or "/usr/bin/python3")
        path = os.pathsep.join([stubs, py, "/usr/bin", "/bin"])
        env = {"HOME": home, "PATH": path, "SESSION_NOTES_CONFIG": os.path.join(tmp, "config.toml"),
               "TERM": "dumb", "LANG": "en_US.UTF-8"}
        open(env["SESSION_NOTES_CONFIG"], "w").close()
        if shutil.which("launchctl", path=path) != os.path.join(stubs, "launchctl"):
            print("FAIL the launchctl stub is not first on PATH; refusing to run install.sh")
            return 1

        def install(*args, extra_path=None):
            e = dict(env)
            if extra_path:
                e["PATH"] = os.pathsep.join([extra_path, path])
            r = subprocess.run(["sh", "install.sh", "--codex"] + list(args), cwd=repo, env=e,
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            return r.returncode, r.stdout, r.stderr

        # 1. dry run: lists a link for every command into the given bin dir, creates nothing
        rc, out, err = install("--dry-run", "--bin-dir", binx)
        check("dry run: exits 0", rc == 0, err[-300:])
        missing = [c for c in COMMANDS
                   if "would: ln -s %s/bin/%s.py %s/%s" % (repo, c, binx, c) not in out]
        check("dry run: lists a link into --bin-dir for every command", not missing, "missing: %s" % missing)
        check("dry run: creates no bin dir and no ~/.claude",
              not os.path.exists(binx) and not os.path.exists(os.path.join(home, ".claude")))

        # 2. real install (--yes): links every command, nothing under ~/.claude
        rc, out, err = install("--yes", "--bin-dir", binx)
        check("--yes install: exits 0", rc == 0, (out + err)[-400:])
        bad = [c for c in COMMANDS
               if not (os.path.islink(os.path.join(binx, c))
                       and os.readlink(os.path.join(binx, c)) == os.path.join(repo, "bin", c + ".py"))]
        check("--yes install: every command is a symlink to its bin/*.py", not bad, "bad: %s" % bad)
        check("--yes install: no ~/.claude of any kind",
              not os.path.exists(os.path.join(home, ".claude")),
              "found: %s" % [f for f in tree_files(home) if "/.claude" in f][:5])
        check("--yes install: Codex side was set up (skills, hooks.json, launcher)",
              os.path.islink(os.path.join(home, ".codex", "skills", "reflect"))
              and os.path.isfile(os.path.join(home, ".codex", "hooks.json"))
              and os.path.isfile(os.path.join(home, ".local", "bin", "session-notes")))
        check("--yes install: PATH note printed when --bin-dir is not on PATH",
              "is not on your PATH" in out, out[-300:])
        if os.path.exists(os.path.join(binx, "wt")):
            wt = subprocess.run([os.path.join(binx, "wt"), "--help"], env=env, capture_output=True,
                                text=True, timeout=60, stdin=subprocess.DEVNULL)
            check("the linked wt command runs", wt.returncode == 0, (wt.stdout + wt.stderr)[-200:])
        else:
            check("the linked wt command runs", False, "wt was not linked")

        # 3. rerun is idempotent, and no PATH note when --bin-dir is on PATH
        rc, out, err = install("--yes", "--bin-dir", binx, extra_path=binx)
        check("rerun with --bin-dir on PATH: exits 0, no PATH note",
              rc == 0 and "is not on your PATH" not in out, (out + err)[-300:])

        # 4. no --bin-dir: nothing linked, and it says so
        rc, out, err = install("--dry-run")
        check("no --bin-dir: says the commands are not linked and names the flag",
              rc == 0 and "Commands not linked: pass --bin-dir" in out and "would: ln -s %s/bin/wt.py" % repo not in out,
              out[:300])
        return 1 if FAILS else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
