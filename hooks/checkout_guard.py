#!/usr/bin/env python3
"""checkout_guard.py -- PreToolUse guard for Claude Code and Codex: keep work out of the
main checkout and keep a dirty tree from being wiped.

Reads one hook payload (JSON: tool_name, tool_input, cwd) on stdin. To refuse it prints one
paragraph to stderr and exits 2 (the PreToolUse blocking convention), nothing on stdout. To
allow it exits 0 silently.

ARM A, edits. Edit, Write, MultiEdit, NotebookEdit and Codex apply_patch are refused when the
target is a TRACKED file of a repo whose MAIN checkout is on the default branch. The fix is
to work in a worktree (`wt new <name>`). Never refused: a new untracked file, a file in a
linked worktree (whatever its branch), a main checkout on a non-default branch or a detached
HEAD, anything outside a git repo.

ARM B, shell commands (Bash, and the Codex shell tools). Refused:
  * any raw `gh pr merge` invocation (not `--help`); `wt merge` is the gated way. Only an
    invocation counts: `echo "gh pr merge"` and a commit message that names it do not.
  * on a tree with uncommitted changes: `git checkout -f`, `git checkout -- <paths>`,
    `git checkout .`, `git restore` (except a plain `--staged`), `git reset --hard`, and
    `git clean` with force and -d (the tree counts untracked files for clean). On a clean
    tree they pass.

EXEMPTIONS come from the config only ([checkout_guard] exempt_repos / exempt_paths, plus every
scope's memory store for edits). There is NO environment variable, flag or command-line comment
that turns the guard off; the hook reads no environment variable except HOME and
SESSION_NOTES_CONFIG (through notes_config).

FAILS OPEN: an unparseable payload, a git failure or a broken config exits 0 with one stderr
line `checkout_guard: <error>`. A config that fails to parse says so by file name.

    python3 checkout_guard.py --selftest
"""
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BIN = os.path.join(ROOT, "bin")
sys.path.insert(0, BIN)
try:
    import notes_config  # noqa: E402
except Exception as _exc:  # fail open, reported by main()
    notes_config = None
    _IMPORT_ERROR = str(_exc)

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
PATCH_TOOLS = ("apply_patch",)
# UNVERIFIED until a live Codex run: the names Codex gives its shell tool. Claude's is "Bash".
SHELL_TOOLS = ("shell", "exec_command", "local_shell", "container.exec", "Bash")

SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
WRAPPERS = ("command", "env", "sudo", "time", "nohup", "exec")
_WRAPPER_VALUE_FLAGS = ("-u", "-g", "-h", "-p", "-U", "-C", "-D", "-R", "-T")
_PATCH_RE = re.compile(r"^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+?)\s*$", re.M)
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC_RE = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
_MERGE_FALLBACK_RE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:(?:command|env|sudo|time|nohup|exec)\s+)*"
    r"(?:\S*/)?gh\s+(?:(?:-R|--repo)\s+\S+\s+)*pr\s+merge\b")


class Fail(Exception):
    pass


# ------------------------------------------------------------------ git helpers
def _git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True,
                          timeout=8, env=env)


def _nearest_dir(p):
    d = p
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return d or "/"


def repo_info(path):
    """(toplevel, common git dir), both real paths, for the repo holding `path`; None if none."""
    d = _nearest_dir(path)
    r = _git(d, "rev-parse", "--show-toplevel", "--git-common-dir")
    if r.returncode != 0:
        return None
    lines = r.stdout.splitlines()
    if len(lines) < 2:
        return None
    common = lines[1]
    if not os.path.isabs(common):
        common = os.path.join(d, common)
    return os.path.realpath(lines[0]), os.path.realpath(common)


def is_main_checkout(top, common):
    return os.path.basename(common) == ".git" and os.path.dirname(common) == top


def current_branch(top):
    r = _git(top, "symbolic-ref", "--short", "-q", "HEAD")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def on_default_branch(top, branch):
    """origin/HEAD's branch if set (the wt.default_branch rule), else main; master also
    counts when origin/HEAD is unset and the checkout is on master."""
    r = _git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    name = r.stdout.strip()
    if r.returncode == 0 and name.startswith("origin/"):
        return branch == name[len("origin/"):]
    return branch in ("main", "master")


