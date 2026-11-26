#!/usr/bin/env python3
"""codex_save.py -- save memory from Codex in two steps, so the desktop app never asks.

The Codex desktop app runs each chat in its own sandbox: writable in the project folder,
a per-thread scratch folder and /tmp, and nothing else. It ignores
sandbox_workspace_write.writable_roots in config.toml. So every write to
~/.codex/session-notes is an escalation, and the app asks for approval each time: the
lock, the memory file, the index line, the release. A saved approval rule matches only an
exact command line (the app wraps every command in `/bin/zsh -lc "<command>"`), so a
command whose arguments change can never be approved once and for all.

This splits a save into:

  stage   python3 <repo>/bin/codex_save.py stage --store global|project ...  < content
          Queues one change in /tmp/session-notes-<uid>/<thread>/. Writes only /tmp, which
          the sandbox allows, so it never asks. The arguments may differ every time.

  commit  python3 <repo>/bin/codex_save.py commit
          Always exactly this text, so one saved rule (install.sh --codex offers it)
          approves it for good. It takes the reflect lock (waiting for another holder),
          applies this thread's queue in order, releases the lock and clears the queue.

What a staged change can be:
  --file NAME.md                 a memory file (content on stdin), created or replaced
  --index-line "- [T](f.md) - h" a MEMORY.md line, through append_memory_index (cap,
                                 REFERENCE routing and duplicate checks included)
  --resume --base SHA            RESUME.md replaced by stdin, only if the file on disk still
                                 hashes to SHA (`codex_save.py hash --store ...` prints it);
                                 otherwise commit skips it and says to re-read and restage.

Everything resolves under the Codex store (notes_config in codex mode), or under the shared
store root (paths.shared_stores_dir) for a scope that opted in with `shared_store = true`: that
scope's project store is the same folder Claude reads and writes. A path outside both, a name
with a slash, or MEMORY.md/REFERENCE.md as a --file is refused at stage time.
A --resume write that would leave RESUME.md over its budget (boot_budget's resume_bytes) is
refused by resume_guard at stage time and again at commit: move finished items to
RESUME-archive.md first. `--harness claude|codex` on --file stamps `harness:` frontmatter and on
--index-line prefixes the hook with `[harness]`, so the other agent knows to skip what does not
apply to it (absent = applies to all).
Commit needs CODEX_THREAD_ID (set by Codex for every command) to find its own queue.

    codex_save.py stage --store global --file feedback_x.md < body.md
    codex_save.py stage --store global --index-line "- [X](feedback_x.md) - hook"
    codex_save.py hash --store project
    codex_save.py stage --store project --resume --base <sha> < new_resume.md
    codex_save.py commit
    codex_save.py --selftest
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
# This tool exists only for Codex: never resolve Claude's stores, whatever the env says.
os.environ["SESSION_NOTES_AGENT"] = "codex"
import notes_config  # noqa: E402
import append_memory_index  # noqa: E402
import memory_hygiene_canary  # noqa: E402
import memory_index_lint  # noqa: E402
import reflect_lock  # noqa: E402
import resume_guard  # noqa: E402
import shared_store  # noqa: E402

LOCK_TIMEOUT = 15 * 60
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")


class SaveError(Exception):
    pass


def queue_root():
    return os.path.join("/tmp", "session-notes-%d" % os.getuid())


def _thread():
    t = (os.environ.get("CODEX_THREAD_ID") or "").strip()
    if not t or "/" in t or t.startswith("."):
        raise SaveError("CODEX_THREAD_ID is not set: run this from a Codex session")
    return t


def queue_dir():
    return os.path.join(queue_root(), _thread())


def resolve_store(which, cwd=None):
    cfg = notes_config.load()
    stores = notes_config.stores_for(cwd or os.getcwd(), cfg)
    path = stores["global"] if which == "global" else stores["project"]
    if not path:
        raise SaveError("no project store for %s (not in a configured scope or a git repo);"
                        " use --store global" % (cwd or os.getcwd()))
    check_root(path, cfg)
    return path


def check_root(path, cfg):
    """A store must sit under the Codex home's session-notes folder or the shared stores root;
    anything else is refused."""
    base = os.path.join(cfg["agent"]["codex_home"], "session-notes")
    shared = cfg["paths"]["shared_stores_dir"]
    if not (notes_config._under(path, base) or notes_config._under(path, shared)):
        raise SaveError("store %s is outside %s and the shared stores root %s" % (path, base, shared))


def file_hash(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except FileNotFoundError:
        return "absent"


HARNESSES = shared_store.HARNESSES


def _new_resume_text(content):
    return content if content.endswith("\n") else content + "\n"


def guard_resume(path, content):
    """Refuse (SaveError) a RESUME.md write that leaves it over budget (resume_guard)."""
    try:
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
    except FileNotFoundError:
        old = ""
    msg = resume_guard.check_write(path, old, _new_resume_text(content), cwd=os.getcwd())
    if msg:
        raise SaveError(msg)


_FRONTMATTER_FORM = ("---\nname: descriptive-kebab-slug\ndescription: one line saying when this is relevant\n"
                     "metadata:\n  type: user | feedback | project | reference\n---")


def check_frontmatter(name, content):
    """Refuse (SaveError) a memory file whose `type:` sits at the top level of its frontmatter: the
    canary's format is `metadata:` with `type:` nested under it. Parsed by the canary's own parser."""
    fm = memory_hygiene_canary.parse_frontmatter(content.lstrip("\ufeff"))  # a BOM must not hide the frontmatter
    if fm is not None and fm["top_type"] is not None:
        raise SaveError("%s: `type:` must be nested under `metadata:`, not at the top level of the frontmatter. "
                        "Re-stage the whole file in this form:\n%s" % (name, _FRONTMATTER_FORM))


def stage(args, content):
    store = resolve_store(args.store)
    item = {"store": store, "ts": time.time()}
    harness = getattr(args, "harness", None)
    if harness and not (args.file or args.index_line):
        raise SaveError("--harness goes with --file or --index-line")
    if args.file:
        if not _NAME_RE.match(args.file) or args.file in ("MEMORY.md", "REFERENCE.md", "RESUME.md"):
            raise SaveError("--file must be a plain NAME.md (not MEMORY.md, REFERENCE.md or"
                            " RESUME.md; use --index-line or --resume): %r" % args.file)
        if not content.strip():
            raise SaveError("--file needs the memory body on stdin")
        check_frontmatter(args.file, content)
        try:
            labeled = shared_store.with_harness(content, harness)
        except ValueError as exc:
            raise SaveError("%s: %s" % (args.file, exc)) from None
        item.update(kind="file", name=args.file, content=labeled)
    elif args.index_line:
        line = args.index_line.strip()
        if harness:
            if not shared_store._INDEX_RE.match(line):
                raise SaveError("--harness needs an index line shaped `- [Title](file.md) - hook`")
            line = shared_store.label_index_line(line, harness)
        # Refuse an over-cap line now, while the model can still shorten it.
        result = memory_index_lint.check_line(line)
        if not result["ok"]:
            raise SaveError(result["message"])
        item.update(kind="index", line=line, pin=bool(args.pin))
    elif args.resume:
        if not args.base:
            raise SaveError("--resume needs --base (from `codex_save.py hash --store ...`)")
        if not content.strip():
            raise SaveError("--resume needs the whole new RESUME.md on stdin")
        guard_resume(os.path.join(store, "RESUME.md"), content)
        item.update(kind="resume", base=args.base, content=content)
    else:
        raise SaveError("stage needs one of --file, --index-line, --resume")
    d = queue_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    name = "%017.6f-%d.json" % (time.time(), os.getpid())
    tmp = os.path.join(d, "." + name)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(item, fh)
    os.replace(tmp, os.path.join(d, name))
    print("staged %s for %s (run: codex_save.py commit)" % (item["kind"], store))


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, path)


def apply_item(item):
    """Apply one staged change. Returns a one-line report."""
    store = item["store"]
    check_root(store, notes_config.load())
    if item["kind"] == "file":
        path = os.path.join(store, item["name"])
        _atomic_write(path, item["content"])
        return "wrote %s" % path
    if item["kind"] == "index":
        index = os.path.join(store, "MEMORY.md")
        added = append_memory_index.append_line(index, item["line"], pin=item.get("pin", False))
        dest = append_memory_index.target_path_for(index, item["line"], pin=item.get("pin", False))
        return ("indexed in %s" if added else "already indexed in %s") % dest
    if item["kind"] == "resume":
        path = os.path.join(store, "RESUME.md")
        now = file_hash(path)
        if now != item["base"]:
            return ("SKIPPED %s: it changed since you read it (another session wrote it);"
                    " re-read it, merge, and stage --resume again" % path)
        guard_resume(path, item["content"])
        _atomic_write(path, item["content"])
        return "wrote %s" % path
    raise SaveError("unknown staged kind %r" % item.get("kind"))


def commit(timeout=LOCK_TIMEOUT):
    d = queue_dir()
    names = sorted(n for n in os.listdir(d) if n.endswith(".json")) if os.path.isdir(d) else []
    if not names:
        print("nothing staged for this thread")
        return 0
    ident, label, pid, pid_source = reflect_lock.my_identity()
    deadline = time.time() + timeout
    said = False
    while True:
        r = reflect_lock.acquire_all(ident, label, pid, pid_source)
        if r in ("acquired", "mine"):
            break
        if time.time() >= deadline:
            print("timeout after %ds: reflect lock still held by %s; nothing written" % (timeout, r),
                  file=sys.stderr)
            return 2
        if not said:
            print("waiting for the reflect lock (held by %s)" % r)
            said = True
        time.sleep(reflect_lock.POLL_SECS)
    took_it = r == "acquired"
    rc = 0
    try:
        for n in names:
            p = os.path.join(d, n)
            with open(p, encoding="utf-8") as fh:
                item = json.load(fh)
            try:
                report = apply_item(item)
            except (SaveError, memory_index_lint.HookCapError, OSError) as exc:
                report = "FAILED %s: %s" % (item.get("kind"), exc)
                rc = 1
            if report.startswith("SKIPPED"):
                rc = 1
            print(report)
            os.unlink(p)
        try:
            os.rmdir(d)  # only succeeds when the queue is empty
        except OSError:
            pass
    finally:
        if took_it:
            reflect_lock.release_all(ident, label)
            print("released the reflect lock")
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("stage")
    s.add_argument("--store", choices=["global", "project"], required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--index-line")
    g.add_argument("--resume", action="store_true")
    s.add_argument("--base")
    s.add_argument("--pin", action="store_true")
    s.add_argument("--harness", choices=HARNESSES)
    h = sub.add_parser("hash")
    h.add_argument("--store", choices=["global", "project"], required=True)
    sub.add_parser("commit")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "stage":
            content = "" if args.index_line else sys.stdin.read()
            stage(args, content)
            return 0
        if args.cmd == "hash":
            print(file_hash(os.path.join(resolve_store(args.store), "RESUME.md")))
            return 0
        if args.cmd == "commit":
            return commit()
    except (SaveError, notes_config.ConfigError) as exc:
        print("codex_save: %s" % exc, file=sys.stderr)
        return 2
    ap.print_help()
    return 2


def selftest():
    import io
    import contextlib
    import subprocess
    import tempfile
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
        if not cond:
            fails.append(name)

    def run(argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(argv)
        finally:
            sys.stdin = old
        return rc, out.getvalue(), err.getvalue()

    global queue_root
    real_queue_root = queue_root
    saved_env = {k: os.environ.get(k) for k in ("SESSION_NOTES_CONFIG", "CODEX_THREAD_ID")}
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        cfgf = os.path.join(d, "c.toml")
        with open(cfgf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\n[agent]\ncodex_home = "%s/codex"\n' % (d, d))
        os.environ["SESSION_NOTES_CONFIG"] = cfgf
        os.environ["CODEX_THREAD_ID"] = "thr-test"
        queue_root = lambda: os.path.join(d, "queue")  # noqa: E731
        reflect_lock.LOCK = None
        repo = os.path.join(d, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        here = os.getcwd()
        os.chdir(repo)
        try:
            g = os.path.join(d, "codex", "session-notes", "global")
            rc, out, _ = run(["stage", "--store", "global", "--file", "user_x.md"], "---\nname: x\n---\nbody\n")
            check("stage a file", rc == 0 and "staged file" in out)
            rc, _, _ = run(["stage", "--store", "global", "--index-line", "- [X](user_x.md) - hook"])
            check("stage an index line", rc == 0)
            check("nothing written before commit", not os.path.exists(os.path.join(g, "user_x.md")))
            rc, out, _ = run(["commit"])
            check("commit applies in order", rc == 0 and os.path.isfile(os.path.join(g, "user_x.md"))
                  and "- [X](user_x.md) - hook" in open(os.path.join(g, "MEMORY.md")).read())
            check("commit releases the lock", reflect_lock.read_lock()[0] is None)
            check("queue cleared", run(["commit"])[1].strip() == "nothing staged for this thread")
            check("empty queue folder removed", not os.path.exists(queue_dir()))

            rc, _, err = run(["stage", "--store", "global", "--file", "../evil.md"], "x")
            check("path traversal refused", rc == 2 and "plain NAME.md" in err)
            rc, _, err = run(["stage", "--store", "global", "--file", "MEMORY.md"], "x")
            check("MEMORY.md as a file refused", rc == 2)
            rc, _, err = run(["stage", "--store", "global", "--file", "user_t.md"],
                             "---\nname: t\ndescription: d\ntype: reference\n---\nbody\n")
            check("a top-level type: is refused with the correct form shown",
                  rc == 2 and "metadata:" in err and "  type:" in err, (rc, err))
            rc, _, err = run(["stage", "--store", "global", "--file", "user_t.md"],
                             "\ufeff---\nname: t\ndescription: d\ntype: reference\n---\nbody\n")
            check("a top-level type: behind a BOM is refused too", rc == 2 and "metadata:" in err, (rc, err))
            rc, out, _ = run(["stage", "--store", "global", "--file", "user_t.md"],
                             "---\nname: t\ndescription: d\nmetadata:\n  type: reference\n---\nbody\n")
            check("the nested metadata type is accepted", rc == 0 and "staged file" in out, (rc, out))
            run(["commit"])
            rc, _, _ = run(["stage", "--store", "global", "--index-line", "- [T](user_t.md) - type: reference"])
            check("an index line mentioning type: is unaffected", rc == 0)
            run(["commit"])
            rc, _, err = run(["stage", "--store", "global", "--index-line", "- [X](user_x.md) - " + "h" * 200])
            check("over-cap index line refused at stage time", rc == 2)

            # RESUME: applied when the base matches, skipped when another session wrote it.
            rc, base, _ = run(["hash", "--store", "project"])
            check("hash of an absent RESUME", base.strip() == "absent")
            run(["stage", "--store", "project", "--resume", "--base", base.strip()], "# RESUME\n- one\n")
            rc, out, _ = run(["commit"])
            p = notes_config.codex_project_store(repo, os.path.join(d, "codex"))
            check("resume written on a matching base", rc == 0 and "- one" in open(os.path.join(p, "RESUME.md")).read())
            rc, base, _ = run(["hash", "--store", "project"])
            run(["stage", "--store", "project", "--resume", "--base", base.strip()], "# RESUME\n- two\n")
            with open(os.path.join(p, "RESUME.md"), "a") as fh:
                fh.write("- sibling wrote this\n")
            rc, out, _ = run(["commit"])
            check("stale resume skipped, not clobbered", rc == 1 and "SKIPPED" in out
                  and "sibling wrote this" in open(os.path.join(p, "RESUME.md")).read())

            # The Codex boot cap: under resume_bytes (12000) but too big for the SessionStart pack.
            rc, base, _ = run(["hash", "--store", "project"])
            rc, _, err = run(["stage", "--store", "project", "--resume", "--base", base.strip()],
                             "".join("- line %04d\n" % i for i in range(999)))  # 11,988 chars: under 12000, path-independent
            check("a RESUME under the budget that Codex's boot pack would drop is refused at stage",
                  rc == 2 and "would not load in Codex" in err and "codex_boot_bytes" in err, (rc, err))

            # Lock contention: another holder makes commit wait, then time out without writing.
            run(["stage", "--store", "global", "--file", "user_y.md"], "body\n")
            reflect_lock.try_acquire("codex-other", "other", os.getpid(), "shell")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = commit(timeout=0)
            check("held lock: commit waits and writes nothing", rc == 2
                  and not os.path.exists(os.path.join(g, "user_y.md")))
            os.unlink(reflect_lock.lock_path())
            check("queued item survives a timeout", run(["commit"])[0] == 0
                  and os.path.isfile(os.path.join(g, "user_y.md")))
            check("nothing under claude_home", not os.path.exists(os.path.join(d, "claude")))

            os.environ.pop("CODEX_THREAD_ID")
            rc, _, err = run(["commit"])
            check("no thread id refused", rc == 2 and "CODEX_THREAD_ID" in err)
        finally:
            os.chdir(here)
            queue_root = real_queue_root
            reflect_lock.LOCK = None
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
