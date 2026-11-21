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
    python3 launch_agents.py install nightly-check   # run it every night (macOS)
    python3 nightly_check.py --selftest

Exit codes: 0 clean, 3 findings (the report is on stdout), 1 a check crashed.
"""
import argparse
import os
import subprocess
import sys
import tempfile

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import notes_config  # noqa: E402

EXIT_FINDINGS = 3


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


    print("all ok" if not fails else "%d FAILED" % len(fails))
    return 0 if not fails else 1


class _Buf(object):
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    return run_all()


if __name__ == "__main__":
    sys.exit(main())