def is_tracked(top, real):
    return _git(top, "ls-files", "--error-unmatch", "--", real).returncode == 0


def dirty(tree, untracked):
    """True when `tree` has uncommitted changes. None when it is not a git repo."""
    d = _nearest_dir(tree)
    r = _git(d, "status", "--porcelain", "--untracked-files=" + ("normal" if untracked else "no"))
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def wt_command():
    if shutil.which("wt"):
        return "wt"
    return "python3 " + os.path.join(BIN, "wt.py")


# ------------------------------------------------------------------ exemptions
def repo_exempt(cfg, top):
    return any(notes_config._under(top, r) for r in cfg["checkout_guard"]["exempt_repos"])


def path_exempt(cfg, real):
    stores = [s["store"] for s in cfg["scopes"]]
    if cfg["paths"].get("global_store"):
        stores.append(cfg["paths"]["global_store"])
    if any(notes_config._under(real, s) for s in stores if s):
        return True
    return any(fnmatch.fnmatch(real, g) for g in cfg["checkout_guard"]["exempt_paths"])


# ------------------------------------------------------------------ Arm A
def patch_paths(text):
    return _PATCH_RE.findall(text or "")


def check_edit(paths, cwd, cfg):
    for p in paths:
        ap = p if os.path.isabs(p) else os.path.join(cwd, p)
        real = os.path.realpath(ap)
        info = repo_info(os.path.dirname(real))
        if not info:
            continue
        top, common = info
        if repo_exempt(cfg, top) or path_exempt(cfg, real):
            continue
        if not is_main_checkout(top, common):
            continue
        branch = current_branch(top)
        if not branch or not on_default_branch(top, branch):
            continue
        if not is_tracked(top, real):
            continue
        return ("checkout_guard: %s is tracked on %s in %s; edits go in a worktree: run %s new "
                "<name> first, then edit there." % (real, branch, top, wt_command()))
    return None


# ------------------------------------------------------------------ Arm B
def strip_heredocs(cmd):
    """Drop heredoc bodies (the operator line stays). An unterminated one is left alone."""
    lines = cmd.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        pending = [(m.group(1) == "-", m.group(2) or m.group(3) or m.group(4))
                   for m in _HEREDOC_RE.finditer(line)]
        for dash, term in pending:
            j = i
            while j < len(lines):
                body = lines[j].lstrip("\t") if dash else lines[j]
                if body == term:
                    break
                j += 1
            if j < len(lines):
                i = j + 1
    return "\n".join(out)


def split_segments(cmd):
    """Split on unquoted ; & | newline ( ) and backtick; drop unquoted comments."""
    segs, buf, q, i, n = [], [], None, 0, len(cmd)

    def flush():
        s = "".join(buf).strip()
        del buf[:]
        if s:
            segs.append(s)
    while i < n:
        c = cmd[i]
        if q:
            buf.append(c)
            if q == '"' and c == "\\" and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] == "\n":
                buf.append(" ")
            else:
                buf.append(c + cmd[i + 1])
            i += 2
            continue
        if c in "\"'":
            q = c
            buf.append(c)
        elif c == "#" and (not buf or buf[-1].isspace()):
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        elif c in ";&|\n()`":
            flush()
        else:
            buf.append(c)
        i += 1
    flush()
    return segs


def strip_prefix(argv):
    """Drop env assignments and wrapper commands in front of the real command."""
    while argv:
        tok = argv[0]
        if _ASSIGN_RE.match(tok) or tok == "{":
            argv = argv[1:]
        elif tok in WRAPPERS:
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or _ASSIGN_RE.match(argv[0])):
                skip = 2 if argv[0] in _WRAPPER_VALUE_FLAGS else 1
                argv = argv[skip:]
        else:
            break
    return argv


def _skip_gh_flags(args, i):
    while i < len(args):
        t = args[i]
        if t in ("-R", "--repo"):
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            break
    return i


def is_gh_pr_merge(args):
    """args = argv after `gh`. True for an invocation of `pr merge` that is not --help."""
    i = _skip_gh_flags(args, 0)
    if i >= len(args) or args[i] != "pr":
        return False
    i = _skip_gh_flags(args, i + 1)
    if i >= len(args) or args[i] != "merge":
        return False
    return not any(t in ("--help", "-h") for t in args[i + 1:])


