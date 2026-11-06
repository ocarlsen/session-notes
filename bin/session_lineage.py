#!/usr/bin/env python3
"""session_lineage.py -- record which session launched which.

WHY THIS EXISTS
    session_peers.py and session_bus.py answer "who is live right now", but
    neither answers "who launched whom". A handoff chain (your session runs
    handoff_session.py to spawn a worker, that worker spawns another) has no
    record of its own shape once the panes are up, so a worker found
    mid-chain cannot be told which session started it without someone
    relaying it by hand.

THE ONE RECORD
    <state_dir>/session_panes.json, the existing pane registry, written only
    by the SessionStart hook hooks/session_pane_registry.py. No new store, no
    tmux user option, no separate spawn log: the hook already writes one entry
    per session at startup, and this module teaches it to also note the
    session that spawned it, as a `parent` sub-object of that entry.

HOW THE PARENT CROSSES THE PROCESS BOUNDARY
    A child session cannot read its parent pane's environment. Instead the
    LAUNCHER (bin/handoff_session.py) resolves its OWN tmux identity at spawn
    time and hands it to the child with `tmux new-session -e VAR=value`
    (repeatable since tmux 3.2). The child's SessionStart hook reads those
    three env vars and writes them into its own registry entry. A session
    launched any other way (a plain `tmux new-session`, a claude typed into an
    existing shell) has none of the vars set, so it registers with no parent:
    a root, not an error.
    The vars are tmux SESSION environment, so a claude started later in a new
    window of the child session inherits them too and records the same parent:
    lineage is per tmux session, not per pane.

PUBLIC API
    ENV_SESSION, ENV_CREATED, ENV_PANE   the three env var names
    spawn_env_args(env=None, run=None)   LAUNCHER: -e args to hand the child
    parent_from_env(env=None)            CHILD's hook: read what was handed down
    registry_path(cfg=None), read_registry(path=None)
    live_sessions(run=None)              [{"name", "created"}] or None
    parent_status(parent, sessions)      THE liveness rule; readers call it
    parents_by_session(registry, panes)  {tmux session: parent}, pid-verified
    build_tree(sessions, parents, live=None)

USAGE
    session_lineage.py me          this session's handed-down parent + liveness
    session_lineage.py --selftest
"""
import argparse
import json
import os
import re
import subprocess
import sys

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)

ENV_SESSION = "SESSION_NOTES_PARENT_SESSION"
ENV_CREATED = "SESSION_NOTES_PARENT_CREATED"
ENV_PANE = "SESSION_NOTES_PARENT_PANE"

REGISTRY_NAME = "session_panes.json"


def _str(v):
    """Stable string form for a `created` epoch, whatever type it arrived as; None -> ""."""
    if v is None:
        return ""
    return str(v).strip()


# --- the record ----------------------------------------------------------------

def registry_path(cfg=None):
    """<state_dir>/session_panes.json, resolved at call time."""
    import notes_config
    cfg = cfg or notes_config.load()
    return os.path.join(cfg["paths"]["state_dir"], REGISTRY_NAME)


