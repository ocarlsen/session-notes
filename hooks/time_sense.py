#!/usr/bin/env python3
"""UserPromptSubmit hook: give Claude a sense of time within a session.

Injects a compact local timestamp plus, on a real gap since the previous
prompt in THIS session, a louder elapsed/stale-context directive (state is
per-session so parallel sessions don't cross-talk). See the tiers below: this
is deliberately SILENT under 30 minutes and is NOT a per-prompt stamp.

Tiers (silent below threshold: a stamp on every prompt is wallpaper the model
learns to ignore):
  first prompt      [now: ...] stamp + UTC-conversion reminder (the boot
                    currentDate has no clock hour, so anchor every session)
  gap < 30 min      NO output at all (the common case)
  gap >= 30 min     [now: ...] stamp + one-line elapsed note
  gap >= stale      [now: ...] stamp + IMPERATIVE auto-refresh directive: run
                    /refresh first, before acting on the prompt
                    (stale = config time.stale_after_minutes)
  date flipped      [now: ...] stamp + explicit "today is now ..." note at ANY
                    gap size (a 20-min gap across midnight still changes "today")

Config (bin/notes_config.py):
  time.timezone            IANA zone for the stamp; empty = system local zone
  time.stale_after_minutes idle minutes before the /refresh directive
  paths.state_dir          per-session last-prompt stamps live in
                           <state_dir>/time-sense/

Fail-safe: any error -> exit 0 with no output; never blocks a prompt.

    python3 time_sense.py --selftest
"""
import json
import os
import sys
import time
from datetime import datetime

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin")

NOTE_GAP = 30 * 60          # 30 min: quiet elapsed note
STALE_MINUTES_DEFAULT = 120  # used only if the config lacks the key
STATE_MAX_AGE = 30 * 86400  # prune per-session state files after 30 days


def human(seconds):
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    parts = []
    if d:
        parts.append("%dd" % d)
    if h:
        parts.append("%dh" % h)
    if m or not parts:
        parts.append("%dm" % m)
    return " ".join(parts[:2])


def load_cfg():
    sys.path.insert(0, BIN)
    import notes_config
    return notes_config.load()


def zone_for(name):
    """A tzinfo for the configured zone, or None for the system local zone."""
    if not name:
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def to_local(ts, tz):
    if tz is None:
        return datetime.fromtimestamp(ts).astimezone()
    return datetime.fromtimestamp(ts, tz)


def run(payload, now, cfg):
    """The hook's output dict, or None when nothing crosses a threshold.
    Reads and updates the per-session stamp under state_dir."""
    session = str(payload.get("session_id") or "default")
    session = "".join(c for c in session if c.isalnum() or c in "-_")[:64] or "default"

    tcfg = cfg.get("time", {})
    tz = zone_for(tcfg.get("timezone", ""))
    stale_gap = 60 * int(tcfg.get("stale_after_minutes", STALE_MINUTES_DEFAULT))
    note_gap = min(NOTE_GAP, stale_gap)

    now_l = to_local(now, tz)
    zone_label = now_l.tzname() or "local"
    stamp = now_l.strftime("[now: %a %Y-%m-%d %H:%M ") + zone_label + "]"

    state_dir = os.path.join(cfg["paths"]["state_dir"], "time-sense")
    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, session)

    last = None
    try:
        with open(state_path) as f:
            last = float(f.read().strip())
    except Exception:
        pass
    with open(state_path, "w") as f:
        f.write(str(now))

    # opportunistic prune of dead sessions' state files
    try:
        for entry in os.scandir(state_dir):
            if entry.is_file() and now - entry.stat().st_mtime > STATE_MAX_AGE:
                os.unlink(entry.path)
    except Exception:
        pass

    # Silent unless a threshold trips; the [now:] stamp rides along only when
    # there's a note worth injecting. Exception: the FIRST prompt of a session
    # always gets the stamp. The boot currentDate carries only the date, so
    # without this the model has no clock hour and infers one from UTC log
    # stamps (and can confidently call early evening "past 9 PM"). Once per
    # session, so the anti-wallpaper thresholding holds.
    lines = []
    if last is None:
        lines.append(
            "\U0001f570 Session start clock anchor (%s). Log stamps ending in Z "
            "are UTC: convert to this zone before stating the day or hour."
            % zone_label
        )
    elif now > last:
        gap = now - last
        last_l = to_local(last, tz)
        date_flipped = last_l.date() != now_l.date()
        if gap >= stale_gap:
            lines.append(
                "⏰ %s have passed since the previous message in this "
                "session: the boot-loaded context (CLAUDE.md imports, RESUME, "
                "memory index) is STALE. RUN /refresh NOW, as your first action "
                "this turn, BEFORE acting on the user's message, then handle the "
                "message with the refreshed context. (An idle session must never "
                "answer from its stale boot snapshot.)" % human(gap)
            )
        elif gap >= note_gap:
            lines.append("⏱ %s since the previous message in this session." % human(gap))
        if date_flipped:
            lines.append(
                "\U0001f4c5 The calendar date changed since the previous message: today "
                "is now %s (%s). Ignore any earlier-loaded currentDate that disagrees."
                % (now_l.strftime("%A %Y-%m-%d"), zone_label)
            )

    if not lines:
        return None  # below every threshold: contribute nothing to the prompt
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join([stamp] + lines),
        },
        "suppressOutput": True,
    }


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    out = run(payload, time.time(), load_cfg())
    if out is not None:
        print(json.dumps(out))


