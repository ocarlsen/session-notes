#!/usr/bin/env python3
"""live.py -- live context: one machine-wide rolling file of what just happened.

Memory stores record what a scope decided; they are written at decision points and
read at boot. Live context is the short, recent, cross-scope view: one line per
event ("merged PR #12: ...", "decided to drop the cache layer"), newest first, so a
session starting anywhere sees what the others just did. Both Claude Code and Codex
read the same file, and both can write it. It lives outside both agent homes
(~/.local/state/session-notes/ by default), because Codex must
never touch ~/.claude.

File format (default ~/.local/state/session-notes/live-context.md, key `live.path`):

    # Live context (newest first; written by live.py, do not hand-edit)

    - 2026-11-21 14:03 PST [recipes-app] merged PR #12: add search (https://...)
    - 2026-11-21 13:40 PST [finance] decided to keep the old ledger format

Only the last `live.max_lines` entries are kept; one entry's text is at most
`live.max_chars` characters (a longer one is refused, never truncated). The stamp is in
`[time].timezone` (empty = system local). The file is only ever rewritten whole, under
an flock, through a temp file and os.replace; a symlinked file is updated in place.

ISOLATION. A scope in the config may set `live`:
  "shared" (default)  reads and writes the shared file
  "own"               reads and writes ONLY its `live_file` (an isolated scope with
                      its own file); relative paths are relative to the scope root
  "none"              isolated: reads nothing, writes nothing
A folder outside every scope is tagged with its git root's name (else "unscoped")
and is shared. A linked git worktree always takes the scope of its main checkout, wherever
it lives. If git cannot be run, a folder under no scope root is treated as isolated.

Importable API (for tools that write events mechanically):
  scope_for(cwd, cfg=None) -> (tag, mode, path)
  add(scope_or_cwd, text, cfg=None, now=None) -> the written line, or None (mode none)
  read_entries(cwd, cfg=None) -> [entry lines]
  render(cwd, cfg=None, budget=None) -> the block a session starting in cwd should see

Commands:
  live.py note "text" | live.py note -      add an entry for the cwd (`-` reads stdin;
                                            prefer it, shells expand $ and backticks);
                                            exit 3 in an isolated scope, 2 on error
  live.py show [--cwd DIR]                  print what a session in DIR should see
  live.py hook                              Claude Code SessionStart hook (JSON on stdin);
                                            silent when empty, never fails session start
  live.py --selftest
"""
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import notes_config  # noqa: E402

HEADER = "# Live context (newest first; written by live.py, do not hand-edit)"


class LiveError(ValueError):
    pass


UNDETERMINED = "undetermined"


