#!/usr/bin/env python3
"""SessionStart hook: record this session's (tmux pane -> transcript id) pair.
Also registered for UserPromptSubmit, Stop and the idle_prompt Notification,
where it records only whether the pane is working or idle (see TURN STATE).

A pane cannot be resolved to its on-disk transcript by observation: macOS
refuses to let one process read another's environ (so the sibling's
CLAUDE_CODE_SESSION_ID is unreadable), and Claude appends-and-closes its
transcript so there is no open file handle to follow either. The only process
that knows BOTH facts is the session itself, so the session writes them down
once at startup and everyone else reads the note. General pattern: when an
observable signal (cwd, a file handle) stops being an identity signal, have
the owner register its identity explicitly rather than having observers guess.

Contract:
  - ALWAYS exits 0. A session must never fail to start because bookkeeping
    broke; a missing entry degrades the reader to a loud refusal, which is
    the designed fallback, not an outage.
  - Writes nothing when not under tmux (no pane to key on).
  - Atomic replace under an flock, so N sessions starting at once cannot
    interleave a torn file.

An entry also carries a `parent` sub-object when the launcher handed one down
(bin/session_lineage.py explains how); a session started any other way has none.

TURN STATE
  session_peers used to read working/idle from the leading glyph of Claude
  Code's terminal title, and on 2.1.281 that glyph is the idle star even
  mid-turn. So the session reports its own state, the same pattern as above:
  "working" on UserPromptSubmit, "idle" on Stop and SessionStart, and "idle"
  on the idle_prompt notification. One small file per pane, tagged with
  CLAUDE_PID, which the reader checks against the process in the pane
  (session_peers owns the file format).

  idle_prompt is not a reliable Stop substitute: it only fires about 60
  seconds after a Stop that nothing then touches, and an interrupt
  mid-generation produces neither a Stop nor an idle_prompt, so a pane is
  left reading "working" until its next prompt; a reader of this state
  should ignore a "working" record that is more than a couple of hours old
  for exactly this reason. A turn parked at a permission prompt also stays
  "working" on purpose: the permission-prompt notification is not the end
  of a turn.

The registry lives at <state_dir>/session_panes.json (state_dir from
notes_config). It is user-level, not project-scoped: sessions exist
outside any one project, and the reader keys on pane id, which is a tmux fact
rather than a repo fact.
"""
import fcntl
import json
import os
import sys
import tempfile
import time

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


def registry_path(cfg=None):
    """<state_dir>/session_panes.json, resolved at call time (session_lineage owns the name)."""
    import session_lineage
    return session_lineage.registry_path(cfg)


# Entries older than this with no live pane are swept on the next write.
_MAX_AGE_S = 14 * 24 * 3600


def _load(fh):
    """Registry dict from an open handle. A corrupt/empty file yields {}:
    a bad registry must not be able to block a session start."""
    try:
        fh.seek(0)
        raw = fh.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _live_pane_ids():
    """Pane ids tmux currently knows about, or None if tmux can't be asked.

    None and empty-set are deliberately different: None means "no information"
    (don't prune anything), empty means "tmux answered, nothing is live".
    Collapsing them would let a failed tmux call wipe every entry.
    """
    try:
        import subprocess
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    except Exception:
        return None


def _prune(reg, live):
    """Drop entries whose pane is provably gone, and ancient ones regardless.

    Keeps the file from growing without bound across months of sessions
    without ever removing an entry that might still resolve.
    """
    now = time.time()
    out = {}
    for pane, ent in reg.items():
        if not isinstance(ent, dict):
            continue
        if live is not None and pane not in live:
            continue
        if now - float(ent.get("ts") or 0) > _MAX_AGE_S:
            continue
        out[pane] = ent
    return out


TURN_EVENTS = {"UserPromptSubmit": "working", "Stop": "idle", "Notification": "idle"}


def _payload():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_turn_state(pane, event, payload):
    """Write this pane's working/idle state for a turn event. Fail-open."""
    state = TURN_EVENTS.get(event, "idle")
    if event == "Notification" and payload.get("notification_type") not in (None, "idle_prompt"):
        return  # a permission prompt or other notice is not the end of a turn
    claude_pid = os.environ.get("CLAUDE_PID", "").strip()
    if not claude_pid:
        return  # unverifiable, so the reader would have to trust it blindly
    try:
        import session_peers
        session_peers.write_pane_state(pane, state, claude_pid)
    except Exception:
        pass