def _flags(args):
    """(long flags, short letters, positionals, saw `--`) of a git subcommand's args."""
    longs, shorts, pos, dd = set(), set(), [], False
    for t in args:
        if dd:
            pos.append(t)
        elif t == "--":
            dd = True
        elif t.startswith("--"):
            longs.add(t.split("=", 1)[0])
        elif t.startswith("-") and len(t) > 1:
            shorts.update(t[1:])
        else:
            pos.append(t)
    return longs, shorts, pos, dd


def destructive_verb(sub, args):
    """(verb text, needs untracked in the dirty check) when this git call can destroy
    uncommitted work, else None."""
    longs, shorts, pos, dd = _flags(args)
    if sub == "checkout":
        if "--force" in longs or "f" in shorts:
            return "git checkout -f", False
        if dd or pos == ["."]:
            return "git checkout (paths)", False
    elif sub == "restore":
        staged = "--staged" in longs or "S" in shorts
        worktree = "--worktree" in longs or "W" in shorts
        if not (staged and not worktree):
            return "git restore", False
    elif sub == "reset":
        if "--hard" in longs:
            return "git reset --hard", False
    elif sub == "clean":
        force = "--force" in longs or "f" in shorts
        dryrun = "--dry-run" in longs or "n" in shorts
        if force and "d" in shorts and not dryrun:
            return "git clean -fd", True
    return None


def _dirty_path_operand(tree, args):
    """True for `git checkout <path>` (no `--`) where an operand is not a commit-ish, names an
    existing path, and that path has tracked changes. A real branch name wins, as in git."""
    _, _, pos, dd = _flags(args)
    if dd:
        return False
    d = _nearest_dir(tree)
    for a in pos:
        if _git(d, "rev-parse", "--verify", "-q", a + "^{commit}").returncode == 0:
            continue
        if not os.path.exists(os.path.join(d, a)):
            continue
        r = _git(d, "status", "--porcelain", "--untracked-files=no", "--", a)
        if r.returncode == 0 and r.stdout.strip():
            return True
    return False


def _git_target(argv, tree):
    """(subcommand, its args, tree) of a `git ...` argv, honoring -C."""
    i = 1
    while i < len(argv):
        t = argv[i]
        if t == "-C" and i + 1 < len(argv):
            d = os.path.expanduser(argv[i + 1])
            tree = d if os.path.isabs(d) else os.path.join(tree, d)
            i += 2
        elif t in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(argv):
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            return t, argv[i + 1:], tree
    return None, [], tree


def _script_of(argv):
    """The script of `bash -c "<script>"` (flags like -lc accepted), else None."""
    for k in range(1, len(argv) - 1):
        t = argv[k]
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return argv[k + 1]
        if not t.startswith("-"):
            return None
    return None


def check_command(cmd, cwd, cfg, depth=0):
    wt = wt_command()
    for seg in split_segments(strip_heredocs(cmd)):
        try:
            argv = shlex.split(seg)
        except ValueError:
            if _MERGE_FALLBACK_RE.match(seg) and not _exempt_cwd(cfg, cwd):
                return _merge_msg(wt)
            continue
        argv = strip_prefix(argv)
        if not argv:
            continue
        name = os.path.basename(argv[0])
        if name in ("cd", "pushd"):
            if len(argv) > 1 and not argv[1].startswith("-"):
                d = os.path.expanduser(argv[1])
                cwd = os.path.normpath(d if os.path.isabs(d) else os.path.join(cwd, d))
            continue
        if name in SHELLS and depth < 3:
            script = _script_of(argv)
            if script:
                r = check_command(script, cwd, cfg, depth + 1)
                if r:
                    return r
            continue
        if name == "gh":
            if is_gh_pr_merge(argv[1:]) and not _exempt_cwd(cfg, cwd):
                return _merge_msg(wt)
        elif name == "git":
            sub, args, tree = _git_target(argv, cwd)
            hit = destructive_verb(sub, args) if sub else None
            if not hit and sub == "checkout" and _dirty_path_operand(tree, args):
                hit = ("git checkout <path>", False)
            if not hit:
                continue
            verb, untracked = hit
            if _exempt_cwd(cfg, tree) or not dirty(tree, untracked):
                continue
            return ("checkout_guard: `%s` would destroy uncommitted changes: the tree at %s has "
                    "%s. Commit them first (a WIP commit is fine) or save a copy, then retry."
                    % (verb, tree, "uncommitted or untracked files" if untracked
                       else "uncommitted changes"))
    return None