def _git_facts(cwd):
    """(main_checkout, toplevel) of the repo holding cwd, (None, None) when cwd is not in a
    git work tree, or raises OSError/subprocess.SubprocessError for ANY other answer: git
    missing or hung, a non-zero exit that is not "not a git repository" (dubious ownership,
    a moved main checkout), or output that is not exactly two absolute paths (an old git).
    Fail closed: the caller then reads and writes nothing.
    Every GIT_* variable is dropped so an inherited GIT_DIR cannot answer for another repo,
    and LC_ALL=C keeps the "not a git repository" message in English."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["LC_ALL"] = "C"
    out = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute",
                          "--git-common-dir", "--show-toplevel"],
                         capture_output=True, text=True, timeout=5, env=env)
    lines = out.stdout.splitlines()
    if out.returncode != 0:
        if "not a git repository" in out.stderr:
            return None, None
        raise subprocess.SubprocessError("git rev-parse exited %d: %s"
                                         % (out.returncode, out.stderr.strip()[:200]))
    if len(lines) != 2 or not all(os.path.isabs(x) for x in lines):
        raise subprocess.SubprocessError("unexpected git rev-parse output: %r" % out.stdout[:200])
    common, top = os.path.realpath(lines[0]), os.path.realpath(lines[1])
    main = os.path.dirname(common) if os.path.basename(common) == ".git" else top
    return main, top


def _deepest(path, scopes):
    best = None
    for s in scopes:
        r = os.path.realpath(s["root"])
        if notes_config._under(path, r) and (best is None or len(r) > best[0]):
            best = (len(r), s)
    return best[1] if best else None


def scope_for(cwd, cfg=None):
    """(tag, mode, path) for a working directory. path is None when mode is "none".

    A LINKED git worktree always belongs to the scope of its MAIN checkout, wherever wt.py
    or git put it (even under another scope's root); any other folder is matched by its own
    path. If git cannot answer cleanly (see _git_facts), the result is mode "none" with tag
    UNDETERMINED, even inside a shared scope: fail closed, read and write nothing."""
    cfg = cfg or notes_config.load()
    cwd = os.path.realpath(os.path.expanduser(cwd))
    main = top = None
    if os.path.isdir(cwd):
        try:
            main, top = _git_facts(cwd)
        except (OSError, subprocess.SubprocessError):
            return UNDETERMINED, "none", None
    linked = bool(main) and main != top
    hit = _deepest(main if linked else cwd, cfg["scopes"])
    if hit:
        return _scope_result(hit, cfg)
    tag = os.path.basename(main) if main else "unscoped"
    return tag or "unscoped", "shared", cfg["live"]["path"]


def _scope_result(s, cfg):
    mode = s["live"]
    if mode == "none":
        return s["name"], mode, None
    return s["name"], mode, (s["live_file"] if mode == "own" else cfg["live"]["path"])


def _resolve_target(scope_or_cwd, cfg):
    if os.sep not in scope_or_cwd:
        for s in cfg["scopes"]:
            if s["name"] == scope_or_cwd:
                return _scope_result(s, cfg)
        if scope_or_cwd == "unscoped":
            return "unscoped", "shared", cfg["live"]["path"]
        if not os.path.isdir(scope_or_cwd):
            raise ValueError("%r is neither a configured scope nor a folder" % scope_or_cwd)
    return scope_for(scope_or_cwd, cfg)


def _tz(cfg):
    name = cfg["time"]["timezone"].strip()
    if not name:
        return None
    import zoneinfo
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception as exc:
        raise LiveError("time.timezone %r is not a usable zone (%s)" % (name, exc))


def _stamp(cfg, now):
    tz = _tz(cfg)
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz) if tz else now.astimezone()
    local = now.astimezone(tz) if tz else now.astimezone()
    return "%s %s" % (local.strftime("%Y-%m-%d %H:%M"), local.tzname())


def _parse(path):
    """Entry lines of an existing file. Raises LiveError on a stray non-entry line."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for n, line in enumerate(lines, 1):
        if not line.strip() or line == HEADER:
            continue
        if line.startswith("- "):
            out.append(line)
        else:
            raise LiveError("%s line %d is not a live-context entry (%r); refusing to rewrite "
                            "the file. Remove the line by hand or move the file aside."
                            % (path, n, line[:60]))
    return out


def add(scope_or_cwd, text, cfg=None, now=None):
    """Prepend one entry. Returns the written line, or None in an isolated ("none") scope."""
    cfg = cfg or notes_config.load()
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("live context entry is empty")
    limit = cfg["live"]["max_chars"]
    if len(text) > limit:
        raise ValueError("live context entry is %d characters, over live.max_chars = %d; "
                         "shorten it" % (len(text), limit))
    tag, mode, path = _resolve_target(scope_or_cwd, cfg)
    if mode == "none":
        return None
    line = "- %s [%s] %s" % (_stamp(cfg, now), tag, text)
    real = os.path.realpath(path)
    d = os.path.dirname(real)
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(real + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = _parse(real)
        entries = ([line] + entries)[:cfg["live"]["max_lines"]]
        fd, tmp = tempfile.mkstemp(prefix=".live-", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(HEADER + "\n\n" + "\n".join(entries) + "\n")
            os.replace(tmp, real)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return line


def read_entries(cwd, cfg=None):
    """Entry lines (newest first) a session in cwd should see; [] when isolated or empty."""
    cfg = cfg or notes_config.load()
    _tag, mode, path = scope_for(cwd, cfg)
    if mode == "none" or not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [ln for ln in lines if ln.startswith("- ")]


def render(cwd, cfg=None, budget=None):
    """The block for cwd: an intro line, then entries. "" when there is nothing to show
    or (with budget, in characters) not even one entry fits."""
    cfg = cfg or notes_config.load()
    entries = read_entries(cwd, cfg)
    if not entries:
        return ""
    path = scope_for(cwd, cfg)[2]
    intro = ("Live context (newest first, from %s; loaded at session start, "
             "re-read with live.py show):" % path)
    n = len(entries)
    for k in range(n, 0, -1):
        text = "\n".join([intro] + entries[:k])
        if k < n:
            text += "\n(%d older entries not shown)" % (n - k)
        if budget is None or len(text) <= budget:
            return text
    return ""


def _trace(where):
    try:
        d = os.path.join(notes_config.load()["paths"]["state_dir"], "errors")
    except Exception:
        d = os.path.expanduser("~/.claude/session-notes/state/errors")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "live_%s.txt" % where), "a") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()))
            fh.write(traceback.format_exc() + "\n")
    except OSError:
        pass


