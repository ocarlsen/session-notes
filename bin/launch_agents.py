#!/usr/bin/env python3
"""launch_agents -- run session-notes' background jobs as macOS LaunchAgents.

Two jobs, each installed only after a yes:

  nightly-check   nightly_check.py every night at 04:00: the memory checks for
                  every installed agent. Output goes to the log.
  bus-nudge       bus_nudge.py, started at login and restarted by launchd if it
                  exits, so idle sessions wake for bus mail without a tmux pane
                  kept open for the loop.

    python3 launch_agents.py status
    python3 launch_agents.py install nightly-check [--yes] [--dry-run]
    python3 launch_agents.py install bus-nudge [--yes] [--dry-run]
    python3 launch_agents.py uninstall bus-nudge
    python3 launch_agents.py --selftest

macOS only for now. Logs: ~/Library/Logs/session-notes/<job>.{out,err}.
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

BIN = os.path.dirname(os.path.realpath(__file__))
# The path the user's links point at (abspath, not realpath), so a job runs the
# linked tool when there is one, the same rule as session_bus's restart line.
LAUNCH_BIN = os.path.dirname(os.path.abspath(__file__))

PREFIX = "com.session-notes."
NIGHTLY_HOUR = 4
JOBS = ("nightly-check", "bus-nudge")


def label(job):
    return PREFIX + job


def plist_path(job, home=None):
    home = home or os.path.expanduser("~")
    return os.path.join(home, "Library", "LaunchAgents", label(job) + ".plist")


def log_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), "Library", "Logs", "session-notes")


def job_path_env(which=shutil.which):
    """PATH for a job. launchd starts jobs with a bare PATH that usually lacks tmux
    (Homebrew puts it in /opt/homebrew/bin), so the dir tmux lives in goes first."""
    dirs = []
    tmux = which("tmux")
    if tmux:
        dirs.append(os.path.dirname(tmux))
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def job_spec(job, python, home=None, which=shutil.which):
    """The plist dict for one job."""
    home = home or os.path.expanduser("~")
    script = {"nightly-check": "nightly_check.py", "bus-nudge": "bus_nudge.py"}[job]
    spec = {
        "Label": label(job),
        "ProgramArguments": [python, os.path.join(LAUNCH_BIN, script)],
        "EnvironmentVariables": {"HOME": home, "PATH": job_path_env(which)},
        "WorkingDirectory": home,
        "StandardOutPath": os.path.join(log_dir(home), job + ".out"),
        "StandardErrorPath": os.path.join(log_dir(home), job + ".err"),
        "ProcessType": "Background",
    }
    if job == "nightly-check":
        spec["StartCalendarInterval"] = {"Hour": NIGHTLY_HOUR, "Minute": 0}
    else:
        spec["RunAtLoad"] = True
        spec["KeepAlive"] = True
        # A loop that crashes at start (a bad config, say) is restarted at most once a
        # minute, so its traceback cannot flood the unrotated .err log.
        spec["ThrottleInterval"] = 60
        # The loop prints one line per state change; unbuffered so the log is live.
        spec["EnvironmentVariables"]["PYTHONUNBUFFERED"] = "1"
    return spec


def describe(job):
    return {"nightly-check": "the memory checks every night at %02d:00" % NIGHTLY_HOUR,
            "bus-nudge": "the bus_nudge loop, at login and restarted if it exits"}[job]


def kickstart_line(job):
    return "launchctl kickstart -k gui/%d/%s" % (os.getuid(), label(job))


def install(job, dry=False, yes=False, home=None, ask=None, out=None, run=subprocess.run,
            platform=None, python=None):
    out = out or sys.stdout
    if (platform or sys.platform) != "darwin":
        out.write("launch_agents: macOS only for now; run %s yourself on this system\n"
                  % job)
        return 0
    python = python or shutil.which("python3") or sys.executable
    path = plist_path(job, home)
    data = plistlib.dumps(job_spec(job, python, home)).decode("utf-8")
    out.write("%s: %s\n  %s\n" % (job, describe(job), path))
    if dry:
        out.write(data)
        return 0
    if os.path.exists(path):
        out.write("already installed; leaving it (uninstall first to change it)\n")
        return 0
    if not yes and not (ask or _ask)("Install it?"):
        out.write("not installed\n")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(log_dir(home), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(data)
    r = run(["launchctl", "load", "-w", path], capture_output=True, text=True)
    if r.returncode != 0:
        out.write("wrote %s, but launchctl load failed: %s\n" % (path, (r.stderr or "").strip()))
        return 1
    out.write("installed and loaded: %s\n" % label(job))
    return 0


def uninstall(job, home=None, out=None, run=subprocess.run):
    out = out or sys.stdout
    path = plist_path(job, home)
    if not os.path.exists(path):
        out.write("%s: not installed\n" % job)
        return 0
    run(["launchctl", "unload", "-w", path], capture_output=True, text=True)
    os.remove(path)
    out.write("%s: unloaded and removed %s\n" % (job, path))
    return 0


def installed(job, home=None):
    return os.path.exists(plist_path(job, home))


def status(home=None, out=None):
    out = out or sys.stdout
    for job in JOBS:
        out.write("%-14s %s\n" % (job, "installed (%s)" % plist_path(job, home)
                                  if installed(job, home) else "not installed"))
    return 0


def _ask(q):
    if not sys.stdin.isatty():
        return False
    try:
        return input(q + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def selftest():
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    class Buf(object):
        text = ""

        def write(self, s):
            self.text += s

    calls = []

    def run(argv, **kw):
        calls.append(argv)

        class R(object):
            returncode, stderr = 0, ""
        return R()

    with tempfile.TemporaryDirectory() as home:
        def which(name):
            return "/opt/tools/bin/tmux" if name == "tmux" else None
        bn = job_spec("bus-nudge", "/usr/bin/python3", home, which)
        check("bus-nudge: KeepAlive + RunAtLoad, no calendar",
              bn["KeepAlive"] is True and bn["RunAtLoad"] is True and "StartCalendarInterval" not in bn)
        check("bus-nudge restarts at most once a minute", bn["ThrottleInterval"] == 60)
        check("job PATH starts with the dir tmux lives in",
              bn["EnvironmentVariables"]["PATH"].split(":")[0] == "/opt/tools/bin")
        check("bus-nudge runs the linked bus_nudge.py",
              bn["ProgramArguments"][1] == os.path.join(LAUNCH_BIN, "bus_nudge.py"))
        nc = job_spec("nightly-check", "/usr/bin/python3", home, which)
        check("nightly-check: a calendar job, not kept alive",
              nc["StartCalendarInterval"] == {"Hour": NIGHTLY_HOUR, "Minute": 0}
              and "KeepAlive" not in nc)

        b = Buf()
        install("bus-nudge", dry=True, home=home, out=b, run=run, platform="darwin")
        check("--dry-run prints a valid plist and writes nothing",
              plistlib.loads(b.text[b.text.index("<?xml"):].encode())["Label"] == label("bus-nudge")
              and not os.path.exists(plist_path("bus-nudge", home)) and not calls)
        b = Buf()
        install("bus-nudge", home=home, out=b, run=run, platform="darwin", ask=lambda q: False)
        check("no yes: nothing written or loaded",
              "not installed" in b.text and not os.path.exists(plist_path("bus-nudge", home))
              and not calls)
        b = Buf()
        rc = install("bus-nudge", yes=True, home=home, out=b, run=run, platform="darwin")
        check("--yes: plist written and loaded",
              rc == 0 and installed("bus-nudge", home)
              and calls == [["launchctl", "load", "-w", plist_path("bus-nudge", home)]])
        b = Buf()
        install("bus-nudge", yes=True, home=home, out=b, run=run, platform="darwin")
        check("a second install leaves the existing job alone",
              "already installed" in b.text and len(calls) == 1)
        uninstall("bus-nudge", home=home, out=Buf(), run=run)
        check("uninstall unloads and removes",
              not installed("bus-nudge", home) and calls[-1][:2] == ["launchctl", "unload"])
        b = Buf()
        install("bus-nudge", yes=True, home=home, out=b, run=run, platform="linux")
        check("not macOS: says so, writes nothing",
              "macOS only" in b.text and not installed("bus-nudge", home))

    print("all ok" if not fails else "%d FAILED" % len(fails))
    return 0 if not fails else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", nargs="?", choices=["status", "install", "uninstall"])
    ap.add_argument("job", nargs="?", choices=JOBS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.cmd in ("install", "uninstall") and not a.job:
        ap.error("%s needs a job: %s" % (a.cmd, ", ".join(JOBS)))
    if a.cmd == "install":
        return install(a.job, dry=a.dry_run, yes=a.yes)
    if a.cmd == "uninstall":
        return uninstall(a.job)
    return status()


if __name__ == "__main__":
    sys.exit(main())
