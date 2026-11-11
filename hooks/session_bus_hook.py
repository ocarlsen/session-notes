#!/usr/bin/env python3
"""UserPromptSubmit + Stop hook: deliver inter-session messages to THIS session.

Register it for both events. On UserPromptSubmit it delivers; on Stop it retires
what that turn was offered (see RETIRE ON STOP below).

The send half is bin/session_bus.py in this repo. This is the delivery half, and
it exists because an interactive session has no interrupt: it reads a file only
when its model decides to, so a shared bus file with no delivery mechanism is
just a file nobody opens. Hooking UserPromptSubmit is the closest thing to an
interrupt Claude Code offers.

Behaviour:
  - SILENT when the mailbox is empty (the overwhelmingly common case).
  - The FIRST time it ever delivers to a given session it appends a one-time
    capability primer including the reply syntax. That is the retrofit path:
    a session that booted before this existed does not need to have been told
    about it, because receiving one message teaches it how to reply.

FREE AT REST (load-bearing): this runs on EVERY prompt in EVERY session, so the
empty-mailbox path must touch nothing expensive. It reads one small file and
returns; it does NOT shell out to tmux unless there is actually mail. An earlier
version resolved identity via tmux on every prompt, which put a subprocess on
the critical path of every prompt the user ever types, and a wedged tmux server
would have added seconds to each one fleet-wide. The fail-safe below catches
exceptions, not hangs, so the cheapest fix is not to make the call.

DELIVERY ORDER IS LOAD-BEARING: emit the injection FIRST, mark delivered after.
A handoff bus must be at-least-once. Marking first means a kill, a hook timeout,
or a serialization error between the two silently eats the message, which is the
one outcome the module's own docstring says is worse than not sending. Reversed,
the same crash costs at most a duplicated message on the next prompt.

RETIRE ON STOP: every message is offered on up to DELIVERIES prompts, because an
interrupted turn loses its injected context. A turn that finishes has read its
mail, and offering it again made a session re-read a handoff spec as new work.
So each delivery records the ids offered to this pane's turn, and the Stop hook
(which Claude Code does not run for an interrupted turn) retires them. Free at
rest there too: with no turn record on disk, Stop returns before touching tmux.

Fail-safe: any error -> exit 0 with no output; never blocks a prompt. But it is
no longer SILENT about it: the traceback is recorded under
<state_dir>/session-bus/errors/<pane>,
which `session_bus.py peers` surfaces and `send` warns on. Swallowing the error
without a trace made a broken bus and an empty one look identical.
"""
import json
import os
import sys
import time
import traceback

# realpath: install.sh symlinks this file, and the bus module lives in the
# repo's bin/, beside notes_config.py.
BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin")
sys.path.insert(0, BIN)


def fallback_errors_dir():
    """Where to record a failure when session_bus itself cannot be imported.
    Still under the configured state_dir if notes_config loads; the default
    state_dir otherwise, so a broken config is recorded rather than lost."""
    try:
        import notes_config
        root = notes_config.load()["paths"]["state_dir"]
    except Exception:
        try:
            import notes_config
            root = os.path.expanduser(notes_config.DEFAULTS["paths"]["state_dir"])
        except Exception:
            root = os.path.expanduser("~/.claude/session-notes/state")
    return os.path.join(root, "session-bus", "errors")