def cmd_hook():
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        text = render(payload.get("cwd") or os.getcwd())
        if text:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception:
        _trace("hook")
    return 0


def _flag(argv, name):
    if name in argv:
        i = argv.index(name)
        if i + 1 >= len(argv):
            raise LiveError("%s needs a value" % name)
        v = argv[i + 1]
        del argv[i:i + 2]
        return v
    return None


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], list(argv[1:])
    try:
        if cmd == "hook":
            return cmd_hook()
        cwd = _flag(rest, "--cwd") or os.getcwd()
        if cmd == "show":
            text = render(cwd)
            if text:
                print(text)
            return 0
        if cmd == "note":
            if len(rest) != 1:
                raise LiveError('usage: live.py note "text"  |  live.py note -  (text on stdin)')
            text = sys.stdin.read() if rest[0] == "-" else rest[0]
            tag, mode, _p = scope_for(cwd)
            if mode == "none":
                if tag == UNDETERMINED:
                    print("live.py: scope could not be determined because git failed; "
                          "nothing written", file=sys.stderr)
                else:
                    print('live.py: scope %s is isolated from live context (live = "none"); '
                          'nothing written' % tag, file=sys.stderr)
                return 3
            print(add(cwd, text))
            return 0
        raise LiveError("unknown command %r (note, show, hook)" % cmd)
    except (LiveError, ValueError, OSError, notes_config.ConfigError) as exc:
        print("live.py: %s" % exc, file=sys.stderr)
        return 2