def _exempt_cwd(cfg, d):
    info = repo_info(d)
    return bool(info) and repo_exempt(cfg, info[0])


def _merge_msg(wt):
    return ("checkout_guard: raw `gh pr merge` is refused; run %s merge instead (it checks the "
            "review verdict, merges pinned to the gated head and removes the worktree)." % wt)


# ------------------------------------------------------------------ payload
def _shell_command(tool_input):
    raw = tool_input.get("command", tool_input.get("cmd"))
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and raw and all(isinstance(x, str) for x in raw):
        script = _script_of(raw) if os.path.basename(raw[0]) in SHELLS else None
        return script if script else shlex.join(raw)
    return None


def decide(payload):
    """The refusal text for a payload, or None to allow. May raise (callers fail open)."""
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name")
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") \
        else os.getcwd()
    if tool in EDIT_TOOLS:
        paths = [ti.get("notebook_path" if tool == "NotebookEdit" else "file_path")]
    elif tool in PATCH_TOOLS:
        raw = ti.get("command", ti.get("input"))
        if isinstance(raw, list):
            raw = raw[-1] if raw else ""
        paths = patch_paths(raw if isinstance(raw, str) else "")
    elif tool in SHELL_TOOLS:
        paths = None
    else:
        return None
    paths = [p for p in (paths or []) if isinstance(p, str) and p]
    if tool in SHELL_TOOLS:
        wd = ti.get("workdir")
        if isinstance(wd, str) and wd:
            cwd = wd if os.path.isabs(wd) else os.path.join(cwd, wd)
        cmd = _shell_command(ti)
        if not cmd:
            return None
    elif not paths:
        return None
    if notes_config is None:
        raise Fail("cannot import notes_config: %s" % _IMPORT_ERROR)
    try:
        cfg = notes_config.load()
    except notes_config.ConfigError as exc:
        sys.stderr.write("checkout_guard: LOUD: config %s failed to parse (%s); the guard is "
                         "OFF for this call, fix the file\n" % (notes_config.config_path(), exc))
        return None
    if tool in SHELL_TOOLS:
        # An apply_patch run through the shell is an edit too.
        if _runs_apply_patch(cmd):
            r = check_edit(patch_paths(cmd), cwd, cfg)
            if r:
                return r
        return check_command(cmd, cwd, cfg)
    return check_edit(paths, cwd, cfg)


def _runs_apply_patch(cmd):
    for seg in split_segments(strip_heredocs(cmd)):
        try:
            argv = strip_prefix(shlex.split(seg))
        except ValueError:
            continue
        if argv and os.path.basename(argv[0]) == "apply_patch":
            return True
    return False


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        sys.stderr.write("checkout_guard: unreadable payload: %s\n" % exc)
        return 0
    try:
        msg = decide(payload)
    except Exception as exc:
        sys.stderr.write("checkout_guard: %s: %s\n" % (type(exc).__name__, exc))
        return 0
    if msg:
        sys.stderr.write(msg + "\n")
        return 2
    return 0


