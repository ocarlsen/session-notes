#!/usr/bin/env python3
"""upstream_drift.py -- what changed upstream that this repo has not taken yet.

session-notes was extracted from a working Claude Code config (a ~/.claude kept in git).
Fixes keep landing there, and nothing carries them here unless someone remembers. This tool
lists, for every file the two share (tools/upstream.toml), the upstream commits since the
last decision about that file. A decision is one of two marks, and every upstream change has
to end in one of them:

  ported      the change (or its equivalent) is in this repo
  local-only  the change is specific to that one setup and does not belong here

An upstream commit whose message says it is itself a port OF a session-notes PR (the change
originated here, then got carried back upstream) is auto-closed: it does not count as pending
and does not need a `mark`. It is still listed, because a port commit can carry an extra
upstream-only change alongside the port, and that extra change needs the usual decision.

The per-file watermark (the last upstream commit decided) is private state, kept outside the
repo: ~/.config/session-notes/upstream-state.json, or $SESSION_NOTES_UPSTREAM_STATE.
Upstream commit ids and messages are never written into this repo.

    upstream_drift.py seed --upstream ~/.claude --at <rev> [--force]   start every watermark at <rev>
    upstream_drift.py check [--json]                         list pending upstream commits
    upstream_drift.py mark FILE ported|local-only [--through REV] [--note TEXT] [--reset]

--reset is for an upstream whose history was rewritten (rebase, force-push): the old
watermark is no longer in its history, so the usual "only move forward" check is skipped.
seed refuses to overwrite existing decisions without --force.
    upstream_drift.py --selftest

check exits 0 when nothing is pending and every repo file is mapped, 1 otherwise, and 2
on a setup error (no state, missing upstream). It never runs in CI, which has no upstream.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import notes_config  # noqa: E402

TRACKED_DIRS = ("bin", "hooks", "skills")
DECISIONS = ("ported", "local-only")

# "support of session-notes #3" contains the literal substring "port of" (supPORT OF) -- the
# leading \b keeps that from matching, since there is no word boundary before "port" there.
# The owner prefix (as in "someuser/session-notes") is a generic token, not this repo's own
# name, so a fork can run this unmodified and so this file's own leak scan stays clean.
AUTOCLOSE_RE = re.compile(
    r"\bport of\s+(?:[\w.-]+/)?session-notes(?:-dev)?\s*(?:pr)?\s*#(\d+)", re.IGNORECASE)


class SetupError(Exception):
    pass


def state_path():
    return os.path.expanduser(os.environ.get(
        "SESSION_NOTES_UPSTREAM_STATE", "~/.config/session-notes/upstream-state.json"))


def git(repo, *args):
    """git in `repo`, with every GIT_ variable stripped: an inherited GIT_DIR (from a hook)
    would otherwise override -C and point the call at another repo."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise SetupError("git %s in %s: %s" % (" ".join(args), repo, r.stderr.strip()))
    return r.stdout


def git_rc(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env).returncode


def is_ancestor(repo, old, new):
    """True / False, or raise SetupError when git cannot answer (an unknown commit id)."""
    rc = git_rc(repo, "merge-base", "--is-ancestor", old, new)
    if rc in (0, 1):
        return rc == 0
    raise SetupError("cannot compare %s and %s in %s (unknown commit?)" % (old[:9], new[:9], repo))


def load_map(root=None, path=None):
    with open(path or os.path.join(root or ROOT, "tools", "upstream.toml")) as fh:
        data = notes_config.parse_toml(fh.read())
    pairs = {p["repo"]: p["upstream"] for p in data.get("pair", [])}
    return pairs, set(data.get("repo_only", []))


