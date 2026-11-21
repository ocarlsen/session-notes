#!/usr/bin/env python3
"""nightly_check -- the memory checks for every agent installed on this machine.

One run covers both agents when both are installed. For each one it runs
memory_hygiene_canary.py with SESSION_NOTES_AGENT set to that agent, so each
check sees only that agent's own stores (the two memories never mix). For
Claude it also runs `boot_budget.py check`, which measures Claude Code's boot
prefix. Codex has no boot-budget check yet; its AGENTS.md has its own cap.

An agent counts as installed when its home directory exists: paths.claude_home
for Claude, agent.codex_home for Codex.

    python3 nightly_check.py               # silent + exit 0 when every check is clean
    python3 nightly_check.py schedule      # offer to run it nightly (launchd on macOS)
    python3 nightly_check.py schedule --dry-run
    python3 nightly_check.py --selftest

Exit codes: 0 clean, 3 findings (the report is on stdout), 1 a check crashed.
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import notes_config  # noqa: E402

EXIT_FINDINGS = 3
LABEL = "com.session-notes.nightly-check"
HOUR = 4  # local time; quiet hours, after a day's /reflects


def installed_kinds(cfg_for):
    """The agent kinds whose home exists, in a fixed order. cfg_for(kind) -> config."""
    kinds = []
    if os.path.isdir(cfg_for("claude")["paths"]["claude_home"]):
        kinds.append("claude")
    if os.path.isdir(cfg_for("codex")["agent"]["codex_home"]):
        kinds.append("codex")
    return kinds


def checks_for(kind):
    """[(name, argv)] to run for one agent kind."""
    py = sys.executable or "python3"
    out = [("memory_hygiene_canary", [py, os.path.join(BIN, "memory_hygiene_canary.py")])]
    if kind == "claude":
        out.append(("boot_budget", [py, os.path.join(BIN, "boot_budget.py"), "check"]))
    return out


def run_all(env=None, run=None, out=None):
    """Run every check for every installed agent. Returns the exit code."""
    env = dict(os.environ if env is None else env)
    run = run or subprocess.run
    out = out or sys.stdout

    def cfg_for(kind):
        return notes_config.load(env=dict(env, SESSION_NOTES_AGENT=kind))

    try:
        kinds = installed_kinds(cfg_for)
    except notes_config.ConfigError as exc:
        out.write("nightly_check: bad config: %s\n" % exc)
        return 1
    if not kinds:
        out.write("nightly_check: no agent home found (neither Claude nor Codex)\n")
        return 1
    worst = 0
    for kind in kinds:
        for name, argv in checks_for(kind):
            r = run(argv, env=dict(env, SESSION_NOTES_AGENT=kind),
                    capture_output=True, text=True, timeout=600)
            text = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0:
                continue
            # boot_budget check exits 1 on a finding (each a FILE/SCOPE line); the canary
            # exits 3. Anything else, a traceback, or a boot_budget 1 with no finding line
            # (a bad config, say) is a crash and outranks findings.
            if name == "boot_budget":
                crashed = r.returncode != 1 or not any(
                    ln.startswith(("FILE ", "SCOPE ")) for ln in text.splitlines())
            else:
                crashed = r.returncode != EXIT_FINDINGS
            crashed = crashed or "Traceback" in text
            out.write("== %s: %s (%s) ==\n%s\n" % (
                kind, name, "CRASHED rc=%d" % r.returncode if crashed else "findings",
                text.rstrip()))
            if crashed:
                worst = 1
            elif worst != 1:
                worst = EXIT_FINDINGS
    return worst


# ---------------------------------------------------------------- schedule
def plist_text(python, script, log_dir, hour=HOUR):
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, script],
        "StartCalendarInterval": {"Hour": hour, "Minute": 0},
        "StandardOutPath": os.path.join(log_dir, "nightly-check.out"),
        "StandardErrorPath": os.path.join(log_dir, "nightly-check.err"),
    }).decode("utf-8")


def cron_line(python, script, log_dir, hour=HOUR):
    return "0 %d * * * %s %s >> %s/nightly-check.out 2>&1" % (hour, python, script, log_dir)


def schedule(dry=False, yes=False, platform=None, home=None, ask=None, out=None):
    """Offer to schedule run_all nightly. macOS: write a LaunchAgent (after a yes).
    Elsewhere: print the crontab line to add by hand."""
    platform = platform or sys.platform
    home = home or os.path.expanduser("~")
    out = out or sys.stdout
    python = shutil.which("python3") or sys.executable
    script = os.path.realpath(__file__)
    if platform != "darwin":
        log_dir = os.path.join(home, ".local", "state", "session-notes")
        out.write("Add this line with `crontab -e` to run the memory checks nightly:\n  %s\n"
                  % cron_line(python, script, log_dir))
        return 0
    log_dir = os.path.join(home, "Library", "Logs", "session-notes")
    path = os.path.join(home, "Library", "LaunchAgents", LABEL + ".plist")
    text = plist_text(python, script, log_dir)
    out.write("Nightly memory checks: %s at %02d:00, logs in %s\n" % (path, HOUR, log_dir))
    if dry:
        out.write(text)
        return 0
    if os.path.exists(path):
        out.write("already scheduled (%s exists); leaving it\n" % path)
        return 0
    if not yes:
        ask = ask or _ask
        if not ask("Schedule it?"):
            out.write("not scheduled\n")
            return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    r = subprocess.run(["launchctl", "load", path], capture_output=True, text=True)
    if r.returncode != 0:
        out.write("wrote %s, but launchctl load failed: %s\n" % (path, (r.stderr or "").strip()))
        return 1
    out.write("scheduled: %s\n" % path)
    return 0


def _ask(q):
    if not sys.stdin.isatty():
        return False
    try:
        return input(q + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------- selftest
def selftest():
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    class R(object):
        def __init__(self, rc, so=""):
            self.returncode, self.stdout, self.stderr = rc, so, ""

    with tempfile.TemporaryDirectory() as d:
        claude_home = os.path.join(d, "claude")
        codex_home = os.path.join(d, "codex")
        conf = os.path.join(d, "config.toml")
        with open(conf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s"\n[agent]\ncodex_home = "%s"\n'
                     % (claude_home, codex_home))
        env = {"SESSION_NOTES_CONFIG": conf, "HOME": d}

        def fake(results):
            calls = []

            def run(argv, env=None, **kw):
                name = os.path.basename(argv[1])
                calls.append((env["SESSION_NOTES_AGENT"], name))
                return results.get((env["SESSION_NOTES_AGENT"], name), R(0))
            return run, calls

        run, calls = fake({})
        buf = _Buf()
        check("no agent home: exit 1", run_all(env, run, buf) == 1 and not calls)

        os.makedirs(claude_home)
        run, calls = fake({})
        buf = _Buf()
        rc = run_all(env, run, buf)
        check("claude only: canary + boot_budget under SESSION_NOTES_AGENT=claude, silent, 0",
              rc == 0 and buf.text == "" and calls == [
                  ("claude", "memory_hygiene_canary.py"), ("claude", "boot_budget.py")])

        os.makedirs(codex_home)
        run, calls = fake({("codex", "memory_hygiene_canary.py"): R(3, "## Store: global\n- x")})
        buf = _Buf()
        rc = run_all(env, run, buf)
        check("both agents: each agent's canary runs with its own kind; no Codex boot_budget",
              calls == [("claude", "memory_hygiene_canary.py"), ("claude", "boot_budget.py"),
                        ("codex", "memory_hygiene_canary.py")])
        check("a finding exits 3 and names the agent", rc == 3 and "== codex: memory_hygiene_canary"
              " (findings)" in buf.text)

        run, _ = fake({("claude", "boot_budget.py"): R(1, "FILE x: over budget"),
                       ("codex", "memory_hygiene_canary.py"): R(3, "x")})
        check("boot_budget's exit 1 is a finding, not a crash", run_all(env, run, _Buf()) == 3)
        run, _ = fake({("claude", "boot_budget.py"): R(1, "boot_budget: bad config: x")})
        check("boot_budget's exit 1 with no FILE/SCOPE line is a crash", run_all(env, run, _Buf()) == 1)

        run, _ = fake({("claude", "memory_hygiene_canary.py"): R(1, "Traceback (most recent")})
        buf = _Buf()
        check("a crash outranks findings: exit 1, labelled CRASHED",
              run_all(env, run, buf) == 1 and "CRASHED" in buf.text)

        run, _ = fake({("claude", "memory_hygiene_canary.py"): R(1, "Traceback"),
                       ("codex", "memory_hygiene_canary.py"): R(3, "x")})
        check("a later finding does not mask an earlier crash", run_all(env, run, _Buf()) == 1)

        buf = _Buf()
        schedule(platform="linux", home=d, out=buf)
        check("non-macOS: prints a crontab line, writes nothing",
              "crontab -e" in buf.text and "0 %d * * *" % HOUR in buf.text
              and not os.path.exists(os.path.join(d, "Library")))
        buf = _Buf()
        schedule(dry=True, platform="darwin", home=d, out=buf)
        check("macOS --dry-run: shows the plist, writes nothing",
              plistlib.loads(buf.text[buf.text.index("<?xml"):].encode())["Label"] == LABEL
              and not os.path.exists(os.path.join(d, "Library", "LaunchAgents")))
        buf = _Buf()
        schedule(platform="darwin", home=d, ask=lambda q: False, out=buf)
        check("macOS without a yes: not scheduled, nothing written",
              "not scheduled" in buf.text
              and not os.path.exists(os.path.join(d, "Library", "LaunchAgents")))

    print("all ok" if not fails else "%d FAILED" % len(fails))
    return 0 if not fails else 1


class _Buf(object):
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", nargs="?", choices=["schedule"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.cmd == "schedule":
        return schedule(dry=a.dry_run, yes=a.yes)
    return run_all()


if __name__ == "__main__":
    sys.exit(main())