def main():
    try:
        payload = json.load(sys.stdin)  # identity comes from tmux, not the UUID
    except Exception:
        payload = {}

    import session_bus as bus

    if isinstance(payload, dict) and payload.get("hook_event_name") == "Stop":
        return on_stop(bus)

    # Cheap gate BEFORE any subprocess: if nothing is live on the bus, this
    # session cannot have mail, so do not pay for identity resolution.
    if not bus._live(bus._read()):
        return

    me, created = bus.session_info()
    if not me:
        return  # not in tmux: no address, nothing to deliver

    msgs = bus.undelivered(me, created=created)
    if not msgs:
        return

    key = bus.state_key(me, created)
    primed_path = os.path.join(bus.primed_path(), key)
    first_ever = not os.path.exists(primed_path)

    # A pure-broadcast delivery never shows the primer, even on a
    # session's first-ever delivery -- a "reply syntax" lesson triggered by
    # --all noise is the wrong moment to teach it (the recipient wasn't
    # personally addressed, so a reply is unlikely to be the right next
    # action). Deliberately do NOT mark `first_ever` consumed in that case:
    # leaving primed_path unwritten means the primer still fires on this
    # session's first ADDRESSED delivery, which is when the lesson is
    # actually useful.
    all_broadcast = bool(msgs) and all(m.get("to") == bus.BROADCAST for m in msgs)
    show_primer = first_ever and not all_broadcast

    text = bus.render_injection(me, msgs, primer=show_primer)
    payload = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        },
        "suppressOutput": True,
    })

    # Emit BEFORE recording delivery (see DELIVERY ORDER above).
    print(payload)
    sys.stdout.flush()

    ids = [m.get("id") for m in msgs]
    bus.record_offer(key, ids)
    bus.record_turn(key, os.environ.get("TMUX_PANE"), ids)
    if show_primer:
        try:
            os.makedirs(bus.primed_path(), exist_ok=True)
            with open(primed_path, "w") as f:
                f.write("1")
        except Exception:
            pass


def on_stop(bus):
    """The turn finished: retire the mail it was offered."""
    # Gate on THIS pane's turn file, not the shared dir: another pane's leftover
    # (an interrupted turn whose pane then exited) must not cost every Stop a tmux
    # call. The key needs tmux, so match the pane suffix _turn_file writes.
    suffix = os.path.basename(bus._turn_file("", os.environ.get("TMUX_PANE")))
    try:
        if not any(e.is_file() and e.name.endswith(suffix)
                   for e in os.scandir(bus.turn_path())):
            return  # nothing offered to this pane: no tmux call
    except FileNotFoundError:
        return
    me, created = bus.session_info()
    if not me:
        return
    bus.record_done(bus.state_key(me, created), os.environ.get("TMUX_PANE"))