def tracked_files(root=None):
    """Every .py and SKILL.md under the tracked dirs, relative to root."""
    root = root or ROOT
    out = []
    for top in TRACKED_DIRS:
        for dirpath, dirnames, files in os.walk(os.path.join(root, top)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for f in files:
                if f.endswith(".py") or f == "SKILL.md":
                    out.append(os.path.relpath(os.path.join(dirpath, f), root))
    return sorted(out)


def load_state(path=None):
    path = path or state_path()
    if not os.path.exists(path):
        raise SetupError("no state at %s: run `upstream_drift.py seed --upstream DIR --at REV` first" % path)
    with open(path) as fh:
        return json.load(fh)


def save_state(state, path=None):
    path = path or state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from the start
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def pending(state, pairs, problems=None):
    """Three maps of upstream commits after each watermark, keyed by repo_file: commits still
    needing a decision as {file: [(sha, date, subject), ...]}; commits auto-closed because the
    message itself says it is a port of a session-notes PR, as {file: [(sha, date, subject,
    ref), ...]} (ref like "#15"); and {file: sha} of the newest commit in either list (upstream
    `git log` order is newest first). Map problems that need a person (a pair never decided, an
    upstream path that no longer exists, a watermark gone from upstream history) are appended
    to `problems` when given, else raised."""
    up = os.path.expanduser(state["upstream"])
    if not os.path.isdir(up):
        raise SetupError("upstream %s does not exist" % up)
    todo, closed, newest = {}, {}, {}

    def problem(msg):
        if problems is None:
            raise SetupError(msg)
        problems.append(msg)

    for h, u in sorted(pairs.items()):
        mark = state["files"].get(h)
        if not mark:
            problem("%s: no decision yet (new pair): `mark %s ported|local-only`" % (h, h))
            continue
        if git_rc(up, "cat-file", "-e", "HEAD:" + u) != 0:
            problem("%s: upstream path %s no longer exists (renamed or deleted): fix tools/upstream.toml" % (h, u))
            continue
        if not is_ancestor(up, mark["through"], "HEAD"):
            problem("%s: watermark %s is not in upstream history (rebased or force-pushed?): "
                    "review, then `mark %s ... --reset`" % (h, mark["through"][:9], h))
            continue
        # %B (the full message, subject and body) carries the auto-close phrasing whether it
        # falls on the subject line or below it. \x1f/\x1e separate fields/records since a body
        # can hold anything, including tabs and blank lines. Each record STARTS with \x1e and
        # a full sha, and the split requires both, so a stray \x1e inside a body stays part of
        # that body instead of breaking the parse.
        log = git(up, "log", "--format=%x1e%H%x1f%ad%x1f%s%x1f%B", "--date=short",
                  "%s..HEAD" % mark["through"], "--", u)
        todo_rows, closed_rows = [], []
        for rec in re.split(r"\x1e(?=[0-9a-f]{40}\x1f)", log):
            if not rec.strip():
                continue
            sha, date, subject, body = rec.split("\x1f", 3)
            sha = sha.strip()
            if h not in newest:
                newest[h] = sha  # first record seen is the newest (git log is newest-first)
            m = AUTOCLOSE_RE.search(body)
            if m:
                closed_rows.append((sha, date, subject, "#" + m.group(1)))
            else:
                todo_rows.append((sha, date, subject))
        if todo_rows:
            todo[h] = todo_rows
        if closed_rows:
            closed[h] = closed_rows
    return todo, closed, newest


def unmapped(pairs, repo_only, root=None):
    known = set(pairs) | repo_only
    return [f for f in tracked_files(root) if f not in known]


def cmd_seed(args):
    if os.path.exists(state_path()) and not args.force:
        raise SetupError("%s exists; seeding would replace every decision in it. Use --force to do that."
                         % state_path())
    up = os.path.expanduser(args.upstream)
    sha = git(up, "rev-parse", "--verify", args.at + "^{commit}").strip()
    pairs, _ = load_map()
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state = {"upstream": args.upstream, "files": {
        h: {"through": sha, "how": "seed", "at": now, "note": "extraction point"} for h in pairs}}
    save_state(state)
    print("seeded %d files at %s" % (len(pairs), sha[:9]))
    return 0


def cmd_check(args):
    pairs, only = load_map()
    state = load_state()
    problems = []
    todo, closed, newest = pending(state, pairs, problems)
    loose = unmapped(pairs, only)
    count = sum(len(v) for v in todo.values())
    if args.json:
        print(json.dumps({"pending": count, "files": {h: [{"sha": s, "date": d, "subject": m}
                          for s, d, m in rows] for h, rows in todo.items()},
                          "auto_closed": {h: [{"sha": s, "date": d, "subject": m, "ref": r}
                          for s, d, m, r in rows] for h, rows in closed.items()},
                          "unmapped": loose, "problems": problems}, indent=2))
    else:
        if not todo and not loose and not problems:
            print("upstream_drift: nothing pending")
        for p in problems:
            print("PROBLEM " + p)
        for h, rows in todo.items():
            print("%s  (%d upstream change%s)" % (h, len(rows), "" if len(rows) == 1 else "s"))
            for s, d, m in rows:
                print("    %s %s %s" % (s[:9], d, m))
        for h, rows in closed.items():
            print("%s  (%d auto-closed: ports of session-notes PRs)" % (h, len(rows)))
            for s, d, m, r in rows:
                print("    %s %s %s  [port of %s]" % (s[:9], d, m, r))
        for f in loose:
            print("%s  UNMAPPED: add it to tools/upstream.toml as a pair or repo_only" % f)
        if closed:
            print("\nAuto-closed commits came from this repo; check none carries an extra "
                  "upstream-only change, then mark the file.")
        todo_or_closed = sorted(set(todo) | set(closed))
        if todo_or_closed:
            print("\nthen record the decision (after the port merges, or to decide local-only):")
            for h in todo_or_closed:
                print("  upstream_drift.py mark %s ported|local-only --through %s" % (h, newest[h][:9]))
    return 1 if (todo or loose or problems) else 0


def cmd_mark(args):
    pairs, _ = load_map()
    if args.file not in pairs:
        raise SetupError("%s is not a shared file in tools/upstream.toml" % args.file)
    state = load_state()
    up = os.path.expanduser(state["upstream"])
    through = git(up, "rev-parse", "--verify", (args.through or "HEAD") + "^{commit}").strip()
    prev = state["files"].get(args.file)
    closes = ""
    if prev and not args.reset:
        # The new watermark must be at or after the old one, or decisions would be undone.
        if not is_ancestor(up, prev["through"], through):
            raise SetupError("%s is not after the current watermark %s (upstream rewritten? see --reset)"
                             % (through[:9], prev["through"][:9]))
        n = len([l for l in git(up, "log", "--format=%H", "%s..%s" % (prev["through"], through),
                                "--", pairs[args.file]).splitlines() if l.strip()])
        closes = ", closing %d upstream change%s" % (n, "" if n == 1 else "s")
    state["files"][args.file] = {"through": through, "how": args.decision,
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "note": args.note or ""}
    save_state(state)
    print("%s: %s through %s%s" % (args.file, args.decision, through[:9],
                                     closes or (" (first decision)" if not prev else " (reset)")))
    return 0


def selftest():
    import shutil
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as d:
        up, repo = os.path.join(d, "up"), os.path.join(d, "repo")
        os.makedirs(os.path.join(up, "bin"))
        os.makedirs(os.path.join(repo, "bin"))
        os.makedirs(os.path.join(repo, "tools"))
        git(up, "init", "-q")
        git(up, "config", "user.email", "test@example.com")
        git(up, "config", "user.name", "Selftest")

        def commit(path, text, msg):
            with open(os.path.join(up, path), "w") as fh:
                fh.write(text)
            git(up, "add", path)
            git(up, "commit", "-q", "-m", msg)
            return git(up, "rev-parse", "HEAD").strip()

        base = commit("bin/a.py", "a1\n", "a: first")
        commit("bin/b.py", "b1\n", "b: first")
        for f in ("a.py", "b.py", "only.py"):
            with open(os.path.join(repo, "bin", f), "w") as fh:
                fh.write("x\n")
        with open(os.path.join(repo, "tools", "upstream.toml"), "w") as fh:
            fh.write('repo_only = ["bin/only.py"]\n[[pair]]\nrepo = "bin/a.py"\nupstream = "bin/a.py"\n'
                     '[[pair]]\nrepo = "bin/b.py"\nupstream = "bin/b.py"\n')
        pairs, only = load_map(repo)
        check("map parsed", pairs == {"bin/a.py": "bin/a.py", "bin/b.py": "bin/b.py"} and only == {"bin/only.py"})
        check("nothing unmapped", unmapped(pairs, only, repo) == [])

        head = git(up, "rev-parse", "HEAD").strip()
        state = {"upstream": up, "files": {h: {"through": head, "how": "seed"} for h in pairs}}
        check("clean after seed at HEAD", pending(state, pairs)[0] == {})

        fix = commit("bin/a.py", "a2\n", "a: fix the thing")
        todo, closed, newest = pending(state, pairs)
        check("upstream change to a shared file is pending",
              list(todo) == ["bin/a.py"] and todo["bin/a.py"][0][0] == fix)
        check("no auto-closed commits here", closed == {})
        check("newest tracks the pending commit too", newest["bin/a.py"] == fix)
        commit("bin/unrelated.txt", "z\n", "unrelated")
        check("unrelated upstream change is not pending", list(pending(state, pairs)[0]) == ["bin/a.py"])

        state["files"]["bin/a.py"] = {"through": git(up, "rev-parse", "HEAD").strip(), "how": "ported"}
        check("marking clears it", pending(state, pairs)[0] == {})

        with open(os.path.join(repo, "bin", "new.py"), "w") as fh:
            fh.write("y\n")
        check("a new repo file is reported unmapped", unmapped(pairs, only, repo) == ["bin/new.py"])

        state["files"].pop("bin/b.py")
        try:
            pending(state, pairs)
            check("a pair with no watermark is a setup error", False)
        except SetupError:
            check("a pair with no watermark is a setup error", True)

        # An inherited GIT_DIR (as inside a git hook) must not redirect the tool's git calls.
        saved_gd = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = os.path.join(d, "nowhere", ".git")
        try:
            state["files"]["bin/b.py"] = {"through": base, "how": "seed"}
            try:
                got = "bin/b.py" in pending(state, pairs)[0]
            except SetupError:
                got = False
            check("git calls ignore an inherited GIT_DIR", got)
        finally:
            if saved_gd is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = saved_gd

        probs = []
        state["files"]["bin/b.py"] = {"through": "0" * 40, "how": "seed"}
        try:
            pending(state, pairs, probs)
            check("an unknown watermark is a setup error, not 'not after'", False)
        except SetupError as exc:
            check("an unknown watermark is a setup error, not 'not after'", "unknown commit" in str(exc))
        state["files"]["bin/b.py"] = {"through": base, "how": "seed"}
        git(up, "rm", "-q", "bin/b.py")
        git(up, "commit", "-q", "-m", "b: removed")
        probs = []
        pending(state, pairs, probs)
        check("an upstream path that vanished is a problem", any("no longer exists" in p for p in probs))

        # CLI paths, against this throwaway map and state.
        global ROOT
        saved_root, saved_env = ROOT, os.environ.get("SESSION_NOTES_UPSTREAM_STATE")
        ROOT = repo
        os.environ["SESSION_NOTES_UPSTREAM_STATE"] = os.path.join(d, "cli", "state.json")
        try:
            import contextlib
            import io
            def run(*a):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    rc = main(list(a))
                return rc, buf.getvalue()
            check("check before seed exits 2", run("check")[0] == 2)
            check("seed exits 0", run("seed", "--upstream", up, "--at", base)[0] == 0)
            check("seed again without --force refuses", run("seed", "--upstream", up, "--at", base)[0] == 2)
            check("seed --force replaces", run("seed", "--upstream", up, "--at", base, "--force")[0] == 0)
            rc, out = run("mark", "bin/a.py", "ported", "--through", fix)
            check("mark reports what it closes", rc == 0 and "closing 1 upstream change" in out)
            rc, _ = run("mark", "bin/a.py", "ported", "--through", base)
            check("mark backwards refuses", rc == 2)
            st = load_state()
            st["files"].pop("bin/a.py")
            save_state(st)
            rc, out = run("mark", "bin/a.py", "local-only")
            check("mark with no watermark records a first decision", rc == 0 and "first decision" in out)
            check("check exits 1 while something needs a decision", run("check")[0] == 1)
        finally:
            ROOT = saved_root
            if saved_env is None:
                os.environ.pop("SESSION_NOTES_UPSTREAM_STATE", None)
            else:
                os.environ["SESSION_NOTES_UPSTREAM_STATE"] = saved_env

        sp = os.path.join(d, "cfg", "state.json")
        save_state(state, sp)
        check("state saved private (0600)", oct(os.stat(sp).st_mode & 0o777) == "0o600")
        check("state round-trips", load_state(sp) == state)
        shutil.rmtree(up)

        # Auto-close: an upstream commit saying it is itself a port OF a session-notes PR.
        up2, repo2 = os.path.join(d, "up2"), os.path.join(d, "repo2")
        os.makedirs(os.path.join(up2, "bin"))
        os.makedirs(os.path.join(repo2, "bin"))
        os.makedirs(os.path.join(repo2, "tools"))
        git(up2, "init", "-q")
        git(up2, "config", "user.email", "test@example.com")
        git(up2, "config", "user.name", "Selftest")

        def commit2(text, msg, body=None):
            with open(os.path.join(up2, "bin", "a.py"), "w") as fh:
                fh.write(text)
            git(up2, "add", "bin/a.py")
            cargs = ["commit", "-q", "-m", msg]
            if body:
                cargs += ["-m", body]
            git(up2, *cargs)
            return git(up2, "rev-parse", "HEAD").strip()

        ac_base = commit2("a0\n", "a: first")
        with open(os.path.join(repo2, "bin", "a.py"), "w") as fh:
            fh.write("x\n")
        with open(os.path.join(repo2, "tools", "upstream.toml"), "w") as fh:
            fh.write('[[pair]]\nrepo = "bin/a.py"\nupstream = "bin/a.py"\n')
        pairs2, only2 = load_map(repo2)

        # Positive: real upstream phrasings (subject or body) that this repo originated.
        # (The owner is a placeholder, not this repo's own GitHub name: the regex's owner
        # prefix is generic, see AUTOCLOSE_RE, so this exercises that generic match.)
        pos_pr = commit2("a1\n", "Port of someuser/session-notes PR #15.")
        pos_hash = commit2("a2\n", "Port of session-notes #14.")
        pos_compact = commit2("a3\n", "bus: retire a finished turn's mail on Stop "
                                      "(port of session-notes#8)")
        pos_body = commit2("a4\n", "a: refactor the helper", body="Port of someuser/session-notes PR #22.")
        # Anti-population: real near-miss phrasings that must stay pending.
        neg_unrelated_hash = commit2("a5\n", "personal_brief: one line for session-notes "
                                              "upstream drift (#31)")
        neg_sent_to = commit2("a6\n", "Port sent to session-notes.")
        neg_ported_to = commit2("a7\n", "ported to session-notes later")
        neg_bare_mention = commit2("a8\n", "session-notes#8 is related")
        odd_body = commit2("a8b\n", "a: odd body", body="stray \x1e and \x1f separators")
        neg_support_of = commit2("a9\n", "support of session-notes #3")
        newest_sha = neg_support_of  # the true HEAD: last commit made

        state2 = {"upstream": up2, "files": {"bin/a.py": {"through": ac_base, "how": "seed"}}}
        todo2, closed2, newest2 = pending(state2, pairs2)
        todo_shas = {r[0] for r in todo2.get("bin/a.py", [])}
        refs = {c[0]: c[3] for c in closed2.get("bin/a.py", [])}

        check("'Port of owner/session-notes PR #N.' auto-closes", refs.get(pos_pr) == "#15")
        check("'Port of session-notes #N.' auto-closes", refs.get(pos_hash) == "#14")
        check("compact parenthetical 'port of session-notes#N' auto-closes", refs.get(pos_compact) == "#8")
        check("a match in the BODY (not the subject) auto-closes", refs.get(pos_body) == "#22")
        check("auto-closed commits are excluded from pending",
              todo_shas.isdisjoint({pos_pr, pos_hash, pos_compact, pos_body}))
        check("a body holding the separator characters parses and stays pending", odd_body in todo_shas)
        check("an unrelated trailing '(#N)' with no 'port of' stays pending", neg_unrelated_hash in todo_shas)
        check("'Port sent to' (no 'of') stays pending", neg_sent_to in todo_shas)
        check("'ported to' (not 'port of') stays pending", neg_ported_to in todo_shas)
        check("a bare 'session-notes#N' mention with no 'port of' stays pending", neg_bare_mention in todo_shas)
        check("'support of' does not false-match the 'port of' substring it contains",
              neg_support_of in todo_shas)
        check("newest tracks the true HEAD across pending and auto-closed", newest2["bin/a.py"] == newest_sha)

        # Only-auto-closed history: nothing pending, but a decision is still owed.
        up3 = os.path.join(d, "up3")
        os.makedirs(os.path.join(up3, "bin"))
        git(up3, "init", "-q")
        git(up3, "config", "user.email", "test@example.com")
        git(up3, "config", "user.name", "Selftest")

        def commit3(text, msg, body=None):
            with open(os.path.join(up3, "bin", "a.py"), "w") as fh:
                fh.write(text)
            git(up3, "add", "bin/a.py")
            cargs = ["commit", "-q", "-m", msg]
            if body:
                cargs += ["-m", body]
            git(up3, *cargs)
            return git(up3, "rev-parse", "HEAD").strip()

        base3 = commit3("a0\n", "a: first")
        only_close = commit3("a1\n", "Port of session-notes #7.")
        state3 = {"upstream": up3, "files": {"bin/a.py": {"through": base3, "how": "seed"}}}
        todo3, closed3, newest3 = pending(state3, pairs2)
        check("an only-auto-closed history has empty pending", todo3 == {})
        check("an only-auto-closed history still reports the auto-close",
              closed3.get("bin/a.py", [(None,)])[0][0] == only_close)

        # CLI: JSON shape, exit code, and the copy-paste mark-command footer.
        saved_root2, saved_env2 = ROOT, os.environ.get("SESSION_NOTES_UPSTREAM_STATE")
        ROOT = repo2
        os.environ["SESSION_NOTES_UPSTREAM_STATE"] = os.path.join(d, "cli2", "state.json")
        try:
            import contextlib
            import io

            def run2(*a):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    rc = main(list(a))
                return rc, buf.getvalue()

            check("seed on the mixed history exits 0", run2("seed", "--upstream", up2, "--at", ac_base)[0] == 0)
            rc, out = run2("check", "--json")
            payload = json.loads(out)
            check("JSON pending excludes auto-closed shas", {r["sha"] for r in payload["files"]["bin/a.py"]}
                  == todo_shas)
            check("JSON pending count matches", payload["pending"] == len(todo_shas))
            check("JSON auto_closed lists ports with their ref", {
                (r["sha"], r["ref"]) for r in payload["auto_closed"]["bin/a.py"]} ==
                {(pos_pr, "#15"), (pos_hash, "#14"), (pos_compact, "#8"), (pos_body, "#22")})

            rc, out = run2("check")
            check("exit code is 1 while real upstream changes are pending", rc == 1)
            check("footer prints a mark command with the newest sha for that file",
                  ("upstream_drift.py mark bin/a.py ported|local-only --through %s" % newest_sha[:9]) in out)
            check("the mark command keeps 'ported|local-only' explicit, not auto-chosen", "ported|local-only" in out)
            check("auto-closed commits print their own section",
                  "(4 auto-closed: ports of session-notes PRs)" in out)
            check("the auto-closed check-before-marking footer line prints",
                  "check none carries an extra upstream-only change, then mark the file." in out)

            rc, out = run2("mark", "bin/a.py", "ported", "--through", newest_sha[:9])
            check("mark accepts a 9-character abbreviated sha", rc == 0)

            check("re-seed on the only-auto-closed history exits 0",
                  run2("seed", "--upstream", up3, "--at", base3, "--force")[0] == 0)
            rc, out = run2("check")
            check("exit code is 0 when only auto-closed commits remain and nothing is unmapped", rc == 0)
            check("'nothing pending' still prints when only auto-closed commits remain",
                  "upstream_drift: nothing pending" in out)
            check("the auto-closed section still prints after 'nothing pending'",
                  "(1 auto-closed: ports of session-notes PRs)" in out)
        finally:
            ROOT = saved_root2
            if saved_env2 is None:
                os.environ.pop("SESSION_NOTES_UPSTREAM_STATE", None)
            else:
                os.environ["SESSION_NOTES_UPSTREAM_STATE"] = saved_env2
        shutil.rmtree(up2)
        shutil.rmtree(up3)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("seed", help="start every watermark at one upstream commit")
    s.add_argument("--upstream", required=True)
    s.add_argument("--at", required=True)
    s.add_argument("--force", action="store_true", help="replace an existing state file")
    c = sub.add_parser("check", help="list pending upstream commits")
    c.add_argument("--json", action="store_true")
    m = sub.add_parser("mark", help="record a decision for one file")
    m.add_argument("file")
    m.add_argument("decision", choices=DECISIONS)
    m.add_argument("--through", help="upstream commit decided through (default: upstream HEAD)")
    m.add_argument("--note")
    m.add_argument("--reset", action="store_true", help="upstream history was rewritten; skip the forward-only check")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    try:
        if args.cmd == "seed":
            return cmd_seed(args)
        if args.cmd == "mark":
            return cmd_mark(args)
        return cmd_check(argparse.Namespace(json=getattr(args, "json", False)))
    except SetupError as exc:
        print("upstream_drift: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