def main():
    pane = os.environ.get("TMUX_PANE", "").strip()
    if not pane:
        return 0  # not under tmux: nothing to key on, nothing to record

    payload = _payload()
    event = str(payload.get("hook_event_name") or "SessionStart")
    record_turn_state(pane, event, payload)
    if event in TURN_EVENTS:
        return 0  # a turn event updates the state file only, never the registry

    live = _live_pane_ids()
    if event == "SessionStart":
        try:
            import session_peers
            session_peers.sweep_pane_state(live, pane)
        except Exception:
            pass

    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session_id:
        # The hook payload carries it too; prefer env, fall back to stdin.
        session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return 0

    # The reader compares this against the live pane_pid. tmux REUSES pane ids
    # after a pane dies, so pane id alone would happily resolve a dead session's
    # transcript for whoever inherits the id.
    claude_pid = os.environ.get("CLAUDE_PID", "").strip()
    if not claude_pid:
        # Refuse to write an entry the reader cannot verify. An entry without a
        # pid is INDISTINGUISHABLE from a live one at read time, so it would be
        # trusted forever and survive pane-id reuse: the exact confident-wrong
        # resolution this whole mechanism exists to prevent. No entry degrades
        # to a loud refusal, which is the designed failure.
        return 0

    entry = {
        "session_id": session_id,
        "cwd": os.getcwd(),
        "claude_pid": claude_pid,
        "ts": time.time(),
    }
    # Lineage: note who spawned us, when a lineage-aware launcher (handoff_session.py)
    # handed it down via tmux -e. Omit the field entirely rather than writing None/{}, so
    # a reader can tell "no parent recorded" apart from a broken record.
    try:
        import session_lineage
        parent = session_lineage.parent_from_env()
        if parent:
            entry["parent"] = parent
    except Exception:
        pass  # never fail this hook's existing job over the new field

    try:
        REGISTRY = registry_path()
        os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
        lock_path = REGISTRY + ".lock"
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                reg = {}
                if os.path.exists(REGISTRY):
                    with open(REGISTRY, "r") as fh:
                        reg = _load(fh)
                reg[pane] = entry
                reg = _prune(reg, live)
                d = os.path.dirname(REGISTRY)
                fd, tmp = tempfile.mkstemp(dir=d, prefix=".session_panes.")
                try:
                    with os.fdopen(fd, "w") as out:
                        json.dump(reg, out, indent=2, sort_keys=True)
                    os.replace(tmp, REGISTRY)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except Exception:
        return 0  # fail-open, always
    return 0