def selftest():
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    def ctx(out):
        return out["hookSpecificOutput"]["additionalContext"] if out else ""

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        conf = os.path.join(home, "config.toml")
        state = os.path.join(home, "state")
        with open(conf, "w") as fh:
            fh.write('[paths]\nstate_dir = "%s"\n[time]\ntimezone = "UTC"\n'
                     "stale_after_minutes = 120\n" % state)
        os.environ["SESSION_NOTES_CONFIG"] = conf
        cfg = load_cfg()

        t0 = 1700000000.0  # 2023-11-14 22:13 UTC
        first = run({"session_id": "alpha"}, t0, cfg)
        check("first prompt anchors", "clock anchor (UTC)" in ctx(first))
        check("stamp present", "[now: Tue 2023-11-14 22:13 UTC]" in ctx(first))
        check("state under state_dir",
              os.path.exists(os.path.join(state, "time-sense", "alpha")))
        check("short gap silent", run({"session_id": "alpha"}, t0 + 600, cfg) is None)
        mid = run({"session_id": "alpha"}, t0 + 600 + 45 * 60, cfg)
        check("30m+ gap notes", "45m since the previous message" in ctx(mid)
              and "/refresh" not in ctx(mid))
        t1 = t0 + 600 + 45 * 60
        stale = run({"session_id": "alpha"}, t1 + 3 * 3600, cfg)
        check("stale gap directs /refresh", "RUN /refresh NOW" in ctx(stale))
        check("date flip noted", "today is now Wednesday 2023-11-15" in ctx(stale))
        check("generic wording", "the user's message" in ctx(stale))
        check("sessions isolated", "clock anchor" in ctx(run({"session_id": "beta"}, t1, cfg)))
        check("hostile session id sanitized",
              run({"session_id": "../../x"}, t0, cfg) is not None
              and os.path.exists(os.path.join(state, "time-sense", "x")))

        with open(conf, "w") as fh:
            fh.write('[paths]\nstate_dir = "%s"\n[time]\nstale_after_minutes = 20\n' % state)
        cfg2 = load_cfg()
        run({"session_id": "gamma"}, t0, cfg2)
        check("configured stale threshold",
              "RUN /refresh NOW" in ctx(run({"session_id": "gamma"}, t0 + 25 * 60, cfg2)))
        check("local zone when timezone empty",
              "[now: " in ctx(run({"session_id": "delta"}, t0, cfg2)))

        empty = os.path.join(home, "empty.toml")
        open(empty, "w").close()
        os.environ["SESSION_NOTES_CONFIG"] = empty
        cfg3 = load_cfg()
        check("defaults keep state in HOME",
              cfg3["paths"]["state_dir"].startswith(home))
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