def selftest():
    fails = []

    def check(name, cond, info=None):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)
            if info is not None:
                print("     %r" % (info,))

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        for sub in ("recipes", "fin", "client", "plain", "repo-x"):
            os.makedirs(os.path.join(d, sub))
        gitc = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        for sub in ("repo-x", "recipes", "fin", "client"):
            subprocess.run(gitc + ["init", "-q", os.path.join(d, sub)], check=True)
            subprocess.run(gitc + ["-C", os.path.join(d, sub), "commit", "-q", "--allow-empty",
                                   "-m", "init"], check=True)
        os.makedirs(os.path.join(d, "work"))
        subprocess.run(gitc + ["init", "-q", os.path.join(d, "work", "client")], check=True)
        subprocess.run(gitc + ["-C", os.path.join(d, "work", "client"), "commit", "-q",
                               "--allow-empty", "-m", "init"], check=True)
        wa = os.path.join(d, "work", ".client-worktrees", "feat")
        wb = os.path.join(d, "work", "shared-wt", "feat2")
        for w in (wa, wb):
            subprocess.run(gitc + ["-C", os.path.join(d, "work", "client"), "worktree", "add", "-q",
                                   "-b", os.path.basename(w), w], check=True)
        wts = {}
        for sub, repo in (("recipes", "recipes"), ("fin", "fin"), ("client", "client"),
                          ("repo-x", "repo-x")):
            wts[sub] = os.path.join(d, ".%s-worktrees" % repo, "feat")
            subprocess.run(gitc + ["-C", os.path.join(d, sub), "worktree", "add", "-q", "-b", "feat",
                                   wts[sub]], check=True)
        cfgf = os.path.join(d, "c.toml")
        with open(cfgf, "w") as fh:
            fh.write('[paths]\nstate_dir = "%(d)s/state"\n[time]\ntimezone = "Etc/GMT+8"\n'
                     '[live]\npath = "%(d)s/state/live-context.md"\nmax_lines = 30\nmax_chars = 60\n'
                     '[[scopes]]\nname = "recipes-app"\nroot = "%(d)s/recipes"\n'
                     '[[scopes]]\nname = "finance"\nroot = "%(d)s/fin"\nlive = "own"\n'
                     'live_file = "live.md"\n'
                     '[[scopes]]\nname = "work-client"\nroot = "%(d)s/client"\nlive = "none"\n'
                     '[[scopes]]\nname = "outer"\nroot = "%(d)s/work"\nworktree_root = "%(d)s/work/shared-wt"\n'
                     '[[scopes]]\nname = "inner-none"\nroot = "%(d)s/work/client"\nlive = "none"\n'
                     % {"d": d})
        cfg = notes_config.load(cfgf)
        env = {k: v for k, v in os.environ.items() if k != "SESSION_NOTES_AGENT"}
        env["SESSION_NOTES_CONFIG"] = cfgf
        shared = os.path.join(d, "state", "live-context.md")
        own = os.path.join(d, "fin", "live.md")
        rec, fin, cli = (os.path.join(d, x) for x in ("recipes", "fin", "client"))
        t0 = datetime.datetime(2026, 11, 21, 22, 3, tzinfo=datetime.timezone.utc)

        def run(*args, stdin="", env=env):
            r = subprocess.run([sys.executable, os.path.realpath(__file__)] + list(args),
                               input=stdin, capture_output=True, text=True, env=env, cwd=d)
            return r.returncode, r.stdout, r.stderr

        check("scope_for: configured scope",
              scope_for(rec, cfg) == ("recipes-app", "shared", shared))
        check("scope_for: own", scope_for(fin, cfg) == ("finance", "own", own))
        check("scope_for: none", scope_for(cli, cfg) == ("work-client", "none", None))
        check("scope_for: unscoped git repo tagged by basename",
              scope_for(os.path.join(d, "repo-x"), cfg) == ("repo-x", "shared", shared))
        check("scope_for: plain folder is unscoped",
              scope_for(os.path.join(d, "plain"), cfg)[0] == "unscoped")

        line = add(rec, "first thing", cfg, now=t0)
        check("line format", line == "- 2026-11-21 14:03 -08 [recipes-app] first thing", line)
        add("recipes-app", "second   thing\nwith newline", cfg, now=t0)
        e = read_entries(rec, cfg)
        check("newest first, whitespace collapsed",
              e[0].endswith("[recipes-app] second thing with newline") and e[1].endswith("first thing"))
        with open(shared) as fh:
            body = fh.read()
        check("header then blank then entries", body.startswith(HEADER + "\n\n- "))
        a1 = add("recipes-app", "same text", cfg, now=t0)
        a2 = add(rec, "same text", cfg, now=t0)
        check("add by scope tag and by cwd produce the same line", a1 == a2 and "[recipes-app]" in a1, (a1, a2))
        try:
            add(rec, "   \n ", cfg)
            check("empty text refused", False)
        except ValueError:
            check("empty text refused", True)
        try:
            add(rec, "x" * 61, cfg)
            check("over max_chars refused", False)
        except ValueError as exc:
            check("over max_chars refused, names the limit", "max_chars = 60" in str(exc))
        check("max_chars boundary accepted", add(rec, "x" * 60, cfg, now=t0) is not None)

        for i in range(35):
            add(rec, "n%d" % i, cfg, now=t0)
        e = read_entries(rec, cfg)
        check("max_lines cap keeps the newest 30",
              len(e) == 30 and e[0].endswith("n34") and e[-1].endswith("n5"), (len(e), e[:1]))

        # own scope
        add(fin, "ledger closed", cfg, now=t0)
        check("own scope writes its own file", os.path.exists(own) and "ledger closed" in open(own).read())
        check("own scope did not touch the shared file", "ledger closed" not in open(shared).read())
        check("own scope reads only its own file",
              read_entries(fin, cfg) == [l for l in open(own).read().splitlines() if l.startswith("- ")]
              and all("recipes-app" not in x for x in read_entries(fin, cfg)))
        check("shared scope does not read the own file",
              all("ledger closed" not in x for x in read_entries(rec, cfg)))

        # none scope
        before = open(shared).read()
        rc, out, err = run("note", "-", "--cwd", cli, stdin="secret")
        check("none scope: note exits 3 with the message",
              rc == 3 and "isolated from live context" in err and "work-client" in err, (rc, err))
        check("none scope: nothing written", open(shared).read() == before and add(cli, "x", cfg) is None)
        check("none scope: show prints nothing", run("show", "--cwd", cli)[1:] == ("", ""))
        rc, out, err = run("hook", stdin=json.dumps({"cwd": cli}))
        check("none scope: hook prints nothing", rc == 0 and out == "", (rc, out))
        check("none scope: render empty", render(cli, cfg) == "")

        # linked worktrees belong to the scope of their main checkout
        wr, wf, wc, wx = wts["recipes"], wts["fin"], wts["client"], wts["repo-x"]
        check("worktree of a shared scope is tagged with the scope name",
              scope_for(wr, cfg) == ("recipes-app", "shared", shared), scope_for(wr, cfg))
        check("worktree of an own scope is that scope",
              scope_for(wf, cfg) == ("finance", "own", own), scope_for(wf, cfg))
        check("worktree of a none scope is that scope",
              scope_for(wc, cfg) == ("work-client", "none", None), scope_for(wc, cfg))
        check("worktree of an unscoped repo is tagged with the main checkout name",
              scope_for(wx, cfg) == ("repo-x", "shared", shared), scope_for(wx, cfg))
        before = open(shared).read()
        rc, out, err = run("note", "-", "--cwd", wc, stdin="from a worktree")
        check("none-scope worktree: note exits 3, shared file unchanged",
              rc == 3 and open(shared).read() == before, (rc, err))
        check("none-scope worktree: show prints nothing", run("show", "--cwd", wc)[1:] == ("", ""))
        rc, out, err = run("hook", stdin=json.dumps({"cwd": wc}))
        check("none-scope worktree: hook prints nothing", rc == 0 and out == "", out)
        rc, out, err = run("note", "-", "--cwd", wf, stdin="own from worktree")
        check("own-scope worktree: note goes to live_file only",
              rc == 0 and "[finance]" in out and "own from worktree" in open(own).read()
              and open(shared).read() == before, (rc, err))
        rc, out, err = run("show", "--cwd", wf)
        check("own-scope worktree: show reads only live_file",
              "own from worktree" in out and "recipes-app" not in out, out)
        rc, out, err = run("note", "-", "--cwd", wr, stdin="shared from worktree")
        check("shared-scope worktree note is tagged with the scope", rc == 0 and "[recipes-app]" in out, out)

        # a worktree under ANOTHER scope's root or worktree_root still takes its main checkout's scope
        for label, w in (("A: under an enclosing shared scope's root", wa),
                         ("B: in an enclosing scope's worktree_root", wb)):
            snap = open(shared).read()
            check("worktree %s resolves to the none scope" % label,
                  scope_for(w, cfg) == ("inner-none", "none", None), scope_for(w, cfg))
            check("worktree %s: show prints nothing" % label, run("show", "--cwd", w)[1:] == ("", ""))
            rc, out, err = run("hook", stdin=json.dumps({"cwd": w}))
            check("worktree %s: hook prints nothing" % label, rc == 0 and out == "", out)
            rc, out, err = run("note", "-", "--cwd", w, stdin="leak")
            check("worktree %s: note exits 3, shared file byte-identical" % label,
                  rc == 3 and open(shared).read() == snap, (rc, err))

        # fail closed when git cannot answer, and ignore an inherited GIT_DIR
        empty = os.path.join(d, "emptybin")
        os.makedirs(empty)
        nogit = dict(env, PATH=empty)
        snap = open(shared).read()
        rc, out, err = run("note", "-", "--cwd", wc, stdin="leak", env=nogit)
        check("git missing: note refuses (exit 3), says git failed, shared file unchanged",
              rc == 3 and "git failed" in err and open(shared).read() == snap, (rc, err))
        check("git missing: show prints nothing", run("show", "--cwd", wc, env=nogit)[1:] == ("", ""))
        rc, out, err = run("hook", stdin=json.dumps({"cwd": wc}), env=nogit)
        check("git missing: hook prints nothing", rc == 0 and out == "", out)
        check("git missing, cwd under a scope root: still fails closed (exit 3)",
              run("note", "-", "--cwd", rec, stdin="leak", env=nogit)[0] == 3
              and open(shared).read() == snap)
        check("git missing, worktree B under an enclosing shared scope: show prints nothing",
              run("show", "--cwd", wb, env=nogit)[1:] == ("", ""))
        rc, out, err = run("note", "-", "--cwd", wb, stdin="leak", env=nogit)
        check("git missing, worktree B: note exits 3, shared file unchanged",
              rc == 3 and open(shared).read() == snap, (rc, err))
        # a git that answers badly: dubious ownership (exit 128), and an old git that echoes
        # the unknown --path-format flag back with exit 0
        for label, body in (("dubious ownership", 'echo "fatal: detected dubious ownership" >&2; exit 128'),
                            ("old git echo", 'echo --path-format=absolute; echo .git; echo "$PWD"; exit 0')):
            fake = os.path.join(d, "fakegit-" + label.replace(" ", "-"))
            os.makedirs(fake)
            with open(os.path.join(fake, "git"), "w") as fh:
                fh.write("#!/bin/sh\n" + body + "\n")
            os.chmod(os.path.join(fake, "git"), 0o755)
            fenv = dict(env, PATH=fake + os.pathsep + env.get("PATH", ""))
            check("%s: show prints nothing for an isolated worktree" % label,
                  run("show", "--cwd", wc, env=fenv)[1:] == ("", ""))
            rc, out, err = run("note", "-", "--cwd", wc, stdin="leak", env=fenv)
            check("%s: note exits 3, shared file unchanged" % label,
                  rc == 3 and open(shared).read() == snap, (rc, err))
        gd = dict(env, GIT_DIR=os.path.join(d, "recipes", ".git"), GIT_WORK_TREE=os.path.join(d, "recipes"))
        snap = open(shared).read()
        check("inherited GIT_DIR: show prints nothing", run("show", "--cwd", wc, env=gd)[1:] == ("", ""))
        rc, out, err = run("note", "-", "--cwd", wc, stdin="leak", env=gd)
        check("inherited GIT_DIR: note exits 3, shared file unchanged",
              rc == 3 and open(shared).read() == snap, (rc, err))
        check("non-repo folder is still unscoped and shared",
              run("note", "-", "--cwd", os.path.join(d, "plain"), stdin="plain ok")[0] == 0)

        # CLI note and show
        rc, out, err = run("note", "-", "--cwd", rec, stdin='uses $HOME and `ticks`\n')
        check("cli note via stdin", rc == 0 and re.match(
            r"^- \d{4}-\d\d-\d\d \d\d:\d\d \S+ \[recipes-app\] uses \$HOME and `ticks`$", out.strip()),
              (rc, out, err))
        rc, out, err = run("note", "", "--cwd", rec)
        check("cli empty note exits 2", rc == 2 and "empty" in err, (rc, err))
        rc, out, err = run("show", "--cwd", rec)
        check("cli show prints intro and entries",
              rc == 0 and out.startswith("Live context (newest first, from ") and "ticks" in out)
        rc, out, err = run("note", "x" * 70, "--cwd", rec)
        check("cli over-long note exits 2", rc == 2 and "max_chars" in err, (rc, err))

        # hook
        rc, out, err = run("hook", stdin=json.dumps({"cwd": rec}))
        j = json.loads(out)
        check("hook prints valid JSON",
              rc == 0 and j["hookSpecificOutput"]["hookEventName"] == "SessionStart"
              and "ticks" in j["hookSpecificOutput"]["additionalContext"])
        rc, out, err = run("hook", stdin="not json {{")
        check("hook survives malformed stdin", rc == 0)
        rc, out, err = run("hook", stdin=json.dumps({"cwd": os.path.join(d, "plain")}))
        check("hook for an unscoped shared cwd prints the shared entries",
              rc == 0 and "ticks" in json.loads(out)["hookSpecificOutput"]["additionalContext"])

        # budget
        full = render(rec, cfg)
        small = render(rec, cfg, budget=len(full) // 2)
        check("budget truncates and says how many are dropped",
              0 < len(small) <= len(full) // 2 and re.search(r"\n\(\d+ older entries not shown\)$", small),
              small[-80:])
        check("budget keeps the newest first", small.split("\n")[1] == full.split("\n")[1])
        check("budget too small gives nothing", render(rec, cfg, budget=20) == "")
        check("no budget = no marker", "not shown" not in full)

        # symlink survives
        real = os.path.join(d, "elsewhere.md")
        os.replace(own, real)
        os.symlink(real, own)
        add(fin, "after link", cfg, now=t0)
        check("symlink kept and updated in place",
              os.path.islink(own) and "after link" in open(real).read())

        # stray line refused, file untouched
        stray = os.path.join(d, "state", "live-context.md")
        with open(stray, "a") as fh:
            fh.write("hand written note\n")
        snap = open(stray).read()
        try:
            add(rec, "more", cfg, now=t0)
            check("stray line makes add raise", False)
        except LiveError as exc:
            check("stray line makes add raise naming the file", stray in str(exc))
        check("stray line: file not rewritten", open(stray).read() == snap)

        # config validation
        def bad(body):
            with open(cfgf + ".bad", "w") as fh:
                fh.write(body)
            try:
                notes_config.load(cfgf + ".bad")
                return None
            except notes_config.ConfigError as exc:
                return str(exc)
        m = bad('[[scopes]]\nname = "a"\nroot = "/x"\nlive = "own"\n')
        check("own needs live_file", m and "live_file" in m and "(a)" in m, m)
        m = bad('[[scopes]]\nname = "a"\nroot = "/x"\nlive_file = "f"\n')
        check("live_file refused without own", m and "live_file" in m, m)
        m = bad('[live]\npath = "/s/live-context.md"\n[[scopes]]\nname = "a"\nroot = "/x"\nlive = "own"\n'
                'live_file = "/s/live-context.md"\n')
        check("live_file equal to the shared path refused", m and "shared" in m and "(a)" in m, m)
        m = bad('[[scopes]]\nname = "a"\nroot = "/x"\nlive = "own"\nlive_file = "f"\n'
                '[[scopes]]\nname = "b"\nroot = "/x"\nlive = "own"\nlive_file = "/x/f"\n')
        check("two own scopes sharing a live_file refused", m and "a" in m and "b" in m and "same" in m, m)
        m = bad('[[scopes]]\nname = "a"\nroot = "/x"\nlive = "maybe"\n')
        check("bad live value refused", m and "'maybe'" in m and "(a)" in m, m)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main(sys.argv[1:]))
