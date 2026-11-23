#!/usr/bin/env python3
"""nightly_check -- the memory checks for every agent installed on this machine.

One run covers both agents when both are installed. For each one it runs
memory_hygiene_canary.py with SESSION_NOTES_AGENT set to that agent, so each
check sees only that agent's own stores (the two memories never mix). For
Claude it also runs `boot_budget.py check`, which measures Claude Code's boot
prefix. Codex has no boot-budget check yet; its AGENTS.md has its own cap.

An agent counts as installed when its home directory exists: paths.claude_home
for Claude, agent.codex_home for Codex.

    python3 nightly_check.py               # silent + exit 0 when every check is clean (a could-not-check note may print)
    python3 nightly_check.py status        # one line (or nothing) from the last run's record
    python3 launch_agents.py install nightly-check   # run it every night (macOS)
    python3 nightly_check.py --selftest

Every run records itself in paths.last_check (default <state_dir>/last-check.json):
when it ran, the exit code, each check's outcome and the report. `status` reads that
file and prints at most one line, so a brief or prompt can show it: nothing when the
last run is clean and fresh; a findings summary, or a loud line when the last run
crashed, never ran, left an unreadable record, or is older than --max-age-days.

Exit codes: 0 clean, 3 findings (the report is on stdout), 1 a check crashed or the
record could not be written. `status` always exits 0.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import notes_config  # noqa: E402

EXIT_FINDINGS = 3
STATUS_VERSION = 1
REPORT_MAX_CHARS = 20000
DEFAULT_MAX_AGE_DAYS = 8


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


def count_findings(name, text):
    """How many finding lines one check printed: the canary's "- " bullets, boot_budget's
    FILE / SCOPE / BLIND lines."""
    lines = text.splitlines()
    if name == "boot_budget":
        return sum(1 for ln in lines if ln.startswith(("FILE ", "SCOPE ", "BLIND ")))
    return sum(1 for ln in lines if ln.startswith("- "))


def last_check_path(env):
    """paths.last_check from the config, or None when no config can be loaded."""
    try:
        return notes_config.load(env=dict(env))["paths"]["last_check"]
    except notes_config.ConfigError:
        return None


def write_status(path, rc, checks, report, now):
    """Write the record atomically (temp file beside it, then os.replace)."""
    doc = {"version": STATUS_VERSION, "timestamp": now.isoformat(), "rc": rc,
           "checks": checks, "report": report[:REPORT_MAX_CHARS]}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".last-check.", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_all(env=None, run=None, out=None, now=None):
    """Run every check for every installed agent, record the run, return the exit code."""
    env = dict(os.environ if env is None else env)
    out = out or sys.stdout
    now = now or (lambda: datetime.now(timezone.utc))
    log = _Tee(out)
    checks = []
    rc = _run_checks(env, run or subprocess.run, log, checks)
    path = last_check_path(env)
    if path:
        try:
            write_status(path, rc, checks, log.text, now())
        except (OSError, ValueError) as exc:
            out.write("nightly_check: cannot write the status file %s (%s: %s)\n"
                      % (path, type(exc).__name__, getattr(exc, "strerror", None) or exc))
            rc = 1
    return rc


class _Tee(object):
    """Writes through to `out` and keeps a copy for the status record."""

    def __init__(self, out):
        self.out, self.text = out, ""

    def write(self, s):
        self.text += s
        self.out.write(s)


def _run_checks(env, run, out, checks):
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
            try:
                r = run(argv, env=dict(env, SESSION_NOTES_AGENT=kind),
                        capture_output=True, text=True, timeout=600)
            except (subprocess.TimeoutExpired, OSError) as exc:
                # A hung or unlaunchable check is a crash of THAT check; the others
                # still run and the record is still written.
                checks.append({"agent": kind, "check": name, "status": "crashed", "findings": 0})
                out.write("== %s: %s (CRASHED %s: %s) ==\n" % (kind, name, type(exc).__name__, exc))
                worst = 1
                continue
            text = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0:
                checks.append({"agent": kind, "check": name, "status": "clean", "findings": 0})
                continue
            # boot_budget check exits 1 on a finding (each a FILE/SCOPE/BLIND line); the
            # canary exits 3. Anything else, a traceback, or a boot_budget 1 with no finding
            # line (a bad config, say) is a crash and outranks findings.
            if name == "boot_budget":
                crashed = r.returncode != 1 or not any(
                    ln.startswith(("FILE ", "SCOPE ", "BLIND ")) for ln in text.splitlines())
            else:
                crashed = r.returncode != EXIT_FINDINGS
            crashed = crashed or "Traceback" in text
            checks.append({"agent": kind, "check": name,
                           "status": "crashed" if crashed else "findings",
                           "findings": 0 if crashed else count_findings(name, text)})
            out.write("== %s: %s (%s) ==\n%s\n" % (
                kind, name, "CRASHED rc=%d" % r.returncode if crashed else "findings",
                text.rstrip()))
            if crashed:
                worst = 1
            elif worst != 1:
                worst = EXIT_FINDINGS
    return max_rc(worst, _environment_check(env, out, checks))


def max_rc(a, b):
    """The worse of two exit codes: a crash (1) outranks findings (3) outranks clean (0)."""
    if 1 in (a, b):
        return 1
    return max(a, b)


def _environment_check(env, out, checks):
    """environment_render.py's drift check, run in-process. It covers both agents' targets
    at once, so it runs once, not per agent, and only when an environment source file exists
    (nothing is recorded or printed when the feature is not set up). Returns 0, 1 or 3."""
    try:
        import environment_render
        ran, rc, text = environment_render.check(dict(env))
    except Exception as exc:  # a crash of THIS check; the others already ran
        checks.append({"agent": "all", "check": "environment_render", "status": "crashed",
                       "findings": 0})
        out.write("== all: environment_render (CRASHED %s: %s) ==\n" % (type(exc).__name__, exc))
        return 1
    if not ran:
        return 0
    if rc == 0:
        checks.append({"agent": "all", "check": "environment_render", "status": "clean",
                       "findings": 0})
        if text.strip():   # a could-not-check note: in the report, never a finding
            out.write("== all: environment_render (note) ==\n%s\n" % text.rstrip())
        return 0
    findings = sum(1 for ln in text.splitlines() if ln.startswith(("claude: ", "codex: ", "upstream ")))
    checks.append({"agent": "all", "check": "environment_render", "status": "findings",
                   "findings": findings})
    out.write("== all: environment_render (findings) ==\n%s\n" % text.rstrip())
    return EXIT_FINDINGS


def status_line(path, max_age_days=DEFAULT_MAX_AGE_DAYS, now=None):
    """The one line `status` prints, or "" when the last run is clean and fresh.
    Fails closed: anything it cannot positively read as clean is a loud line."""
    now = now or datetime.now(timezone.utc)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return "MEMORY CHECKS NEVER RAN: no record at %s" % path
    except (OSError, ValueError) as exc:
        return "MEMORY CHECKS STATUS UNREADABLE: %s (%s)" % (path, type(exc).__name__)
    try:
        ran = datetime.fromisoformat(doc["timestamp"])
        if ran.tzinfo is None:
            raise ValueError("no UTC offset")
        if ran < datetime(1970, 1, 1, tzinfo=timezone.utc):
            # Explicit, not left to astimezone(): that overflows only in zones west of UTC.
            raise ValueError("timestamp before the epoch")
        rc, checks = doc["rc"], doc["checks"]
        rows = [(c["agent"], c["check"], c["status"], int(c["findings"])) for c in checks]
        age = (now - ran).total_seconds() / 86400.0
        day = ran.astimezone().strftime("%Y-%m-%d")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "MEMORY CHECKS STATUS UNREADABLE: %s (malformed record)" % path
    if type(rc) is not int or rc not in (0, 1, EXIT_FINDINGS):
        return "MEMORY CHECKS STATUS UNREADABLE: %s (unknown rc %r)" % (path, rc)
    if age < -1:
        return "MEMORY CHECKS STATUS UNREADABLE: timestamp in the future (%s)" % path
    if age > max_age_days:
        return "MEMORY CHECKS STALE: last run %s, %d days ago" % (day, int(age))
    bad = ", ".join("%s %s" % (a, n) for a, n, st, _ in rows if st not in ("clean", "findings"))
    if bad or rc == 1:
        return "MEMORY CHECKS CRASHED (%s): %s" % (day, bad or "see nightly_check.py")
    found = [(a, n, f) for a, n, st, f in rows if st == "findings"]
    if rc == 0 and not found:
        return ""
    if not found:
        return "Memory checks (%s): findings reported; run nightly_check.py for the report" % day
    total = sum(f for _, _, f in found)
    return ("Memory checks (%s): %d findings (%s); run nightly_check.py for the report"
            % (day, total, ", ".join("%s %s %d" % x for x in found)))


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
            fh.write('[paths]\nclaude_home = "%s"\nstate_dir = "%s"\n[agent]\ncodex_home = "%s"\n'
                     % (claude_home, os.path.join(d, "state"), codex_home))
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

        run, _ = fake({("claude", "boot_budget.py"): R(
            1, "BLIND ~/p/.claude/settings.json: cannot read (PermissionError: Operation not "
               "permitted); the check is blind for scope /p")})
        buf = _Buf()
        check("a boot_budget BLIND line is a finding, not a crash",
              run_all(env, run, buf) == 3 and "(findings)" in buf.text)

        # --- environment_render's could-not-check note reaches the report, not the rc
        import environment_render
        saved_check = environment_render.check
        environment_render.check = lambda env=None: (True, 0, "upstream: could not check (no codex)\n")
        try:
            nbuf = _Buf()
            nchecks = []
            nrc = _environment_check({}, nbuf, nchecks)
        finally:
            environment_render.check = saved_check
        check("environment note: in the report text, rc 0, recorded clean not as a finding",
              nrc == 0 and "could not check" in nbuf.text
              and nchecks == [{"agent": "all", "check": "environment_render", "status": "clean",
                               "findings": 0}])

        # --- the status record ---
        status_file = os.path.join(d, "state", "last-check.json")
        t0 = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
        run, _ = fake({("claude", "memory_hygiene_canary.py"): R(3, "# r\n- a\n- b\n"),
                       ("claude", "boot_budget.py"): R(1, "FILE x: 9 chars over budget 1\n"
                                                       "SCOPE y: total\nBLIND z: cannot read\n")})
        buf = _Buf()
        rc = run_all(env, run, buf, now=lambda: t0)
        with open(status_file) as fh:
            doc = json.load(fh)
        check("run_all writes the status file at the derived default path", doc["rc"] == rc == 3)
        check("record: version, UTC timestamp with offset",
              doc["version"] == 1 and doc["timestamp"] == "2026-01-10T12:00:00+00:00")
        check("record: per-check outcome and finding counts",
              {(c["agent"], c["check"]): (c["status"], c["findings"]) for c in doc["checks"]}
              == {("codex", "memory_hygiene_canary"): ("clean", 0),("claude", "memory_hygiene_canary"): ("findings", 2),
                  ("claude", "boot_budget"): ("findings", 3)})
        check("record: report is the stdout text", doc["report"] == buf.text)
        run, _ = fake({("claude", "memory_hygiene_canary.py"): R(1, "Traceback x")})
        run_all(env, run, _Buf(), now=lambda: t0)
        with open(status_file) as fh:
            doc = json.load(fh)
        check("a crash is recorded: rc 1, status crashed",
              doc["rc"] == 1 and doc["checks"][0]["status"] == "crashed")
        run, _ = fake({("claude", "memory_hygiene_canary.py"): R(3, "x" * 30000)})
        run_all(env, run, _Buf(), now=lambda: t0)
        with open(status_file) as fh:
            check("the report is truncated to 20000 chars", len(json.load(fh)["report"]) == 20000)
        run, _ = fake({})
        run_all(env, run, _Buf(), now=lambda: t0)
        with open(status_file) as fh:
            doc = json.load(fh)
        check("a clean run is recorded too (rc 0, all clean)",
              doc["rc"] == 0 and all(c["status"] == "clean" for c in doc["checks"]))
        check("no temp file is left beside the record",
              os.listdir(os.path.dirname(status_file)) == ["last-check.json"])

        # a failure to write the record is loud and rc 1
        blocker = os.path.join(d, "blocker")
        with open(blocker, "w") as fh:
            fh.write("a file where a directory is needed")
        bad_conf = os.path.join(d, "bad.toml")
        with open(bad_conf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s"\nlast_check = "%s"\n'
                     % (claude_home, os.path.join(blocker, "sub", "x.json")))
        buf = _Buf()
        rc = run_all({"SESSION_NOTES_CONFIG": bad_conf, "HOME": d}, run, buf, now=lambda: t0)
        check("a status file that cannot be written: reported on stdout, rc 1",
              rc == 1 and "cannot write the status file" in buf.text)

        # no agent home: still recorded
        empty = os.path.join(d, "empty.toml")
        with open(empty, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s"\nstate_dir = "%s"\n[agent]\ncodex_home = "%s"\n'
                     % (os.path.join(d, "nohome1"), os.path.join(d, "state2"), os.path.join(d, "nohome2")))
        rc = run_all({"SESSION_NOTES_CONFIG": empty, "HOME": d}, run, _Buf(), now=lambda: t0)
        with open(os.path.join(d, "state2", "last-check.json")) as fh:
            check("no agent home: rc 1 and the record says so",
                  rc == 1 and json.load(fh)["rc"] == 1)

        # --- status ---
        def rec(rc, checks, when=t0, name="rec.json"):
            p = os.path.join(d, name)
            with open(p, "w") as fh:
                json.dump({"version": 1, "timestamp": when.isoformat(), "rc": rc,
                           "checks": checks, "report": ""}, fh)
            return p
        later = t0 + timedelta(days=1)
        clean = [{"agent": "claude", "check": "boot_budget", "status": "clean", "findings": 0}]
        check("status: clean and fresh prints nothing",
              status_line(rec(0, clean), 8, later) == "")
        check("status: rc 3 with no finding rows is a findings line, never '0 findings ()'",
              status_line(rec(3, clean), 8, later).startswith("Memory checks (")
              and "0 findings" not in status_line(rec(3, clean), 8, later))
        line = status_line(rec(3, [
            {"agent": "claude", "check": "memory_hygiene_canary", "status": "findings", "findings": 2},
            {"agent": "codex", "check": "memory_hygiene_canary", "status": "findings", "findings": 1}]),
            8, later)
        check("status: findings are summarised on one line",
              line.startswith("Memory checks (2026-01-") and "3 findings" in line
              and "claude memory_hygiene_canary 2" in line and "codex memory_hygiene_canary 1" in line
              and "\n" not in line and line.endswith("run nightly_check.py for the report"))
        line = status_line(rec(1, [{"agent": "claude", "check": "boot_budget",
                                    "status": "crashed", "findings": 0}]), 8, later)
        check("status: a crash is loud and names the check",
              line.startswith("MEMORY CHECKS CRASHED (") and "claude boot_budget" in line)
        check("status: no record = NEVER RAN",
              status_line(os.path.join(d, "absent.json"), 8, later).startswith("MEMORY CHECKS NEVER RAN"))
        garbage = os.path.join(d, "garbage.json")
        with open(garbage, "w") as fh:
            fh.write("{not json")
        check("status: a malformed record is UNREADABLE, with the path",
              status_line(garbage, 8, later).startswith("MEMORY CHECKS STATUS UNREADABLE")
              and garbage in status_line(garbage, 8, later))
        wrong = os.path.join(d, "wrong.json")
        with open(wrong, "w") as fh:
            fh.write('{"rc": 0}')
        check("status: a record missing fields is UNREADABLE",
              status_line(wrong, 8, later).startswith("MEMORY CHECKS STATUS UNREADABLE"))
        old = t0 + timedelta(days=9)
        check("status: older than max age = STALE, even when clean",
              status_line(rec(0, clean), 8, old).startswith("MEMORY CHECKS STALE: last run 2026-01-")
              and "9 days ago" in status_line(rec(0, clean), 8, old))
        check("status: stale outranks findings",
              status_line(rec(3, [{"agent": "claude", "check": "x", "status": "findings",
                                   "findings": 1}]), 8, old).startswith("MEMORY CHECKS STALE"))
        check("status: --max-age-days is honoured",
              status_line(rec(0, clean), 20, old) == "")

        # --- fix round 1: a check that hangs or cannot launch still gets recorded ---
        for exc in (subprocess.TimeoutExpired(["x"], 600), FileNotFoundError(2, "No such file")):
            seen = []

            def boom(argv, env=None, **kw):
                seen.append(os.path.basename(argv[1]))
                if os.path.basename(argv[1]) == "memory_hygiene_canary.py" and env[
                        "SESSION_NOTES_AGENT"] == "claude":
                    raise exc
                return R(0)
            buf = _Buf()
            rc = run_all(env, boom, buf, now=lambda: t0)
            with open(status_file) as fh:
                doc = json.load(fh)
            check("%s from a check: rc 1, CRASHED block naming it, record rewritten, others still run"
                  % type(exc).__name__,
                  rc == 1 and "CRASHED %s" % type(exc).__name__ in buf.text
                  and doc["rc"] == 1 and doc["checks"][0]["status"] == "crashed"
                  and "boot_budget.py" in seen and len(doc["checks"]) == 3)

        def rec2(doc_over, name="rec2.json"):
            base = {"version": 1, "timestamp": t0.isoformat(), "rc": 0, "checks": clean, "report": ""}
            base.update(doc_over)
            p = os.path.join(d, name)
            with open(p, "w") as fh:
                fh.write(json.dumps(base))
            return p
        check("status: unknown rc is UNREADABLE",
              status_line(rec2({"rc": 2}), 8, later).startswith("MEMORY CHECKS STATUS UNREADABLE"))
        check("status: rc 'true' is not rc 1",
              status_line(rec2({"rc": True}), 8, later).startswith("MEMORY CHECKS STATUS UNREADABLE"))
        bad_row = [{"agent": "claude", "check": "x", "status": "weird", "findings": 0}]
        check("status: an unknown row status is loud CRASHED",
              status_line(rec2({"checks": bad_row}), 8, later).startswith("MEMORY CHECKS CRASHED"))
        crow = [{"agent": "claude", "check": "x", "status": "crashed", "findings": 0}]
        check("status: a crashed row with rc 0 is still loud CRASHED",
              status_line(rec2({"checks": crow}), 8, later).startswith("MEMORY CHECKS CRASHED"))
        check("status: a crashed row with rc 3 is still loud CRASHED",
              status_line(rec2({"rc": 3, "checks": crow}), 8, later).startswith("MEMORY CHECKS CRASHED"))
        check("status: year 0001 timestamp is UNREADABLE, not a traceback",
              status_line(rec2({"timestamp": "0001-01-01T00:00:00+00:00"}), 8, later)
              .startswith("MEMORY CHECKS STATUS UNREADABLE"))
        inf = os.path.join(d, "inf.json")
        with open(inf, "w") as fh:
            fh.write('{"version": 1, "timestamp": "%s", "rc": 3, "report": "", "checks": ['
                     '{"agent": "claude", "check": "x", "status": "findings", "findings": Infinity}]}'
                     % t0.isoformat())
        check("status: findings Infinity is UNREADABLE, not a traceback",
              status_line(inf, 8, later).startswith("MEMORY CHECKS STATUS UNREADABLE"))
        check("status: a timestamp more than a day in the future is loud",
              status_line(rec2({}), 8, t0 - timedelta(days=3)).startswith(
                  "MEMORY CHECKS STATUS UNREADABLE: timestamp in the future"))
        check("status: a naive timestamp is UNREADABLE",
              status_line(rec2({"timestamp": "2026-01-10T12:00:00"}), 8, later)
              .startswith("MEMORY CHECKS STATUS UNREADABLE"))


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
    sub = ap.add_subparsers(dest="cmd")
    st = sub.add_parser("status", help="one line from the last run's record; silent when clean")
    st.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.cmd == "status":
        try:
            path = notes_config.load()["paths"]["last_check"]
        except notes_config.ConfigError as exc:
            print("MEMORY CHECKS STATUS UNREADABLE: bad config (%s)" % exc)
            return 0
        try:
            line = status_line(path, a.max_age_days)
        except Exception as exc:  # noqa: BLE001 -- status must never traceback
            line = "MEMORY CHECKS STATUS UNREADABLE: %s (%s)" % (path, type(exc).__name__)
        if line:
            print(line)
        return 0
    return run_all()


if __name__ == "__main__":
    sys.exit(main())