# ------------------------------------------------------------------ selftest
def selftest():
    import tempfile
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + detail))
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and k not in ("TMUX", "TMUX_PANE")}
        env["HOME"] = home
        env["SESSION_NOTES_CONFIG"] = os.path.join(tmp, "config.toml")
        gitenv = dict(env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                      GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def sh(cwd, *args):
            subprocess.run(args, cwd=cwd, env=gitenv, check=True, capture_output=True)

        def mkrepo(name, files=("a.txt",), branch="main"):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            sh(d, "git", "init", "-q", "-b", branch)
            for f in files:
                os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
                with open(os.path.join(d, f), "w") as fh:
                    fh.write("x\n")
            sh(d, "git", "add", "-A")
            sh(d, "git", "commit", "-q", "-m", "init")
            return d

        main_repo = mkrepo("main_repo", ("a.txt", "docs/g.txt"))
        wt1 = os.path.join(tmp, "wt1")
        sh(main_repo, "git", "worktree", "add", "-q", "-b", "feat", wt1)
        memrepo = mkrepo("memrepo", ("mem/note.md",))
        exrepo = mkrepo("exrepo", ("b.txt",))
        devrepo = mkrepo("devrepo", ("a.txt",), branch="dev")
        dirty_repo = mkrepo("dirty")
        with open(os.path.join(dirty_repo, "a.txt"), "w") as fh:
            fh.write("changed\n")
        clean_repo = mkrepo("clean")
        untr_repo = mkrepo("untr")
        with open(os.path.join(untr_repo, "new.txt"), "w") as fh:
            fh.write("n\n")
        nongit = os.path.join(tmp, "plain")
        os.makedirs(nongit)
        with open(os.path.join(nongit, "f.txt"), "w") as fh:
            fh.write("x\n")
        with open(env["SESSION_NOTES_CONFIG"], "w") as fh:
            fh.write('[checkout_guard]\nexempt_repos = ["%s"]\nexempt_paths = ["%s/docs/*"]\n'
                     '[[scopes]]\nname = "s"\nroot = "%s/scoperoot"\nstore = "%s/mem"\n'
                     % (exrepo, main_repo, tmp, memrepo))

        def run(payload, stdin=None):
            r = subprocess.run([sys.executable, os.path.realpath(__file__)], env=env,
                               input=stdin if stdin is not None else json.dumps(payload),
                               capture_output=True, text=True, timeout=30)
            return r

        def refused(r):
            return r.returncode == 2 and r.stdout == "" and r.stderr.startswith("checkout_guard:")

        def passed(r):
            return r.returncode == 0 and r.stdout == "" and r.stderr == ""

        def edit(tool, path, cwd=None):
            key = "notebook_path" if tool == "NotebookEdit" else "file_path"
            return run({"tool_name": tool, "tool_input": {key: path}, "cwd": cwd or tmp})

        def bash(cmd, cwd, tool="Bash"):
            return run({"tool_name": tool, "tool_input": {"command": cmd}, "cwd": cwd})

        # Arm A refuse
        r = edit("Edit", os.path.join(main_repo, "a.txt"))
        check("A refuse: Edit tracked file on main", refused(r), r.stderr)
        check("A refuse: text names a wt new command",
              re.search(r"(wt|python3 \S*wt\.py) new <name>", r.stderr) is not None
              and "a.txt" in r.stderr, r.stderr)
        check("A refuse: Write over tracked file",
              refused(edit("Write", os.path.join(main_repo, "a.txt"))))
        check("A refuse: MultiEdit tracked file",
              refused(edit("MultiEdit", os.path.join(main_repo, "a.txt"))))
        check("A refuse: relative path resolved against cwd",
              refused(run({"tool_name": "Edit", "tool_input": {"file_path": "a.txt"},
                           "cwd": main_repo})))
        patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-x\n+y\n*** End Patch\n"
        check("A refuse: apply_patch Update File (string)",
              refused(run({"tool_name": "apply_patch", "tool_input": {"command": patch},
                           "cwd": main_repo})))
        check("A refuse: apply_patch (argv list, last element)",
              refused(run({"tool_name": "apply_patch",
                           "tool_input": {"command": ["apply_patch", patch]}, "cwd": main_repo})))
        mixed = ("*** Begin Patch\n*** Add File: fresh.txt\n+x\n*** Update File: a.txt\n"
                 "@@\n-x\n+y\n*** End Patch\n")
        r = run({"tool_name": "apply_patch", "tool_input": {"command": mixed}, "cwd": main_repo})
        check("A refuse: apply_patch refuses if ANY target qualifies, names it",
              refused(r) and "a.txt" in r.stderr, r.stderr)

        # Arm A pass
        check("A pass: same file in a linked worktree",
              passed(edit("Edit", os.path.join(wt1, "a.txt"))))
        check("A pass: apply_patch in a linked worktree",
              passed(run({"tool_name": "apply_patch", "tool_input": {"command": patch},
                          "cwd": wt1})))
        check("A pass: new untracked file on main",
              passed(edit("Write", os.path.join(main_repo, "brand_new.txt"))))
        check("A pass: new file in a not-yet-existing dir on main",
              passed(edit("Write", os.path.join(main_repo, "nodir", "x.txt"))))
        check("A pass: non-git dir", passed(edit("Edit", os.path.join(nongit, "f.txt"))))
        check("A pass: file under a scope memory store",
              passed(edit("Edit", os.path.join(memrepo, "mem", "note.md"))))
        check("A pass: exempt_paths glob",
              passed(edit("Edit", os.path.join(main_repo, "docs", "g.txt"))))
        check("A pass: exempt_repos repo", passed(edit("Edit", os.path.join(exrepo, "b.txt"))))
        check("A pass: main checkout on a non-default branch",
              passed(edit("Edit", os.path.join(devrepo, "a.txt"))))
        check("A pass: unknown tool name", passed(run({"tool_name": "Read",
              "tool_input": {"file_path": os.path.join(main_repo, "a.txt")}, "cwd": tmp})))

        # Arm B refuse
        for cmd in ("gh pr merge 12 --squash", "FOO=1 gh pr merge", "cd x && gh pr merge",
                    "gh -R o/r pr merge 3", "gh pr -R o/r merge 3", "command gh pr merge 1",
                    "sudo -u bob gh pr merge 1", "echo hi; gh pr merge 1 | cat",
                    "bash -c 'gh pr merge 1'", "gh pr merge 1 # fine"):
            r = bash(cmd, nongit)
            check("B refuse: " + cmd, refused(r) and "merge" in r.stderr, r.stderr)
        r = bash("gh pr merge 1", nongit)
        check("B refuse: text points at wt merge",
              re.search(r"(wt|python3 \S*wt\.py) merge", r.stderr) is not None, r.stderr)
        for cmd in ("git checkout -f main", "git checkout --force main", "git checkout -- a.txt",
                    "git checkout main -- a.txt", "git checkout .", "git restore a.txt",
                    "git restore --staged --worktree a.txt", "git reset --hard", "git clean -fd",
                    "git clean -d -f", "git clean -f -d", "git clean -fdx", "git clean --force -d",
                    "git reset --hard HEAD~1 && echo ok"):
            r = bash(cmd, dirty_repo)
            check("B refuse (dirty): " + cmd,
                  refused(r) and "uncommitted" in r.stderr and "commit" in r.stderr, r.stderr)
        check("B refuse (dirty): git -C <repo> from an unrelated cwd",
              refused(bash("git -C %s reset --hard" % dirty_repo, nongit)))
        check("B refuse (dirty): cd <repo> && git reset --hard",
              refused(bash("cd %s && git reset --hard" % dirty_repo, nongit)))
        check("B refuse (untracked only): git clean -fd",
              refused(bash("git clean -fd", untr_repo)))

        # Arm B pass
        for cmd in ("gh pr merge --help", "gh pr merge -h", "echo \"gh pr merge\"",
                    "grep 'gh pr merge' notes.txt", "git commit -m \"use gh pr merge\"",
                    "cat <<'EOF'\ngh pr merge 1\nEOF", "cat <<-EOF > f\n\tgh pr merge 1\n\tEOF\necho done",
                    "gh pr view 3", "gh pr list # gh pr merge"):
            check("B pass: " + cmd.replace("\n", "\\n"), passed(bash(cmd, nongit)))
        check("B pass: heredoc then a real command still parsed",
              refused(bash("cat <<EOF\nhi\nEOF\ngh pr merge 1", nongit)))
        for cmd in ("git checkout -f main", "git reset --hard", "git clean -fd",
                    "git checkout -- a.txt", "git restore a.txt"):
            check("B pass (clean tree): " + cmd, passed(bash(cmd, clean_repo)))
        check("B pass (dirty): git restore --staged a.txt",
              passed(bash("git restore --staged a.txt", dirty_repo)))
        check("B pass (dirty): git checkout otherbranch",
              passed(bash("git checkout otherbranch", dirty_repo)))
        check("B refuse (dirty): bare git checkout a.txt",
              refused(bash("git checkout a.txt", dirty_repo)))
        sh(clean_repo, "git", "branch", "otherbranch")
        with open(os.path.join(clean_repo, "b.txt"), "w") as fh:
            fh.write("b\n")
        sh(clean_repo, "git", "add", "b.txt")
        sh(clean_repo, "git", "commit", "-q", "-m", "b")
        with open(os.path.join(clean_repo, "b.txt"), "w") as fh:
            fh.write("dirty\n")
        check("B pass: bare git checkout a.txt when a.txt is clean but b.txt is dirty",
              passed(bash("git checkout a.txt", clean_repo)))
        check("B pass: bare git checkout of a branch name on a dirty tree",
              passed(bash("git checkout otherbranch", clean_repo)))
        check("B pass (dirty): git checkout -b newbranch",
              passed(bash("git checkout -b newbranch", dirty_repo)))
        check("B pass (dirty): git clean -n -d -f (dry run)",
              passed(bash("git clean -ndf", dirty_repo)))
        check("B pass (untracked only): git reset --hard",
              passed(bash("git reset --hard", untr_repo)))
        check("B pass: not a git repo", passed(bash("git reset --hard", nongit)))
        check("B pass: gh pr merge in an exempt repo", passed(bash("gh pr merge 1", exrepo)))

        # No override
        for cmd in ("CHECKOUT_GUARD=off gh pr merge 1", "gh pr merge 1 # checkout_guard: allow",
                    "# checkout_guard: allow\ngh pr merge 1", "env CHECKOUT_GUARD=0 gh pr merge 1"):
            check("no override: " + cmd.replace("\n", "\\n"), refused(bash(cmd, nongit)))
        guard_env = dict(env, CHECKOUT_GUARD="off", CHECKOUT_GUARD_DISABLE="1",
                         SESSION_NOTES_GUARD="off")
        r = subprocess.run([sys.executable, os.path.realpath(__file__)], env=guard_env,
                           input=json.dumps({"tool_name": "Bash", "cwd": nongit,
                                             "tool_input": {"command": "gh pr merge 1"}}),
                           capture_output=True, text=True)
        check("no override: environment variables change nothing", refused(r))

        # Fail open
        r = run(None, stdin="this is not json {")
        check("fail open: garbage stdin", r.returncode == 0 and r.stdout == ""
              and r.stderr.startswith("checkout_guard:"), r.stderr)
        r = run(None, stdin="")
        check("fail open: empty stdin", r.returncode == 0)
        check("fail open: non-dict payload", passed(run(["x"])))
        check("fail open: tool_input of the wrong type",
              passed(run({"tool_name": "Bash", "tool_input": "oops", "cwd": nongit})))
        bad = os.path.join(tmp, "bad.toml")
        with open(bad, "w") as fh:
            fh.write("this is not toml\n")
        r = subprocess.run([sys.executable, os.path.realpath(__file__)],
                           env=dict(env, SESSION_NOTES_CONFIG=bad),
                           input=json.dumps({"tool_name": "Bash", "cwd": nongit,
                                             "tool_input": {"command": "gh pr merge 1"}}),
                           capture_output=True, text=True)
        check("fail open: broken config exits 0 and names the file loudly",
              r.returncode == 0 and bad in r.stderr and "LOUD" in r.stderr
              and r.stderr.count("\n") == 1, r.stderr)

        # Codex shapes
        r = run({"tool_name": "exec_command", "cwd": nongit,
                 "tool_input": {"command": ["bash", "-lc", "gh pr merge 1"]}})
        check("codex: exec_command argv [bash,-lc,script] refused", refused(r), r.stderr)
        r = run({"tool_name": "shell", "cwd": nongit, "tool_input": {"command": "gh pr merge 1"}})
        check("codex: shell with a string command refused", refused(r), r.stderr)
        r = run({"tool_name": "local_shell", "cwd": dirty_repo,
                 "tool_input": {"command": ["git", "reset", "--hard"]}})
        check("codex: plain argv list joined and parsed", refused(r), r.stderr)
        r = run({"tool_name": "exec_command", "cwd": nongit,
                 "tool_input": {"cmd": "gh pr merge 1", "workdir": nongit}})
        check("codex: `cmd` key accepted", refused(r), r.stderr)
        r = run({"tool_name": "shell", "cwd": nongit,
                 "tool_input": {"command": ["bash", "-lc", "echo gh pr merge"]}})
        check("codex: a mention in an argv script passes", passed(r), r.stderr)
        r = run({"tool_name": "shell", "cwd": main_repo,
                 "tool_input": {"command": ["bash", "-lc", "apply_patch <<'EOF'\n" + patch + "EOF"]}})
        check("codex: apply_patch run through the shell is an edit", refused(r), r.stderr)

    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        sys.stderr.write("checkout_guard: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(0)