def selftest():
    """Hermetic: temp state_dir, synthetic pane/session ids, tmux stubbed."""
    import shutil
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    global _live_pane_ids
    tmp = tempfile.mkdtemp(prefix="panereg_")
    conf = os.path.join(tmp, "c.toml")
    with open(conf, "w") as f:
        f.write('[paths]\nstate_dir = "%s"\n' % os.path.join(tmp, "st"))
    old_env, old_live = dict(os.environ), _live_pane_ids
    import io
    sys.stdin = io.StringIO("")  # Claude Code always pipes a payload; a test must never read a tty
    try:
        os.environ["SESSION_NOTES_CONFIG"] = conf
        reg_file = os.path.join(tmp, "st", "session_panes.json")
        check("registry under state_dir", registry_path() == reg_file)

        for k in ("TMUX_PANE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
            os.environ.pop(k, None)
        main()
        check("no tmux: nothing written", not os.path.exists(reg_file))

        os.environ.update(TMUX_PANE="%1", CLAUDE_CODE_SESSION_ID="sess-alpha")
        main()
        check("no pid: refuses to write an unverifiable entry", not os.path.exists(reg_file))

        os.environ["CLAUDE_PID"] = "4242"
        _live_pane_ids = lambda: {"%1", "%2", "%3"}
        main()
        with open(reg_file) as f:
            reg = json.load(f)
        check("entry recorded", reg.get("%1", {}).get("session_id") == "sess-alpha"
              and reg["%1"].get("claude_pid") == "4242")

        reg["%9"] = {"session_id": "gone", "ts": time.time()}
        reg["%2"] = {"session_id": "ancient", "ts": 0}
        with open(reg_file, "w") as f:
            json.dump(reg, f)
        os.environ.update(TMUX_PANE="%2", CLAUDE_CODE_SESSION_ID="sess-beta")
        main()
        with open(reg_file) as f:
            reg = json.load(f)
        check("dead pane pruned", "%9" not in reg)
        check("rewrite of a live pane keeps the fresh entry",
              reg.get("%2", {}).get("session_id") == "sess-beta" and "%1" in reg)

        # Lineage: a parent handed down via tmux -e lands as a `parent` sub-object.
        import session_lineage
        os.environ.update({"TMUX_PANE": "%3", "CLAUDE_CODE_SESSION_ID": "sess-gamma",
                           session_lineage.ENV_SESSION: "work",
                           session_lineage.ENV_CREATED: "1700000000",
                           session_lineage.ENV_PANE: "%1"})
        main()
        with open(reg_file) as f:
            reg = json.load(f)
        check("parent recorded when the launcher handed it down",
              reg.get("%3", {}).get("parent")
              == {"session": "work", "created": "1700000000", "pane": "%1"})
        check("existing fields still written alongside parent",
              reg.get("%3", {}).get("session_id") == "sess-gamma")
        check("a session launched with no parent env has no parent field at all",
              "parent" not in reg.get("%2", {}) and "%2" in reg)
        for k in (session_lineage.ENV_SESSION, session_lineage.ENV_CREATED,
                  session_lineage.ENV_PANE):
            os.environ.pop(k, None)

        # Turn state: one file per pane, written only for a verifiable pid.
        import session_peers

        def turn(event, pane="%3", **extra):
            os.environ["TMUX_PANE"] = pane
            sys.stdin = io.StringIO(json.dumps(dict(extra, hook_event_name=event)))
            main()
            return (session_peers.read_pane_state(pane) or {}).get("state")
        before = os.path.getmtime(reg_file)
        check("UserPromptSubmit: working", turn("UserPromptSubmit") == "working")
        check("turn event leaves the registry alone", os.path.getmtime(reg_file) == before)
        check("permission notification: state unchanged",
              turn("Notification", notification_type="permission_prompt") == "working")
        check("idle_prompt notification: idle (fires ~60s after an untouched Stop)",
              turn("Notification", notification_type="idle_prompt") == "idle")
        check("UserPromptSubmit again: working", turn("UserPromptSubmit") == "working")
        check("Stop: idle", turn("Stop") == "idle")
        check("record carries the pid", session_peers.read_pane_state("%3").get("claude_pid") == "4242")
        os.environ.pop("CLAUDE_PID")
        check("no pid: nothing written", turn("UserPromptSubmit", pane="%8") is None)
        os.environ["CLAUDE_PID"] = "4242"

        # SessionStart also sweeps pane_state/ itself, not just the registry.
        # "%dead" is gone from tmux, "%live" isn't, "%cur" is this invocation's
        # own pane.
        session_peers.write_pane_state("%dead", "idle", "111")
        session_peers.write_pane_state("%live", "idle", "222")
        session_peers.write_pane_state("%cur", "idle", "333")
        inflight = os.path.join(os.path.dirname(session_peers.pane_state_path("%cur")),
                                 ".pane_state.inflight")
        with open(inflight, "w") as f:
            f.write("{}")
        _live_pane_ids = lambda: {"%live", "%cur"}
        os.environ.update(TMUX_PANE="%cur", CLAUDE_CODE_SESSION_ID="sess-delta", CLAUDE_PID="333")
        sys.stdin = io.StringIO(json.dumps({"hook_event_name": "SessionStart"}))
        main()
        check("SessionStart sweep removes a dead pane's state file",
              session_peers.read_pane_state("%dead") is None)
        check("SessionStart sweep keeps a live pane's state file",
              session_peers.read_pane_state("%live") is not None)
        check("SessionStart sweep keeps the current pane's own file",
              session_peers.read_pane_state("%cur") is not None)
        check("SessionStart sweep leaves a fresh in-flight tempfile alone",
              os.path.exists(inflight))

        session_peers.write_pane_state("%dead2", "idle", "444")
        _live_pane_ids = lambda: None
        sys.stdin = io.StringIO(json.dumps({"hook_event_name": "SessionStart"}))
        main()
        check("SessionStart sweep deletes nothing when liveness is unknown (None)",
              session_peers.read_pane_state("%dead2") is not None)

        _live_pane_ids = lambda: None
        check("unknown liveness prunes nothing but age",
              set(_prune({"%5": {"ts": time.time()}, "%6": {"ts": 0}}, None)) == {"%5"})

        with open(reg_file, "w") as f:
            f.write("{corrupt")
        check("corrupt registry does not raise", main() == 0)
    finally:
        sys.stdin = sys.__stdin__
        _live_pane_ids = old_live
        os.environ.clear()
        os.environ.update(old_env)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
