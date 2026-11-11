#!/usr/bin/env python3
"""reflect_lock.py -- serialize /reflect across parallel Claude Code sessions.

The problem this solves: two sessions running /rnr at once each PEEK the other, each
sees "mid-/rnr", each emits REFLECT: CAUTION and parks waiting for the other, a mutual
standoff the user has to break by hand. A screen-peek is a racy snapshot and cannot
arbitrate; an atomic lock file can. Acquisition (O_CREAT|O_EXCL) decides turn order: of
two simultaneous acquirers, one goes first and the other automatically waits its turn.
Simple turn-taking, no negotiation.

Contract (wired into the rnr / reflect skills):
  - /reflect (and /rnr's reflect step) ACQUIRES before writing any memory/RESUME state
    and RELEASES as its last step. The lock represents *actually reflecting*, not
    refreshing, so peeks stay advisory context only.
  - On contention, WAIT (poll): never ask the user "wait or go?", never clobber.
  - A stale lock (holder crashed / never released) is broken LOUDLY after STALE_SECS.

Commands:
  acquire [--session NAME]   0 = acquired (or already yours); 1 = held by another
  release [--force]          0 = released / not held; 1 = held by another (no --force)
  status                     prints holder + age; 0 = free, 1 = held
  wait    [--timeout SECS]   poll until acquired; 0 = acquired, 2 = timeout
                             (run backgrounded: never foreground-block the user's pane)
  selftest                   identity + stale-break contract, in a temp dir; 0 = OK
  (--selftest works too)

Lock file: <state_dir>/reflect.lock (state_dir from the config).
(JSON: id, session, pid, pid_source, ts).
One GLOBAL lock on purpose: reflects are short (minutes) and rare, and a scope session
often writes both its own store and the global store, so per-scope locks would miss that.

IDENTITY. "You" used to be the tmux session name, falling back to
`pid-{os.getppid()}@host` when $TMUX was unset. That fallback is not an identity: every
agent Bash tool call is its own process, so a model-driven acquire and the release a few
tool calls later ran under DIFFERENT parent pids and release ALWAYS refused ("held by
pid-29950, not you"). The lock then leaked until the 10-minute stale-break, silently
turning "serialize parallel reflects" into "block every sibling reflect for 10 minutes
after each one". The same mismatch made a second acquire from one logical run read its
OWN lock as a stranger's. Both vanished under tmux, which is why they survived: the
broken path was the unwatched one (headless scheduled jobs, cloud sessions, an SSH shell
with no tmux).

So the match key is CLAUDE_CODE_SESSION_ID, the same explicit-registration move the
pane registry hook makes, and stable across every tool call of one session, in tmux or
out. Three deliberate properties:

  - `id` decides ownership; `session` is only a HUMAN LABEL (tmux name when there is
    one, so a refusal still reads "held by alpha"). Two Claude sessions sharing one
    tmux session are correctly two holders, which a name-keyed match gets wrong.
  - A lock with no `id` (written by an older version) falls back to comparing the
    `session` label, so an in-flight lock survives an upgrade.
  - `pid` is a LIVENESS HINT ONLY, never the identity. It records CLAUDE_PID (the
    long-lived claude process) when Claude Code exports it, and `pid_source` says
    which. The time-based stale-break remains the safety floor; a provably-dead pid may
    break the lock EARLIER, but only when pid_source == "claude": the getppid() fallback
    names an already-exited shell, so trusting it would break live holders' locks
    instantly. Only pid ABSENCE is trusted, so a recycled pid reads as alive and falls
    back to the time floor.
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import notes_config  # noqa: E402

# None = resolve from the config on first use (see lock_path()). The selftest
# repoints it at a temp dir.
LOCK = None
STALE_SECS = 10 * 60  # a reflect should never take this long; beyond it = crashed holder
POLL_SECS = 5


def lock_path():
    """The lock file: LOCK if set, else <state_dir>/reflect.lock from the config."""
    global LOCK
    if LOCK is None:
        LOCK = notes_config.state_path("reflect.lock")
    return LOCK


def _tmux_name():
    """The current tmux session's name, or None. Label only, never an identity."""
    if not os.environ.get("TMUX"):
        return None
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def my_identity():
    """(id, label, pid, pid_source) for this caller.

    `id` is what ownership is decided on and MUST be stable across the separate
    processes one logical run spans; `label` is what a human reads in a refusal.
    """
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    name = _tmux_name()
    host = os.uname().nodename

    if sid:
        ident = f"sid-{sid}"
        label = name or f"sid-{sid[:8]}@{host}"
    elif name:
        # No session id (a plain shell inside tmux). The tmux name is stable across
        # this run's processes, so it is a usable identity here even though it cannot
        # tell two Claude sessions in one tmux session apart.
        ident = name
        label = name
    else:
        # Last resort, and the shape the old code always used off-tmux: NOT stable
        # across tool calls. Kept so a bare shell still gets turn-taking within one
        # process, and recorded as such so the liveness hint is not trusted.
        ident = f"pid-{os.getppid()}@{host}"
        label = ident

    claude_pid = (os.environ.get("CLAUDE_PID") or "").strip()
    if claude_pid.isdigit():
        return ident, label, int(claude_pid), "claude"
    return ident, label, os.getppid(), "shell"


