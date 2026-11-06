#!/usr/bin/env python3
"""Smoke tests for the hooks, run the way Claude Code runs them.

Each hook is started as a subprocess with a JSON payload on stdin, HOME set
to a temp dir and no tmux. A hook must exit 0 (or 2 where blocking is its
job), print nothing or one valid JSON object, finish fast, and leave no
swallowed traceback in the state dir. That last check matters: the hooks
catch every exception so they can never block a prompt, which means a
broken hook and a quiet one look the same unless something reads the trace.

    python3 tests/test_hooks.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "hooks")
EM = chr(0x2014)


def run_hook(name, payload, home, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["HOME"] = home
    env["SESSION_NOTES_CONFIG"] = os.path.join(home, "config.toml")
    env.update(extra_env or {})
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.join(HOOKS, name)],
                       input=payload if isinstance(payload, str) else json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=20)
    return r, time.time() - t0


def swallowed_traces(home):
    out = []
    for dirpath, _, files in os.walk(home):
        if os.path.basename(dirpath) == "errors":
            out.extend(os.path.join(dirpath, f) for f in files)
    return out


def main():
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + detail))
        if not cond:
            fails.append(name)

    def well_formed(name, r, elapsed, codes=(0,)):
        check(name + ": exit code", r.returncode in codes,
              "rc=%d stderr=%s" % (r.returncode, r.stderr[-300:]))
        out = r.stdout.strip()
        if out:
            try:
                json.loads(out)
                ok = True
            except ValueError:
                ok = False
            check(name + ": stdout is one JSON object", ok, out[:200])
        check(name + ": fast", elapsed < 5, "%.1fs" % elapsed)

    with tempfile.TemporaryDirectory() as home:
        open(os.path.join(home, "config.toml"), "w").close()  # empty = defaults
        work = os.path.join(home, "work")
        os.makedirs(work)
        base = {"session_id": "test-session", "cwd": work,
                "transcript_path": os.path.join(home, "t.jsonl")}

        r, el = run_hook("session_pane_registry.py",
                         dict(base, hook_event_name="SessionStart", source="startup"), home)
        well_formed("session_pane_registry", r, el)
        check("session_pane_registry: silent", r.stdout.strip() == "", r.stdout[:200])

        r, el = run_hook("session_bus_hook.py",
                         dict(base, hook_event_name="UserPromptSubmit", prompt="hi"), home)
        well_formed("session_bus_hook", r, el)
        check("session_bus_hook: silent with empty mailbox", r.stdout.strip() == "",
              r.stdout[:200])

        r, el = run_hook("session_bus_hook.py", dict(base, hook_event_name="Stop"), home)
        well_formed("session_bus_hook on Stop", r, el)
        check("session_bus_hook: silent on Stop", r.stdout.strip() == "", r.stdout[:200])

        # The pane-registry hook on turn events: working/idle per pane, nothing printed.
        pane_env = {"TMUX_PANE": "%5", "CLAUDE_PID": "1234"}
        state_file = os.path.join(home, ".claude", "session-notes", "state", "pane_state", "5.json")

        def recorded():
            try:
                with open(state_file) as fh:
                    return json.load(fh).get("state")
            except (OSError, ValueError):
                return None
        for event, want in (("UserPromptSubmit", "working"), ("Stop", "idle")):
            r, el = run_hook("session_pane_registry.py",
                             dict(base, hook_event_name=event, prompt="hi"), home, pane_env)
            well_formed("session_pane_registry on " + event, r, el)
            check("session_pane_registry: silent on " + event, r.stdout.strip() == "", r.stdout[:200])
            check("session_pane_registry: %s records %s" % (event, want), recorded() == want,
                  "state=%r" % recorded())
        r, el = run_hook("session_pane_registry.py", "not json at all", home, pane_env)
        well_formed("session_pane_registry bad payload", r, el)

        r, el = run_hook("time_sense.py",
                         dict(base, hook_event_name="UserPromptSubmit", prompt="hi"), home)
        well_formed("time_sense", r, el)

        clean = os.path.join(work, "clean.md")
        dirty = os.path.join(work, "dirty.md")
        with open(clean, "w") as fh:
            fh.write("plain text, no dashes\n")
        with open(dirty, "w") as fh:
            fh.write("one " + EM + " two\n")
        tool = lambda path: dict(base, hook_event_name="PostToolUse", tool_name="Write",
                                 tool_input={"file_path": path, "content": open(path).read()},
                                 tool_response={"filePath": path, "success": True})
        r, el = run_hook("em_dash_guard.py", tool(clean), home)
        well_formed("em_dash_guard clean", r, el)
        check("em_dash_guard: clean file is silent",
              r.stdout.strip() == "" and r.returncode == 0, r.stdout[:200])
        r, el = run_hook("em_dash_guard.py", tool(dirty), home)
        well_formed("em_dash_guard dirty", r, el, codes=(0, 2))
        check("em_dash_guard: dirty file is flagged",
              bool(r.stdout.strip() or r.stderr.strip()), "no output on an em dash")

        r, el = run_hook("session_bus_hook.py", "not json at all", home)
        well_formed("session_bus_hook bad payload", r, el)

        traces = swallowed_traces(home)
        check("no swallowed tracebacks", not traces,
              "; ".join(open(t).read()[-300:] for t in traces))

    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