def read_registry(path=None):
    """The registry dict, {} when missing or corrupt. A reader never raises
    over a bad file: the hook writes it best-effort."""
    try:
        with open(path or registry_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --- launcher side: what to hand the child --------------------------------------

MIN_TMUX = (3, 2)


def spawn_env_args(env=None, run=None):
    """-e args for `tmux new-session`, identifying THIS pane as the parent.

    [] whenever we are not usably inside tmux, or the tmux query fails or
    returns something we don't trust. Never raises, so a launcher can call
    this unconditionally. `run` is injectable (defaults to subprocess.run) so a
    test can stub tmux without a real server.
    """
    env = os.environ if env is None else env
    run = run or subprocess.run

    if not env.get("TMUX") or not env.get("TMUX_PANE"):
        return []

    # `new-session -e` needs tmux 3.2+. On an older tmux, hand the launcher no -e args so it
    # launches a parentless child instead of failing on an unknown option. A version we
    # cannot read is not a reason to give up the record: carry on.
    try:
        ver = run(["tmux", "-V"], capture_output=True, text=True, timeout=5)
        m = re.search(r"(\d+)\.(\d+)", ver.stdout or "") if ver.returncode == 0 else None
        if m and (int(m.group(1)), int(m.group(2))) < MIN_TMUX:
            print("session_lineage: %s has no `new-session -e` (needs %d.%d+); "
                  "spawning without parent env" % ((ver.stdout or "").strip(), MIN_TMUX[0], MIN_TMUX[1]),
                  file=sys.stderr)
            return []
    except Exception:
        pass

    pane = env.get("TMUX_PANE")
    cmd = ["tmux", "display-message", "-p", "-t", pane,
           "#{session_name}\t#{session_created}\t#{pane_id}"]
    try:
        out = run(cmd, capture_output=True, text=True, timeout=5)
    except Exception as e:
        print("session_lineage: tmux display-message failed (%s); "
              "spawning without parent env" % e, file=sys.stderr)
        return []

    if out.returncode != 0:
        print("session_lineage: tmux display-message exited %s; "
              "spawning without parent env" % out.returncode, file=sys.stderr)
        return []

    parts = [p.strip() for p in (out.stdout or "").strip().split("\t")]
    if len(parts) != 3 or not all(parts):
        print("session_lineage: unexpected tmux display-message output (%r); "
              "spawning without parent env" % out.stdout, file=sys.stderr)
        return []
    if not parts[1].isdigit():
        print("session_lineage: tmux session_created was not numeric (%r); "
              "spawning without parent env" % parts[1], file=sys.stderr)
        return []

    name, created, pane_id = parts
    return ["-e", "%s=%s" % (ENV_SESSION, name),
            "-e", "%s=%s" % (ENV_CREATED, created),
            "-e", "%s=%s" % (ENV_PANE, pane_id)]


# --- child side: what was handed to us ------------------------------------------

def parent_from_env(env=None):
    """{"session", "created", "pane"} as handed by spawn_env_args, or None.

    None unless ENV_SESSION is non-empty: a session with no recorded parent
    (launched outside a lineage-aware launcher) is a root, not an error.
    """
    env = os.environ if env is None else env
    session = (env.get(ENV_SESSION) or "").strip()
    if not session:
        return None
    return {
        "session": session,
        "created": (env.get(ENV_CREATED) or "").strip(),
        "pane": (env.get(ENV_PANE) or "").strip(),
    }


# --- reader side ------------------------------------------------------------------

def live_sessions(run=None):
    """[{"name", "created"}] for every live tmux session, or None when tmux
    cannot be asked. None and [] differ on purpose: None means "no
    information" and makes parent_status say so instead of "exited"."""
    run = run or subprocess.run
    try:
        out = run(["tmux", "list-sessions", "-F", "#{session_name}\t#{session_created}"],
                  capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    sessions = []
    for line in (out.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[0].strip():
            sessions.append({"name": parts[0].strip(), "created": parts[1].strip()})
    return sessions


def parent_status(parent, sessions):
    """Liveness of a recorded parent: "live", "exited", "exited (name reused)",
    or "liveness unknown" when sessions is None (tmux could not be asked).

    sessions = [{"name", "created"}] for live tmux sessions. Matching is by
    (name, created) TOGETHER: tmux reuses session names (numbers restart at 0
    with the server), so a name alone could point a child at an unrelated
    later session. The one home for this rule; readers call it, never
    re-derive it."""
    if sessions is None:
        return "liveness unknown"
    name, created = parent.get("session"), _str(parent.get("created"))
    same_name = [s for s in sessions if s.get("name") == name]
    if any(_str(s.get("created")) == created for s in same_name):
        return "live"
    return "exited (name reused)" if same_name else "exited"


def parents_by_session(registry, panes):
    """{tmux session name: parent dict} from registry entries whose pane is
    live AND whose registered claude pid still matches the pane's pid.

    `panes` is session_peers.list_peers() output. The pid match is the same
    staleness rule find-session applies: tmux reuses pane ids, so an entry
    whose pane was restarted under a new pid describes a session that no
    longer exists. Lineage is per tmux session; the first verified entry with
    a parent wins (every pane of one session inherits the same vars)."""
    by_id = {p["pane_id"]: p for p in (panes or [])}
    out = {}
    for pane_id in sorted((registry or {}).keys()):
        entry = registry[pane_id]
        pane = by_id.get(pane_id)
        if pane is None or not isinstance(entry, dict):
            continue
        if str(entry.get("claude_pid") or "") != str(pane.get("pane_pid") or ""):
            continue
        parent = entry.get("parent")
        if isinstance(parent, dict) and (parent.get("session") or "").strip():
            out.setdefault(pane["tmux_session"], parent)
    return out


_NODES = object()  # build_tree's default: judge liveness against the nodes themselves


def build_tree(sessions, parents, live=_NODES):
    """[(depth, name, note_or_None), ...] in stable, printable order.

    sessions: [{"name", "created"}], the nodes to draw (e.g. the tmux
        sessions running Claude).
    parents: {child name: {"session", "created", "pane"}}.
    live: every live tmux session, for parent_status(); defaults to sessions.
        None means tmux could not be asked (every parent: liveness unknown).

    A child nests under its parent only when parent_status() says "live" and
    the parent is itself a node. Otherwise it is a root with a note:
      "parent X exited", "parent X exited (name reused)",
      "parent X liveness unknown", "parent X live, not a Claude session".
    Cycle-safe: a corrupt chain (A's parent is B, B's parent is A) can never
    recurse forever, and every session is still shown exactly once.
    """
    sessions = [s for s in (sessions or []) if s.get("name")]
    if live is _NODES:
        live = sessions
    names = [s["name"] for s in sessions]
    node = set(names)

    parent_of = {}   # child -> parent name, only when the parent is a live node
    notes = {}       # child -> note, only when the child is (therefore) a root

    for name in names:
        pdict = (parents or {}).get(name)
        if not pdict:
            continue
        pname = (pdict.get("session") or "").strip()
        if not pname:
            continue
        status = parent_status(pdict, live)
        if pname == name:
            notes[name] = "parent %s is itself" % pname
        elif status != "live":
            notes[name] = "parent %s %s" % (pname, status)
        elif pname not in node:
            notes[name] = "parent %s live, not a Claude session" % pname
        else:
            parent_of[name] = pname

    children = {}
    for child, parent in parent_of.items():
        children.setdefault(parent, []).append(child)

    out = []
    emitted = set()

    def emit(name, depth, visiting):
        if name in visiting or name in emitted:
            return  # cycle guard: never recurse back through a node twice
        emitted.add(name)
        out.append((depth, name, notes.get(name)))
        for child in sorted(children.get(name, [])):
            emit(child, depth + 1, visiting | {name})

    for r in sorted(n for n in names if n not in parent_of):
        emit(r, 0, set())
    # A pure cycle has no root; still show each session once, in name order.
    for n in sorted(names):
        if n not in emitted:
            emit(n, 0, set())
    return out


# --- CLI ----------------------------------------------------------------------------

def cmd_me(args):
    parent = parent_from_env()
    if parent is None:
        print("no parent recorded (launched outside a lineage-aware launcher)")
        return 0
    print("parent: %s (%s)" % (parent["session"], parent_status(parent, live_sessions())))
    return 0


# --- selftest -----------------------------------------------------------------------

def selftest():
    fails = []

    def check(label, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            fails.append(label + ((": " + detail) if detail else ""))

    class R(object):
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    # --- spawn_env_args (its error paths print a note to stderr; keep the selftest quiet)
    import contextlib
    import io
    quiet = contextlib.redirect_stderr(io.StringIO())
    quiet.__enter__()
    env_tmux = {"TMUX": "/tmp/sock,1,0", "TMUX_PANE": "%1"}

    def versioned(v):
        return lambda c, **k: R(0, v + "\n") if c[:2] == ["tmux", "-V"] else R(0, "work\t1700000000\t%2\n")
    check("spawn_env_args is [] on tmux older than 3.2 (no new-session -e)",
          spawn_env_args(env_tmux, run=versioned("tmux 3.0a")) == [])
    check("spawn_env_args works on tmux 3.2 and later",
          len(spawn_env_args(env_tmux, run=versioned("tmux 3.6b"))) == 6)
    check("spawn_env_args carries on when the version is unreadable",
          len(spawn_env_args(env_tmux, run=versioned("tmux master"))) == 6)
    got = spawn_env_args(env_tmux, run=lambda c, **k: R(0, "work\t1700000000\t%2\n"))
    check("spawn_env_args returns the 6-element -e list inside tmux",
          got == ["-e", ENV_SESSION + "=work", "-e", ENV_CREATED + "=1700000000",
                  "-e", ENV_PANE + "=%2"], repr(got))
    ok_run = lambda c, **k: R(0, "work\t1700000000\t%2\n")
    check("spawn_env_args is [] with no TMUX", spawn_env_args({}, run=ok_run) == [])
    check("spawn_env_args is [] with TMUX but no TMUX_PANE",
          spawn_env_args({"TMUX": "x"}, run=ok_run) == [])
    check("spawn_env_args is [] when tmux exits nonzero",
          spawn_env_args(env_tmux, run=lambda c, **k: R(1, "")) == [])

    def raises(c, **k):
        raise OSError("tmux not found")
    check("spawn_env_args is [] when run() raises", spawn_env_args(env_tmux, run=raises) == [])
    check("spawn_env_args is [] on malformed output",
          spawn_env_args(env_tmux, run=lambda c, **k: R(0, "one-field\n")) == [])
    check("spawn_env_args is [] when session_created is not numeric",
          spawn_env_args(env_tmux, run=lambda c, **k: R(0, "w\tnope\t%2\n")) == [])

    # --- parent_from_env
    check("parent_from_env is None with nothing handed down", parent_from_env({}) is None)
    full = {ENV_SESSION: "work", ENV_CREATED: "1700000000", ENV_PANE: "%2"}
    check("parent_from_env round-trips what spawn_env_args produced",
          parent_from_env(full) == {"session": "work", "created": "1700000000", "pane": "%2"})
    check("parent_from_env is None when the session field is whitespace",
          parent_from_env({ENV_SESSION: "   "}) is None)
    check("env var names carry no host-specific prefix",
          all(v.startswith("SESSION_NOTES_PARENT_") for v in (ENV_SESSION, ENV_CREATED, ENV_PANE)))

    # --- registry_path / read_registry
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="lineage_")
    try:
        cfg = {"paths": {"state_dir": os.path.join(tmp, "st")}}
        check("registry lives at <state_dir>/session_panes.json",
              registry_path(cfg) == os.path.join(tmp, "st", "session_panes.json"))
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{corrupt")
        check("corrupt registry reads as {}", read_registry(bad) == {})
        check("missing registry reads as {}", read_registry(os.path.join(tmp, "no.json")) == {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- live_sessions
    check("live_sessions parses list-sessions",
          live_sessions(run=lambda c, **k: R(0, "a\t1\nb\t2\n"))
          == [{"name": "a", "created": "1"}, {"name": "b", "created": "2"}])
    check("live_sessions is None when tmux fails",
          live_sessions(run=lambda c, **k: R(1, "")) is None)
    check("live_sessions is None when run() raises", live_sessions(run=raises) is None)

    # --- parent_status
    par = {"session": "a", "created": "100", "pane": "%1"}
    check("parent_status: live when name+created match",
          parent_status(par, [{"name": "a", "created": "100"}]) == "live")
    check("parent_status: name reused when created differs",
          parent_status(par, [{"name": "a", "created": "200"}]) == "exited (name reused)")
    check("parent_status: exited when name absent",
          parent_status(par, [{"name": "b", "created": "100"}]) == "exited")
    check("parent_status: int and str created compare equal",
          parent_status(par, [{"name": "a", "created": 100}]) == "live")
    check("parent_status: unknown when tmux could not be asked",
          parent_status(par, None) == "liveness unknown")

    # --- parents_by_session
    reg = {"%1": {"claude_pid": "11", "parent": par},
           "%2": {"claude_pid": "99", "parent": {"session": "x", "created": "1"}},
           "%3": {"claude_pid": "33"},
           "%4": {"claude_pid": "44", "parent": {"session": "y", "created": "1"}}}
    panes = [{"pane_id": "%1", "pane_pid": "11", "tmux_session": "s1"},
             {"pane_id": "%2", "pane_pid": "22", "tmux_session": "s2"},
             {"pane_id": "%3", "pane_pid": "33", "tmux_session": "s3"}]
    pbs = parents_by_session(reg, panes)
    check("parents_by_session keeps a pid-verified entry", pbs.get("s1") == par, repr(pbs))
    check("parents_by_session drops a stale pid (pane restarted)", "s2" not in pbs, repr(pbs))
    check("parents_by_session: no parent field -> absent", "s3" not in pbs, repr(pbs))
    check("parents_by_session drops an entry whose pane is gone", len(pbs) == 1, repr(pbs))

    # --- build_tree
    sessions = [{"name": "A", "created": 1}, {"name": "B", "created": 2},
                {"name": "C", "created": 3}]
    parents = {"B": {"session": "A", "created": "1"}, "C": {"session": "B", "created": "2"}}
    tree = build_tree(sessions, parents)
    check("three generations nest parent -> child -> grandchild",
          tree == [(0, "A", None), (1, "B", None), (2, "C", None)], repr(tree))
    tree = build_tree([{"name": "A", "created": 1}, {"name": "B", "created": 2}],
                      {"B": {"session": "ghost", "created": "9"}})
    check("a parent that is not live leaves the child a root with a note",
          tree == [(0, "A", None), (0, "B", "parent ghost exited")], repr(tree))
    tree = build_tree([{"name": "A", "created": 999}, {"name": "B", "created": 2}],
                      {"B": {"session": "A", "created": "1"}})
    check("a live name whose created time differs reads as name reused",
          tree == [(0, "A", None), (0, "B", "parent A exited (name reused)")], repr(tree))
    tree = build_tree([{"name": "B", "created": 2}], {"B": {"session": "sh", "created": "5"}},
                      live=[{"name": "B", "created": 2}, {"name": "sh", "created": 5}])
    check("a live parent that is not a node is noted, not nested",
          tree == [(0, "B", "parent sh live, not a Claude session")], repr(tree))
    tree = build_tree([{"name": "A", "created": 1}, {"name": "B", "created": 2}],
                      {"B": {"session": "A", "created": "1"}}, live=None)
    check("live=None (tmux unreachable) never nests, says liveness unknown",
          tree == [(0, "A", None), (0, "B", "parent A liveness unknown")], repr(tree))
    quiet.__exit__(None, None, None)
    selfp = build_tree([{"name": "A", "created": 1}], {"A": {"session": "A", "created": 1}})
    check("a session recorded as its own parent is labelled, not a plain root",
          selfp == [(0, "A", "parent A is itself")], repr(selfp))
    check("a session with no recorded parent is a bare root",
          build_tree([{"name": "A", "created": 1}], {}) == [(0, "A", None)])
    tree = build_tree([{"name": "A", "created": 1}, {"name": "B", "created": 2}],
                      {"A": {"session": "B", "created": "2"}, "B": {"session": "A", "created": "1"}})
    check("a parent cycle terminates and shows each session once",
          sorted(n for _, n, _ in tree) == ["A", "B"], repr(tree))
    tree = build_tree([{"name": "z", "created": 1}, {"name": "a", "created": 2},
                       {"name": "zc", "created": 3}, {"name": "ac", "created": 4}],
                      {"zc": {"session": "z", "created": "1"}, "ac": {"session": "a", "created": "2"}})
    check("roots sort by name and each root's children follow it",
          tree == [(0, "a", None), (1, "ac", None), (0, "z", None), (1, "zc", None)], repr(tree))

    print("\n%s" % ("FAILED: " + "; ".join(fails) if fails else "all checks passed"))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("me", help="this session's handed-down parent, and whether it is still live")
    s.set_defaults(fn=cmd_me)
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
