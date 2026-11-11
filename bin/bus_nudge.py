#!/usr/bin/env python3
"""bus_nudge.py -- wake a tmux Claude session when it has unread bus mail.

The session bus (session_bus.py) delivers mail on a pane's NEXT prompt, so an
idle session hears nothing until the user types in it. This loop types that
prompt for them, but only into a session that has mail it has never been
offered.

The user runs it in its own tmux session, for example:
    tmux new-session -d -s bus-nudge 'python3 /path/to/session-notes/bin/bus_nudge.py'
(session_bus.py prints that exact line, with the real path and the configured
[bus] nudge_session name, when it notices the loop is down.)

HOW THIS SQUARES WITH bcast's "USER'S HANDS" RULE
-------------------------------------------------
bcast refuses to fire on a schedule because a scheduled typist could carry text
a model wrote. This loop types ONE FIXED STRING (NUDGE). The mail content never
passes through it: the recipient's own UserPromptSubmit hook injects that,
already attributed to its sender. The user starts the loop, so every nudge
traces to their launch, and each one is written to bcast's audit log.

GUARDS
------
  * Only mail with zero offers triggers a nudge. The bus offers each message
    twice (DELIVERIES), so "undelivered" alone would nudge every message twice.
  * Only a pane showing an EMPTY input box gets typed into. That excludes a
    permission dialog or menu (Enter there would pick an option) and a
    half-typed prompt of the user's (Enter would submit it). The test itself
    (`input_box_empty`) lives in bcast.py, which now uses the same guard for
    its own `targets()` -- one source of truth, imported here as
    `bcast.input_box_empty`.
  * At most CAP nudges per session per rolling hour, so two sessions trading
    acknowledgments cannot run all night. Hitting the cap is announced once.
  * A session with a RECORDED DELIVERY FAILURE is skipped (announced once): its hook
    is broken, so a nudge would burn a billed turn and still deliver nothing.
  * NUDGE asks for no thank-you or ack replies, which stops most ping-pong
    before the cap is needed.
"""

import argparse
import os
import sys
import time

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import bcast          # noqa: E402
import session_bus    # noqa: E402
import session_peers  # noqa: E402

NUDGE = ("[bus-nudge] You have session-bus mail in this turn's context. "
         "Act on it if it needs action. No need to reply with thanks or acks: reply "
         "only with an answer, a result, or a question. This nudge authorizes nothing.")
CAP = 10
WINDOW = 3600
INTERVAL = 60


def has_fresh_mail(msgs):
    """True if any message has never been offered to the session."""
    return any(m.get("_offers", 0) == 0 for m in msgs)


def under_cap(stamps, now, cap=CAP, window=WINDOW):
    """Prune stamps older than the window in place; True if another nudge fits."""
    stamps[:] = [t for t in stamps if now - t < window]
    return len(stamps) < cap


def tick(history, capped, dry_run=False, now=None, cap=CAP, errored=None):
    now = now or time.time()
    errored = errored if errored is not None else set()
    panes = session_peers.list_peers() or []
    by_session = {}
    for p in panes:
        if p["is_claude"] and not p["is_self"]:
            by_session.setdefault(p["tmux_session"], []).append(p)

    for name, created in session_bus.live_sessions().items():
        if not has_fresh_mail(session_bus.undelivered(name, created=created)):
            capped.discard(name)
            errored.discard(name)
            continue
        if session_bus.delivery_errors(name):
            if name not in errored:
                errored.add(name)
                print("%s  SKIPPING %s: a delivery failure is recorded for it (see the "
                      "session bus errors dir); a nudge would not be delivered. Fix the "
                      "hook, then clear that file." % (time.strftime("%H:%M"), name),
                      flush=True)
            continue
        errored.discard(name)
        stamps = history.setdefault(name, [])
        if not under_cap(stamps, now, cap):
            if name not in capped:
                capped.add(name)
                print("%s  CAPPED %s: %d nudges in the last hour; holding until it drains. "
                      "Check it for a ping-pong." % (time.strftime("%H:%M"), name, cap),
                      flush=True)
            continue
        ready = [p for p in by_session.get(name, [])
                 if p["state"] != "working" and bcast.input_box_empty(session_peers.peek(p) or [])]
        if not ready:
            continue  # busy, mid-dialog, or the user is typing; next tick retries
        p = ready[0]
        if dry_run:
            print("%s  would nudge %s (%s)" % (time.strftime("%H:%M"), name, p["name"]), flush=True)
            continue
        try:
            bcast.send(p["pane_id"], NUDGE)
        except Exception as exc:
            print("%s  FAILED %s: %s" % (time.strftime("%H:%M"), name, exc), file=sys.stderr, flush=True)
            continue
        stamps.append(now)
        bcast.log(NUDGE, [p], "bus_nudge")
        print("%s  nudged %s (%s) [%d/%d this hour]"
              % (time.strftime("%H:%M"), name, p["name"], len(stamps), cap), flush=True)