def record_failure():
    """Leave a readable trace of a swallowed failure, keyed by PANE.

    Pane id comes from the environment, so this still works when the failure was
    tmux itself or the import of session_bus.
    """
    try:
        try:
            import session_bus as bus
            d = bus.errors_path()
        except Exception:
            d = fallback_errors_dir()
        os.makedirs(d, exist_ok=True)
        pane = os.environ.get("TMUX_PANE", "unknown")
        safe = "".join(c for c in pane if c.isalnum() or c in "-_")[:64] or "unknown"
        with open(os.path.join(d, safe), "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
            f.write(traceback.format_exc())
    except Exception:
        pass


def clear_failure():
    """A hook run that finished cleanly retires this pane's stale failure trace, so
    bus_nudge and `send` stop treating a long-fixed failure as live."""
    try:
        try:
            import session_bus as bus
            d = bus.errors_path()
        except Exception:
            d = fallback_errors_dir()
        pane = os.environ.get("TMUX_PANE", "unknown")
        safe = "".join(c for c in pane if c.isalnum() or c in "-_")[:64] or "unknown"
        os.unlink(os.path.join(d, safe))
    except Exception:
        pass


def run_hook():
    """main() with every failure swallowed into a trace; success clears the trace."""
    try:
        main()
    except Exception:
        record_failure()
    else:
        clear_failure()


def selftest():
    """Hermetic: a temp state_dir, a stubbed identity, no tmux."""
    import contextlib
    import io
    import shutil
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="bushook_")
    conf = os.path.join(tmp, "c.toml")
    with open(conf, "w") as f:
        f.write('[paths]\nstate_dir = "%s"\n' % os.path.join(tmp, "st"))
    old_env = dict(os.environ)
    old_stdin = sys.stdin
    os.environ["SESSION_NOTES_CONFIG"] = conf
    try:
        import session_bus as bus
        bus._STATE, bus.BUS = None, None

        def run():
            sys.stdin = io.StringIO("{}")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main()
            return out.getvalue()

        calls = []
        bus.session_info = lambda env=None: calls.append(1) or ("alpha", None)
        check("empty bus: silent", run() == "")
        check("empty bus: identity never resolved (free at rest)", not calls)

        bus.append({"id": "m1", "ts": time.time(), "from": "beta", "from_created": 1,
                    "to": "alpha", "body": "hello"})
        first = run()
        ctx = json.loads(first)["hookSpecificOutput"]["additionalContext"] if first else ""
        check("addressed mail is injected", "hello" in ctx)
        check("first delivery carries the reply primer", "send --to beta" in ctx)
        check("primer recorded under state_dir",
              os.path.exists(os.path.join(bus.primed_path(), bus.state_key("alpha", None))))
        second = run()
        check("second prompt re-delivers, flagged", "RE-DELIVERY" in second
              and "send --to beta" not in second)
        check("retired after DELIVERIES offers", run() == "")

        def stop():
            sys.stdin = io.StringIO('{"hook_event_name": "Stop"}')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main()
            return out.getvalue()

        # A turn that FINISHES retires its mail: no second offer.
        bus.append({"id": "m2", "ts": time.time(), "from": "beta", "from_created": 1,
                    "to": "alpha", "body": "spec two"})
        check("new mail is delivered", "spec two" in run())
        check("Stop is silent", stop() == "")
        check("after a finished turn, no re-delivery", run() == "")

        # An interrupted turn gets no Stop: the second offer still comes.
        bus.append({"id": "m3", "ts": time.time(), "from": "beta", "from_created": 1,
                    "to": "alpha", "body": "spec three"})
        check("mail delivered to a turn that is then interrupted", "spec three" in run())
        again = run()
        check("interrupted turn (no Stop): re-delivered, flagged",
              "spec three" in again and "RE-DELIVERY" in again)
        check("and retired after that", run() == "")

        # Stop retires only what THIS pane's turn was offered.
        bus.append({"id": "m4", "ts": time.time(), "from": "beta", "from_created": 1,
                    "to": "alpha", "body": "spec four"})
        os.environ["TMUX_PANE"] = "%1"
        run()
        os.environ["TMUX_PANE"] = "%2"
        stop()
        os.environ["TMUX_PANE"] = "%1"
        check("another pane's Stop does not retire this pane's offer", "spec four" in run())

        # Free at rest: no turn record anywhere, Stop never resolves identity.
        shutil.rmtree(bus.turn_path(), ignore_errors=True)
        calls.clear()
        stop()
        check("Stop with no turn record: identity never resolved", not calls)

        # Another pane's leftover turn file is not this pane's turn: no tmux call.
        os.makedirs(bus.turn_path(), exist_ok=True)
        with open(bus._turn_file(bus.state_key("gone", 1), "%9"), "w") as f:
            f.write("m9\n")
        os.environ["TMUX_PANE"] = "%1"
        calls.clear()
        stop()
        check("another pane's leftover turn file: identity never resolved", not calls)
        with open(bus._turn_file(bus.state_key("alpha", None), "%1"), "w") as f:
            f.write("m9\n")
        stop()
        check("this pane's own turn file still resolves identity", calls)

        os.environ["TMUX_PANE"] = "%7"
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            record_failure()
        trace = os.path.join(bus.errors_path(), "7")
        check("failure trace lands under state_dir", os.path.exists(trace)
              and trace.startswith(os.path.join(tmp, "st")))
        import subprocess
        subprocess.run([sys.executable, os.path.abspath(__file__)], input="{}",
                       capture_output=True, text=True, env=dict(os.environ), timeout=60)
        check("a clean hook run clears this pane's stale failure trace",
              not os.path.exists(trace))
        check("fallback errors dir is under the configured state_dir",
              fallback_errors_dir() == os.path.join(tmp, "st", "session-bus", "errors"))
    finally:
        sys.stdin = old_stdin
        os.environ.clear()
        os.environ.update(old_env)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    run_hook()
    sys.exit(0)
