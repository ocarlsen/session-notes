#!/usr/bin/env python3
"""upstream_drift.py -- what changed upstream that this repo has not taken yet.

session-notes was extracted from a working Claude Code config (a ~/.claude kept in git).
Fixes keep landing there, and nothing carries them here unless someone remembers. This tool
lists, for every file the two share (tools/upstream.toml), the upstream commits since the
last decision about that file. A decision is one of two marks, and every upstream change has
to end in one of them:

  ported      the change (or its equivalent) is in this repo
  local-only  the change is specific to that one setup and does not belong here

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
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import notes_config  # noqa: E402

TRACKED_DIRS = ("bin", "hooks", "skills")
DECISIONS = ("ported", "local-only")


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
    """{repo_file: [(sha, date, subject), ...]} of upstream commits after each watermark.
    Map problems that need a person (a pair never decided, an upstream path that no longer
    exists, a watermark gone from upstream history) are appended to `problems` when given,
    else raised."""
    up = os.path.expanduser(state["upstream"])
    if not os.path.isdir(up):
        raise SetupError("upstream %s does not exist" % up)
    out = {}

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
        log = git(up, "log", "--format=%H%x09%ad%x09%s", "--date=short",
                  "%s..HEAD" % mark["through"], "--", u)
        rows = [tuple(l.split("\t", 2)) for l in log.splitlines() if l.strip()]
        if rows:
            out[h] = rows
    return out


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
    todo = pending(state, pairs, problems)
    loose = unmapped(pairs, only)
    count = sum(len(v) for v in todo.values())
    if args.json:
        print(json.dumps({"pending": count, "files": {h: [{"sha": s, "date": d, "subject": m}
                          for s, d, m in rows] for h, rows in todo.items()},
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
        for f in loose:
            print("%s  UNMAPPED: add it to tools/upstream.toml as a pair or repo_only" % f)
        if todo:
            print("\nFor each file: port the change, or decide it is local-only, then\n"
                  "  upstream_drift.py mark FILE ported|local-only [--through SHA]")
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
        check("clean after seed at HEAD", pending(state, pairs) == {})

        fix = commit("bin/a.py", "a2\n", "a: fix the thing")
        todo = pending(state, pairs)
        check("upstream change to a shared file is pending",
              list(todo) == ["bin/a.py"] and todo["bin/a.py"][0][0] == fix)
        commit("bin/unrelated.txt", "z\n", "unrelated")
        check("unrelated upstream change is not pending", list(pending(state, pairs)) == ["bin/a.py"])

        state["files"]["bin/a.py"] = {"through": git(up, "rev-parse", "HEAD").strip(), "how": "ported"}
        check("marking clears it", pending(state, pairs) == {})

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
                got = "bin/b.py" in pending(state, pairs)
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