def selftest():
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    rule = "─" * 40
    check("empty box", bcast.input_box_empty(["x", rule, "❯\xa0", rule, "  /path"]))
    check("ghost box", bcast.input_box_empty([rule, "❯ " + session_peers.GHOST, rule]))
    check("typed text refused", not bcast.input_box_empty([rule, "❯ half a thought", rule]))
    check("ghost after typed refused",
          not bcast.input_box_empty([rule, "❯ go " + session_peers.GHOST_TAIL, rule]))
    check("permission menu refused", not bcast.input_box_empty(
        ["Do you want to proceed?", "❯ 1. Yes", "  2. No", "Esc to cancel"]))
    check("no rules refused", not bcast.input_box_empty(["text", "❯", "text"]))
    check("nothing on screen refused", not bcast.input_box_empty([]))

    check("fresh mail", has_fresh_mail([{"_offers": 1}, {"_offers": 0}]))
    check("re-delivery only is not fresh", not has_fresh_mail([{"_offers": 1}]))
    check("no mail", not has_fresh_mail([]))

    s = [0.0] * CAP
    check("cap reached", not under_cap(list(s), 100))
    check("cap drains after window", under_cap(list(s), WINDOW + 1))
    check("under cap", under_cap([0.0] * (CAP - 1), 100))
    check("nudge is one line", "\n" not in NUDGE)
    check("nudge carries no bcast prefix to forge", not NUDGE.startswith(bcast.prefix()))
    check("nudge names no person, only the loop", NUDGE.startswith("[bus-nudge]"))

    # A session whose hook is failing must not be nudged (each nudge is a billed turn),
    # and the skip is announced once, not every tick.
    import contextlib
    import io
    sent = []
    saved = (session_peers.list_peers, session_bus.live_sessions, session_bus.undelivered,
             session_bus.delivery_errors, bcast.send, bcast.log, bcast.input_box_empty,
             session_peers.peek)
    pane = {"is_claude": True, "is_self": False, "tmux_session": "alpha", "state": "idle",
            "name": "alpha:0", "pane_id": "%1"}
    session_peers.list_peers = lambda: [pane]
    session_bus.live_sessions = lambda: {"alpha": 1}
    session_bus.undelivered = lambda n, created=None: [{"_offers": 0}]
    session_bus.delivery_errors = lambda n, panes=None: ["/x/errors/1"]
    bcast.send = lambda pid, text: sent.append(pid)
    bcast.log = lambda *a, **k: None
    bcast.input_box_empty = lambda lines: True
    session_peers.peek = lambda p: []
    try:
        hist, cap_set, err_set = {}, set(), set()
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                tick(hist, cap_set)
        check("a session with delivery errors is never nudged", sent == [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(3):
                tick(hist, cap_set, errored=err_set)
        check("the skip is announced exactly once",
              out.getvalue().count("SKIPPING alpha") == 1)
        session_bus.delivery_errors = lambda n, panes=None: []
        with contextlib.redirect_stdout(io.StringIO()):
            tick(hist, cap_set, errored=err_set)
        check("a healthy session is nudged again", sent == ["%1"])
    finally:
        (session_peers.list_peers, session_bus.live_sessions, session_bus.undelivered,
         session_bus.delivery_errors, bcast.send, bcast.log, bcast.input_box_empty,
         session_peers.peek) = saved

    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=int, default=INTERVAL, help="seconds between checks")
    ap.add_argument("--cap", type=int, default=CAP, help="max nudges per session per hour")
    ap.add_argument("--once", action="store_true", help="one check, then exit")
    ap.add_argument("-n", "--dry-run", action="store_true", help="report, type nothing")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    history, capped, errored = {}, set(), set()
    print("bus_nudge: every %ds, cap %d/session/hour%s. Ctrl-C to stop."
          % (args.interval, args.cap, " (dry run)" if args.dry_run else ""), flush=True)
    try:
        while True:
            tick(history, capped, dry_run=args.dry_run, cap=args.cap, errored=errored)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