def my_session():
    """Back-compat shim: the human label for this caller."""
    return my_identity()[1]


def _pid_alive(pid):
    """True if the pid is running, False if provably not, None if unknowable.

    None and False are deliberately different: only a definite NO may shorten a
    stale-break, so an EPERM (a live process we do not own) must never read as dead.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def holder_is_me(holder, ident, label):
    """Does this lock record belong to the caller?

    Matches on `id`. A record with no `id` predates id-keyed ownership, so it is
    matched on its `session` label instead: an in-flight lock must survive the upgrade rather
    than become unreleasable by the very session holding it.
    """
    if not holder:
        return False
    hid = holder.get("id")
    if hid:
        return hid == ident
    return holder.get("session") == label


def read_lock():
    """Return (holder_dict, age_secs) or (None, None) if free/unreadable-gone."""
    try:
        with open(lock_path()) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, None
    except Exception:
        data = {"session": "<unreadable>", "ts": None}
    try:
        age = time.time() - os.stat(lock_path()).st_mtime
    except FileNotFoundError:
        return None, None
    return data, age


def break_reason(holder, age):
    """Why this lock may be broken, or None to leave it alone.

    Two independent grounds, and the TIME one is the floor that has always made
    breaking defensible:
      - age > STALE_SECS: a reflect never takes this long, so the holder crashed.
      - the holder recorded its own claude pid and that process is provably gone.
        Only trusted for pid_source == "claude"; the "shell" fallback names the
        already-exited shell of a single tool call, so it reads dead immediately and
        would break every live holder's lock.
    """
    if age is not None and age > STALE_SECS:
        return f"held for {int(age // 60)}m (> {STALE_SECS // 60}m): holder presumed dead"
    if holder.get("pid_source") == "claude":
        pid = holder.get("pid")
        if isinstance(pid, int) and _pid_alive(pid) is False:
            return f"holder process (pid {pid}) is gone"
    return None


BREAK_MUTEX_STALE_SECS = 60  # a break takes microseconds; an older mutex is a crashed breaker's


def _break_stale(holder):
    """Break the lock we just judged stale. Return True if the lock is now free to create.

    The live lock file is NEVER moved or renamed: any window in which LOCK does not exist
    lets a third acquirer O_EXCL-create it while a breaker still holds (and may then discard)
    a live record. Breakers are instead serialized by an flock on a PERMANENT file,
    LOCK + ".breaking" (never unlinked, so there is no stale-mutex cleanup to race on, and a
    crashed breaker's lock dies with its process). While holding it, the breaker re-reads LOCK
    and unlinks it only if the record is still the stale one it judged; if the record changed
    (someone already broke it and took the lock), it backs off and the caller reports
    contention. Creating the lock stays a plain O_EXCL.
    """
    lock = lock_path()
    fd = os.open(lock + ".breaking", os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False  # another breaker is working: contention
    try:
        cur, cur_age = read_lock()
        if cur is None:
            return True  # already gone: just create
        if cur != holder:
            return False  # replaced since we judged it: not ours to break
        why = break_reason(cur, cur_age)
        if why is None:
            return False
        print(
            f"WARNING: BREAKING STALE LOCK: held by {cur.get('session')}: {why}.",
            file=sys.stderr,
        )
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass
        return True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def try_acquire(ident, label, pid, pid_source):
    """Return 'acquired' | 'mine' | holder-string."""
    holder, age = read_lock()
    if holder is not None:
        if holder_is_me(holder, ident, label):
            return "mine"
        why = break_reason(holder, age)
        if why:
            if not _break_stale(holder):
                # Lost the break race (or it was fresh after all): re-read.
                holder, age = read_lock()
                if holder is not None:
                    return f"{holder.get('session')} ({int(age or 0)}s)"
        else:
            return f"{holder.get('session')} ({int(age or 0)}s)"
    lock = lock_path()
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder, age = read_lock()
        who = holder.get("session") if holder else "<unknown>"
        return f"{who} ({int(age or 0)}s)"
    with os.fdopen(fd, "w") as fh:
        json.dump(
            {
                "id": ident,
                "session": label,
                "pid": pid,
                "pid_source": pid_source,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            fh,
        )
    return "acquired"



def _selftest():
    """Prove the identity contract and the stale-break, in a temp dir.

    Never touches the real lock: LOCK is repointed at a TemporaryDirectory for the
    duration and restored afterwards. Every case here was observed RED against the
    older pid-keyed code; the two "must NOT break" cases are the ones that keep the pid
    liveness hint from clobbering live holders' locks.
    """
    import tempfile
    global LOCK
    real, fails = LOCK, []

    def check(name, got, want):
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")
        if not ok:
            fails.append(name)

    def put(rec, age_s=0):
        with open(LOCK, "w") as fh:
            json.dump(rec, fh)
        if age_s:
            st = os.stat(LOCK)
            os.utime(LOCK, (st.st_atime, time.time() - age_s))

    dead = subprocess.Popen(["true"])
    dead.wait()
    dead_pid = dead.pid

    try:
        with tempfile.TemporaryDirectory() as d:
            LOCK = os.path.join(d, ".reflect.lock")
            # A LIVE pid: this fixture's holder must read as alive, or the early
            # break fires and the "stranger is refused" case passes for the wrong
            # reason (observed while writing this: the first fixture used a made-up
            # pid, which is indistinguishable from a crashed holder).
            live = os.getpid()
            me, mylabel = "sid-alpha", "alpha"
            other = ("sid-beta", "beta")

            # The core bug: acquire and release run in different processes. Identity
            # is passed explicitly here, which is exactly what a stable id buys.
            check("acquire", try_acquire(me, mylabel, live, "claude"), "acquired")
            check("re-acquire is mine", try_acquire(me, mylabel, live, "claude"), "mine")
            holder, _ = read_lock()
            check("owner releases", holder_is_me(holder, me, mylabel), True)
            check("a stranger does not", holder_is_me(holder, *other), False)
            check("stranger is refused", try_acquire(*other, live, "claude").startswith("alpha"), True)
            os.unlink(LOCK)

            # An older-format record has no `id` and must stay releasable by its label.
            put({"session": mylabel, "ts": "old-format"})
            holder, _ = read_lock()
            check("legacy record matches on label", holder_is_me(holder, me, mylabel), True)
            check("legacy record rejects a stranger", holder_is_me(holder, *other), False)
            os.unlink(LOCK)

            # The stale-break, with a session id as the owner rather than a pid.
            base = {"id": "sid-ghost", "session": "ghost", "pid": dead_pid}
            put({**base, "pid_source": "shell"}, age_s=STALE_SECS + 60)
            check("time floor breaks an id-owned lock", bool(break_reason(*read_lock())), True)
            put({**base, "pid_source": "shell"})
            check("fresh + shell pid: must NOT break", break_reason(*read_lock()), None)
            put({**base, "pid_source": "claude"})
            check("fresh + dead claude pid: breaks early", bool(break_reason(*read_lock())), True)
            put({**base, "pid": os.getpid(), "pid_source": "claude"})
            check("fresh + live claude pid: must NOT break", break_reason(*read_lock()), None)

            # Two acquirers judge the SAME stale lock; the first finishes (breaks it and
            # holds a fresh one) before the second acts on its judgment. Exactly one may
            # win. Old unlink-based break: the second unlinks the fresh lock, both win.
            import threading
            put({"id": "sid-ghost", "session": "ghost", "pid": dead_pid,
                 "pid_source": "shell"}, age_s=STALE_SECS + 60)
            real_br = break_reason
            barrier = threading.Barrier(2)
            first_done = threading.Event()
            seen = threading.local()
            results = {}

            def gated_break_reason(h, a):
                r = real_br(h, a)
                if r and not getattr(seen, "gated", False):
                    seen.gated = True
                    barrier.wait(5)
                    if threading.current_thread().name == "B":
                        first_done.wait(5)
                return r

            def racer(name):
                results[name] = try_acquire("sid-" + name, name, live, "claude")
                if name == "A":
                    first_done.set()

            globals()["break_reason"] = gated_break_reason
            try:
                ts = [threading.Thread(target=racer, args=(n,), name=n) for n in "AB"]
                for th in ts:
                    th.start()
                for th in ts:
                    th.join(15)
            finally:
                globals()["break_reason"] = real_br
            check("stale-break race: exactly one acquires",
                  sorted(1 if v == "acquired" else 0 for v in results.values()), [0, 1])
            check("no side-file leftovers (the permanent .breaking mutex is allowed)",
                  [n for n in os.listdir(d) if n not in (".reflect.lock", ".reflect.lock.breaking")], [])
            os.unlink(LOCK)

            # Three parties: A and B both judge the stale lock; A breaks and takes it; then
            # B's break runs and, in its first filesystem step, C polls. Whatever B does with
            # the lock in that window, exactly one party may hold it and the lock must name
            # that party. (A rename-aside break fails this: LOCK is absent mid-break, C
            # creates it, and A's live record is lost.)
            put({"id": "sid-ghost", "session": "ghost", "pid": dead_pid,
                 "pid_source": "shell"}, age_s=STALE_SECS + 60)
            barrier3 = threading.Barrier(2)
            a_done = threading.Event()
            gate_open = threading.Event()
            seen3 = threading.local()
            res3 = {}

            def gated3(h, a):
                r = real_br(h, a)
                if r and not getattr(seen3, "gated", False):
                    seen3.gated = True
                    barrier3.wait(5)
                    if threading.current_thread().name == "B":
                        a_done.wait(5)
                        gate_open.set()
                return r

            real_open3, real_rename3 = os.open, os.rename
            fired = []

            def after_b_first_step(fn):
                def wrapped(*args, **kw):
                    out = fn(*args, **kw)
                    if (threading.current_thread().name == "B" and gate_open.is_set()
                            and not fired):
                        fired.append(1)
                        res3["C"] = try_acquire("sid-C", "C", live, "claude")
                    return out
                return wrapped

            def racer3(name):
                res3[name] = try_acquire("sid-" + name, name, live, "claude")
                if name == "A":
                    a_done.set()

            globals()["break_reason"] = gated3
            os.open, os.rename = after_b_first_step(real_open3), after_b_first_step(real_rename3)
            try:
                ts3 = [threading.Thread(target=racer3, args=(n,), name=n) for n in "AB"]
                for th in ts3:
                    th.start()
                for th in ts3:
                    th.join(15)
            finally:
                globals()["break_reason"] = real_br
                os.open, os.rename = real_open3, real_rename3
            winners = sorted(k for k, v in res3.items() if v == "acquired")
            holder3, _ = read_lock()
            check("three-party break: exactly one holder",
                  (len(winners) == 1 and "C" in res3), True)
            check("three-party break: the lock names the one holder",
                  len(winners) == 1 and holder3 is not None and holder3.get("session") == winners[0], True)
            os.unlink(LOCK)

            # Mutex race: X holds the break mutex (stopped inside its critical section) while
            # Y, which judged the same stale lock, attempts its own break. Y must not get in:
            # exactly one party acquires and the lock names it.
            put({"id": "sid-ghost", "session": "ghost", "pid": dead_pid,
                 "pid_source": "shell"}, age_s=STALE_SECS + 60)
            barrier4 = threading.Barrier(2)
            x_holds, y_done = threading.Event(), threading.Event()
            seen4, x_reads, res4 = threading.local(), [], {}
            real_read4 = read_lock

            def gated4(h, a):
                r = real_br(h, a)
                if r and not getattr(seen4, "gated", False):
                    seen4.gated = True
                    barrier4.wait(5)
                    if threading.current_thread().name == "Y":
                        x_holds.wait(3)
                return r

            def read4():
                out = real_read4()
                if threading.current_thread().name == "X":
                    x_reads.append(1)
                    if len(x_reads) == 2:  # the re-read made while holding the mutex
                        x_holds.set()
                        y_done.wait(3)
                return out

            def racer4(name):
                res4[name] = try_acquire("sid-" + name, name, live, "claude")
                if name == "Y":
                    y_done.set()

            globals()["break_reason"], globals()["read_lock"] = gated4, read4
            try:
                ts4 = [threading.Thread(target=racer4, args=(n,), name=n) for n in "XY"]
                for th in ts4:
                    th.start()
                for th in ts4:
                    th.join(15)
            finally:
                globals()["break_reason"], globals()["read_lock"] = real_br, real_read4
            win4 = sorted(k for k, v in res4.items() if v == "acquired")
            holder4, _ = real_read4()
            check("mutex race: exactly one acquires",
                  len(win4) == 1 and len(res4) == 2, True)
            check("mutex race: the lock names the one holder",
                  len(win4) == 1 and holder4 is not None and holder4.get("session") == win4[0], True)
            os.unlink(LOCK)
    finally:
        LOCK = real

    print("SELFTEST " + ("OK" if not fails else f"FAILED: {fails}"))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    if "--selftest" in sys.argv[1:]:
        return _selftest()
    ap.add_argument("cmd", choices=["acquire", "release", "status", "wait", "selftest"])
    ap.add_argument("--session", default=None)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.cmd == "selftest":
        return _selftest()
    ident, label, pid, pid_source = my_identity()
    if args.session:
        # An explicit --session names BOTH, so a caller that passes one keeps the
        # name-keyed contract: what it acquires under, it releases under.
        ident = label = args.session

    if args.cmd == "acquire":
        r = try_acquire(ident, label, pid, pid_source)
        if r == "acquired":
            print(f"acquired by {label}")
            return 0
        if r == "mine":
            print(f"already held by you ({label})")
            return 0
        print(f"held by {r}: wait (reflect_lock.py wait), don't clobber")
        return 1

    if args.cmd == "release":
        holder, _ = read_lock()
        if holder is None:
            print("not held")
            return 0
        if not holder_is_me(holder, ident, label) and not args.force:
            print(f"held by {holder.get('session')}, not you ({label}): refusing (use --force)")
            return 1
        try:
            os.unlink(lock_path())
        except FileNotFoundError:
            pass
        print(f"released ({holder.get('session')})")
        return 0

    if args.cmd == "status":
        holder, age = read_lock()
        if holder is None:
            print("free")
            return 0
        why = break_reason(holder, age)
        stale = f" STALE ({why})" if why else ""
        mine = " (that is you)" if holder_is_me(holder, ident, label) else ""
        print(f"held by {holder.get('session')} for {int(age or 0)}s{stale}{mine}")
        return 1

    if args.cmd == "wait":
        deadline = time.time() + args.timeout
        while True:
            r = try_acquire(ident, label, pid, pid_source)
            if r in ("acquired", "mine"):
                print(f"acquired by {label}")
                return 0
            if time.time() >= deadline:
                print(f"timeout after {args.timeout}s: still held by {r}", file=sys.stderr)
                return 2
            time.sleep(POLL_SECS)

    return 0


if __name__ == "__main__":
    sys.exit(main())
