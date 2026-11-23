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

Redirections are stripped before any argument is read. Path-form checkout/restore ask `git
status` over the operands once (git matches globs, directories and `.`); a git error counts as
dirty, never clean.

CONFIG PROTECTION: the config file itself (symlink and target alike) cannot be edited or written
by the agent, in either arm, even when its parse is broken and whatever the exemptions say.

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
# Recorded from Codex 0.171 (tests/fixtures/codex_pretooluse_*.json): its shell tool is named
# "Bash" with tool_input {"command": "<string>"}, and apply_patch carries the whole patch text in
# tool_input {"command": "*** Begin Patch\n..."}. The other names are older/other Codex builds.
SHELL_TOOLS = ("shell", "exec_command", "local_shell", "container.exec", "Bash")

SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
# wrapper -> the flags that take a separate value
WRAPPERS = {"command": (), "env": ("-u", "-C", "-S"), "time": (), "nohup": (), "exec": ("-a",),
            "sudo": ("-u", "-g", "-h", "-p", "-U", "-C", "-D", "-R", "-T")}
# shell keywords that can sit in front of a command
KEYWORDS = ("if", "then", "else", "elif", "do", "while", "until", "!", "{", "(")
# Codex trims every line of a patch before reading it (`line.trim()` in its parser), so a marker
# may be indented: `[^\S\n]*` is whitespace that is not a newline.
_PATCH_RE = re.compile(r"^[^\S\n]*\*\*\* (?:Update File|Add File|Delete File|Move to): (.+?)\s*$", re.M)
APPLY_PATCH_NAMES = ("apply_patch", "applypatch")
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


def has_origin(top):
    return _git(top, "config", "--get", "remote.origin.url").returncode == 0


def is_tracked(top, real):
    return _git(top, "ls-files", "--error-unmatch", "--", real).returncode == 0


def tree_dirty(tree, untracked):
    """True when the repo at `tree` has uncommitted changes. FAIL SAFE: a `git status` that
    errors inside a repo counts as dirty, never clean. Not a repo = False (nothing to lose)."""
    d = _nearest_dir(tree)
    if repo_info(d) is None:
        return False
    r = _git(d, "status", "--porcelain", "--untracked-files=" + ("normal" if untracked else "no"))
    return r.returncode != 0 or bool(r.stdout.strip())


# porcelain XY codes of an unmerged (conflicted) path
UNMERGED = ("DD", "AU", "UD", "UA", "DU", "AA", "UU")


def paths_would_lose(tree, operands, conflict):
    """The ONE predicate for path-form checkout/restore: run `git status` once over the
    operands (git does the glob, directory and `.` matching) and refuse unless it is safe.
    Plain: safe only if git succeeded and printed nothing. Conflict resolution (--ours,
    --theirs, --merge): safe only if git succeeded, printed something, and every line is an
    unmerged code. A git error is never safe. Not a repo = safe (git itself will fail)."""
    d = _nearest_dir(tree)
    if repo_info(d) is None:
        return False
    r = _git(d, "status", "--porcelain", "--untracked-files=no", "--", *operands)
    if r.returncode != 0:
        return True
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if conflict:
        return not (lines and all(ln[:2] in UNMERGED for ln in lines))
    return bool(lines)


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
        if not is_main_checkout(top, common) or not has_origin(top):
            continue  # no origin = no PR flow, and `wt new` needs one
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


_REDIR_OPS = ("<<<", "<<", "<&", "<>", "<", ">>", ">|", ">&", ">")


def _redirect(cmd, i):
    """The redirection operator at cmd[i] ('<' or '>'): (op, target word, index after it)."""
    n = len(cmd)
    op = next(o for o in _REDIR_OPS if cmd.startswith(o, i))
    j = i + len(op)
    while j < n and cmd[j] in " \t":
        j += 1
    k, q = j, None
    while k < n:
        ch = cmd[k]
        if q:
            if q == '"' and ch == "\\" and k + 1 < n:
                k += 2
                continue
            if ch == q:
                q = None
            k += 1
            continue
        if ch == "\\" and k + 1 < n:
            k += 2
            continue
        if ch in "\"'":
            q = ch
            k += 1
            continue
        if ch.isspace() or ch in ";&|<>()`":
            break
        k += 1
    raw = cmd[j:k]
    try:
        parts = shlex.split(raw)
        word = parts[0] if parts else ""
    except ValueError:
        word = raw.strip("\"'")
    return op, word, k


def _is_write_redirect(op, word, amp):
    if amp or op in (">", ">>", ">|", "<>"):
        return True
    return op == ">&" and not re.fullmatch(r"\d+-?|-", word)


def split_segments(cmd):
    """Split on unquoted ; & | newline ( ) and backtick; drop unquoted comments. Redirections
    (`>`, `>>`, `<`, `<<<`, `&>`, `N>`, `2>&1`, `>|`, attached or detached) are removed from the
    text and their write targets returned: a list of (text, write targets) per segment."""
    segs, buf, writes, q, i, n = [], [], [], None, 0, len(cmd)

    def flush():
        s = "".join(buf).strip()
        w = list(writes)
        del buf[:]
        del writes[:]
        if s or w:
            segs.append((s, w))
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
        elif c in "<>" or (c == "&" and i + 1 < n and cmd[i + 1] == ">"):
            amp = c == "&"
            pre = "".join(buf)
            m = re.search(r"(?:^|\s)(\d+)$", pre)
            if m and not amp:  # an fd number in front: `2>x`
                del buf[len(pre) - len(m.group(1)):]
            op, word, i = _redirect(cmd, i + 1 if amp else i)
            if _is_write_redirect(op, word, amp) and word:
                writes.append(word)
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
        if _ASSIGN_RE.match(tok) or tok in KEYWORDS:
            argv = argv[1:]
        elif tok in WRAPPERS:
            valued = WRAPPERS[tok]
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or _ASSIGN_RE.match(argv[0])):
                skip = 2 if argv[0] in valued else 1
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


def _is_commit(d, tok):
    return _git(d, "rev-parse", "--verify", "-q", tok + "^{commit}").returncode == 0


def _checkout_operands(d, args):
    """Pathspec operands of `git checkout`: everything after `--`, plus bare operands before it
    that are not a commit-ish. The values of -b/-B/--orphan are names, not paths."""
    ops, skip, dd = [], False, False
    for t in args:
        if skip:
            skip = False
        elif dd:
            ops.append(t)
        elif t == "--":
            dd = True
        elif t in ("-b", "-B", "--orphan"):
            skip = True
        elif t.startswith("-") and len(t) > 1:
            continue
        elif not _is_commit(d, t):
            ops.append(t)
    return ops


def _restore_operands(args):
    ops, skip, dd = [], False, False
    for t in args:
        if skip:
            skip = False
        elif dd:
            ops.append(t)
        elif t == "--":
            dd = True
        elif t in ("-s", "--source"):
            skip = True
        elif t.startswith("-") and len(t) > 1:
            continue
        else:
            ops.append(t)
    return ops


def git_verdict(sub, args, tree):
    """The verb text when this git call would destroy uncommitted work in `tree`, else None.
    Path forms (checkout/restore) ask `paths_would_lose`; whole-tree verbs ask `tree_dirty`."""
    longs, shorts, pos, dd = _flags(args)
    d = _nearest_dir(tree)
    if sub in ("checkout", "restore"):
        conflict = bool(longs & {"--ours", "--theirs", "--merge"}) or (
            sub == "checkout" and "m" in shorts)
        if sub == "checkout":
            ops = _checkout_operands(d, args)
        else:
            if ("--staged" in longs or "S" in shorts) and not (
                    "--worktree" in longs or "W" in shorts):
                return None  # only unstages
            ops = _restore_operands(args)
        if ops and conflict:
            return "git %s (paths)" % sub if paths_would_lose(d, ops, True) else None
        if sub == "checkout" and ("--force" in longs or "f" in shorts):
            return "git checkout -f" if tree_dirty(d, False) else None
        if ops:
            return "git %s (paths)" % sub if paths_would_lose(d, ops, False) else None
        if sub == "restore":  # no operand we can read (e.g. --pathspec-from-file): whole tree
            return "git restore" if tree_dirty(d, False) else None
        return None
    if sub == "switch":
        if "--force" in longs or "--discard-changes" in longs or "f" in shorts:
            return "git switch -f" if tree_dirty(d, False) else None
    elif sub == "reset":
        if "--hard" in longs:
            return "git reset --hard" if tree_dirty(d, False) else None
    elif sub == "clean":
        force = "--force" in longs or "f" in shorts
        dryrun = "--dry-run" in longs or "n" in shorts
        ignored_only = "X" in shorts and "x" not in shorts
        if force and "d" in shorts and not dryrun and not ignored_only:
            return "git clean -fd" if tree_dirty(d, True) else None
    return None


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


def _config_msg(path):
    return ("checkout_guard: %s is the session-notes config, which holds this guard's own "
            "exemptions. It is edited by the user, not the agent: ask the user to change it."
            % path)


def protected_paths():
    """Real paths of the config file (a symlink and its target resolve to the same one). Known
    even when the file fails to parse. Empty only if notes_config could not be imported."""
    if notes_config is None:
        return set()
    return {os.path.realpath(os.path.abspath(notes_config.config_path()))}


def is_protected(path, cwd, protected):
    if not path or not protected:
        return False
    p = os.path.expanduser(path)
    p = p if os.path.isabs(p) else os.path.join(cwd, p)
    return os.path.realpath(p) in protected


def _config_write(argv, writes, cwd, protected):
    """The refusal when this segment writes to the config: a redirect target, tee, sed/perl -i,
    cp/install/ln (destination), mv (either end), rm/truncate/unlink, dd of=."""
    cands = list(writes)
    if argv:
        name, a = os.path.basename(argv[0]), argv[1:]
        ops = [t for t in a if not t.startswith("-")]
        if name == "tee":
            cands += ops
        elif name in ("sed", "perl"):
            if any((t.startswith("-") and not t.startswith("--") and "i" in t[1:])
                   or t.startswith("--in-place") for t in a):
                cands += a
        elif name in ("cp", "install", "ln"):
            cands += ops[-1:]
        elif name == "mv" or name in ("rm", "truncate", "unlink"):
            cands += ops
        elif name == "dd":
            cands += [t[3:] for t in a if t.startswith("of=")]
    for c in cands:
        if is_protected(c, cwd, protected):
            return _config_msg(os.path.realpath(os.path.expanduser(c)))
    return None


def check_command(cmd, cwd, cfg, protected, depth=0):
    """Refusal text for a shell command, or None. cfg None (config failed to parse) leaves only
    the config-file protection active."""
    wt = wt_command()
    for seg, writes in split_segments(strip_heredocs(cmd)):
        msg = _config_write([], writes, cwd, protected)
        if msg:
            return msg
        try:
            argv = shlex.split(seg)
        except ValueError:
            if cfg is not None and _MERGE_FALLBACK_RE.match(seg) and not _exempt_cwd(cfg, cwd):
                return _merge_msg(wt)
            continue
        argv = strip_prefix(argv)
        msg = _config_write(argv, [], cwd, protected)
        if msg:
            return msg
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
                r = check_command(script, cwd, cfg, protected, depth + 1)
                if r:
                    return r
            continue
        if name in APPLY_PATCH_NAMES:
            # an apply_patch run through the shell is an edit too (judged from the cd-adjusted cwd)
            pp = patch_paths(cmd)
            for p in pp:
                if is_protected(p, cwd, protected):
                    return _config_msg(os.path.realpath(os.path.join(cwd, os.path.expanduser(p))))
            r = check_edit(pp, cwd, cfg) if cfg is not None else None
            if r:
                return r
            continue
        if cfg is None:
            continue
        if name == "gh":
            if is_gh_pr_merge(argv[1:]) and not _exempt_cwd(cfg, cwd):
                return _merge_msg(wt)
        elif name == "git":
            sub, args, tree = _git_target(argv, cwd)
            verb = git_verdict(sub, args, tree) if sub else None
            if not verb or _exempt_cwd(cfg, tree):
                continue
            return ("checkout_guard: `%s` would destroy uncommitted changes: the tree at %s has "
                    "uncommitted changes. Commit them first (a WIP commit is fine) or save a "
                    "copy, then retry." % (verb, tree))
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
    # The config file is protected FIRST: before the config is even loaded (so a config that
    # fails to parse cannot make itself editable) and before any exemption.
    protected = protected_paths()
    for p in paths:
        if is_protected(p, cwd, protected):
            return _config_msg(os.path.realpath(os.path.join(cwd, os.path.expanduser(p))))
    cfg = None
    try:
        cfg = notes_config.load()
    except notes_config.ConfigError as exc:
        sys.stderr.write("checkout_guard: LOUD: config %s failed to parse (%s); the guard is "
                         "OFF for this call except for protecting that file, fix it\n"
                         % (notes_config.config_path(), exc))
    if tool in SHELL_TOOLS:
        return check_command(cmd, cwd, cfg, protected)
    return check_edit(paths, cwd, cfg) if cfg is not None else None


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

        def mkrepo(name, files=("a.txt",), branch="main", origin=True):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            sh(d, "git", "init", "-q", "-b", branch)
            for f in files:
                os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
                with open(os.path.join(d, f), "w") as fh:
                    fh.write("x\n")
            sh(d, "git", "add", "-A")
            sh(d, "git", "commit", "-q", "-m", "init")
            if origin:
                bare = os.path.join(tmp, "origins", name + ".git")
                os.makedirs(bare)
                sh(bare, "git", "init", "-q", "--bare", "-b", branch)
                sh(d, "git", "remote", "add", "origin", bare)
                sh(d, "git", "push", "-q", "-u", "origin", branch)
                sh(d, "git", "remote", "set-head", "origin", "-a")
            return d

        main_repo = mkrepo("main_repo", ("a.txt", "docs/g.txt", "pkg/inner/m.py"))
        wt1 = os.path.join(tmp, "wt1")
        sh(main_repo, "git", "worktree", "add", "-q", "-b", "feat", wt1)
        memrepo = mkrepo("memrepo", ("mem/note.md",))
        exrepo = mkrepo("exrepo", ("b.txt",))
        devrepo = mkrepo("devrepo", ("a.txt",))
        sh(devrepo, "git", "checkout", "-q", "-b", "dev")
        noorigin = mkrepo("noorigin", ("a.txt",), origin=False)
        devmain = mkrepo("devmain", ("a.txt",))
        sh(devmain, "git", "checkout", "-q", "-b", "dev")
        wt_on_main = os.path.join(tmp, "wt_on_main")
        sh(devmain, "git", "worktree", "add", "-q", wt_on_main, "main")
        conf = mkrepo("conf", ("a.txt", "b.txt", "c.txt", "app/[id]/page.tsx"))
        sh(conf, "git", "checkout", "-q", "-b", "other")
        for conflicted in ("a.txt", "app/[id]/page.tsx"):
            with open(os.path.join(conf, conflicted), "w") as fh:
                fh.write("theirs\n")
        sh(conf, "git", "commit", "-q", "-am", "o")
        sh(conf, "git", "checkout", "-q", "main")
        for conflicted in ("a.txt", "app/[id]/page.tsx"):
            with open(os.path.join(conf, conflicted), "w") as fh:
                fh.write("ours\n")
        sh(conf, "git", "commit", "-q", "-am", "m")
        subprocess.run(["git", "merge", "other"], cwd=conf, env=gitenv, capture_output=True)
        for dirty_name in ("b.txt", "c.txt"):
            with open(os.path.join(conf, dirty_name), "w") as fh:
                fh.write("dirty\n")
        def mkmerge(name, files, conflicted):
            d = mkrepo(name, files)
            sh(d, "git", "checkout", "-q", "-b", "other")
            for f in conflicted:
                with open(os.path.join(d, f), "w") as fh:
                    fh.write("theirs\n")
            sh(d, "git", "commit", "-q", "-am", "o")
            sh(d, "git", "checkout", "-q", "main")
            for f in conflicted:
                with open(os.path.join(d, f), "w") as fh:
                    fh.write("ours\n")
            sh(d, "git", "commit", "-q", "-am", "m")
            subprocess.run(["git", "merge", "other"], cwd=d, env=gitenv, capture_output=True)
            return d

        def touch(d, f, text="dirty\n"):
            os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
            with open(os.path.join(d, f), "w") as fh:
                fh.write(text)
        globrepo = mkmerge("globrepo", ("a.txt", "b.txt"), ("a.txt",))
        touch(globrepo, "b.txt")
        touch(globrepo, "*.txt", "junk\n")  # untracked file literally named like the glob
        brk = mkmerge("brk", ("app/[id]/page.tsx", "app/i/page.tsx"), ("app/[id]/page.tsx",))
        touch(brk, "app/i/page.tsx")
        onlyconf = mkmerge("onlyconf", ("a.txt", "b.txt"), ("a.txt", "b.txt"))
        dirtydocs = mkrepo("dirtydocs", ("a.txt", "docs/g.md", "docs/h.md"))
        touch(dirtydocs, "docs/g.md")
        # A branch literally named like the dirty directory: `docs --` is a branch switch.
        branchdocs = mkrepo("branchdocs", ("a.txt", "docs/g.md"))
        sh(branchdocs, "git", "branch", "docs")
        touch(branchdocs, "docs/g.md")
        corrupt = mkrepo("corrupt")
        with open(os.path.join(corrupt, ".git", "index"), "wb") as fh:
            fh.write(b"garbage, not an index")
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
        check("A pass: repo with no origin remote (no PR flow)",
              passed(edit("Edit", os.path.join(noorigin, "a.txt"))))
        check("A pass: tracked file in a linked worktree on main while the main checkout is on dev",
              passed(edit("Edit", os.path.join(wt_on_main, "a.txt"))))
        check("A pass: main checkout on dev (not the default branch)",
              passed(edit("Edit", os.path.join(devmain, "a.txt"))))
        check("A refuse: tracked file in a non-exempt subdirectory",
              refused(edit("Edit", os.path.join(main_repo, "pkg", "inner", "m.py"))))
        wtrepo = mkrepo("wtrepo", ("a.txt",))
        r = edit("Edit", os.path.join(wtrepo, "a.txt"))
        m = re.search(r"run (.+?) new <name>", r.stderr)
        check("A refuse: wtrepo (origin-backed) refused with a command", refused(r) and m, r.stderr)
        wt_py = os.path.join(BIN, "wt.py")
        r2 = subprocess.run([sys.executable, wt_py, "new", "probe"], cwd=wtrepo, env=env,
                            capture_output=True, text=True, timeout=60)
        check("A refuse: the printed `wt new` actually works in that repo (rc 0)",
              r2.returncode == 0 and os.path.isdir(r2.stdout.strip() or "/nonexistent"),
              r2.stdout + r2.stderr)
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
        for cmd in ("if gh pr checks 1; then gh pr merge 1; fi",
                    "while ! gh pr merge 1; do sleep 5; done", "time -p gh pr merge 1",
                    "for x in a b; do gh pr merge $x; done"):
            check("B refuse (keywords): " + cmd, refused(bash(cmd, nongit)))
        for cmd in ("if true; then git reset --hard; fi", "for x in 1; do git checkout .; done",
                    "! git reset --hard", "time -p git reset --hard",
                    "git switch -f main", "git switch --force main",
                    "git switch --discard-changes main"):
            check("B refuse (dirty, keywords/switch): " + cmd, refused(bash(cmd, dirty_repo)))
        check("B pass (keywords): for loop with a harmless body",
              passed(bash("for x in a b; do echo $x; done", dirty_repo)))
        check("B pass (dirty): git switch otherbranch / -c",
              passed(bash("git switch -c nb", dirty_repo)) and passed(bash("git switch o", dirty_repo)))
        check("B pass (dirty): git clean -fdX (ignored files only)",
              passed(bash("git clean -fdX", dirty_repo)))
        check("B refuse (dirty): git clean -fdxX is not ignored-only",
              refused(bash("git clean -fdxX", dirty_repo)))
        for cmd in ("git checkout --ours a.txt", "git checkout --theirs -- a.txt",
                    "git checkout -m a.txt", "git checkout --merge a.txt",
                    "git restore --theirs a.txt", "git restore --ours --merge a.txt"):
            check("B pass (conflict): " + cmd, passed(bash(cmd, conf)))
        check("B refuse (conflict tree): --theirs on a non-conflicted dirty path",
              refused(bash("git checkout --theirs b.txt", conf)))
        check("B refuse (conflict tree): git restore --theirs on a non-conflicted dirty path",
              refused(bash("git restore --theirs b.txt", conf)))
        for cmd in ("git checkout --theirs '*.txt'", "git checkout --theirs *.txt",
                    "git checkout --ours -- '*'", "git restore --theirs '*.txt'"):
            check("B refuse (conflict tree, glob): " + cmd, refused(bash(cmd, conf)))
        # Git's own pathspec matching decides: a.tx? matches only the conflicted a.txt, so
        # this is a safe resolution (it was refused by the old character heuristic).
        check("B pass (conflict tree, glob matching only conflicted paths): --theirs a.tx?",
              passed(bash("git checkout --theirs a.tx?", conf)))
        check("B refuse (conflict tree, glob matching a dirty path): --theirs '*.tx?'",
              refused(bash("git checkout --theirs '*.tx?'", conf)))
        for cmd in ("git checkout --theirs 'app/[id]/page.tsx'",
                    "git checkout --ours -- 'app/[id]/page.tsx'",
                    "git restore --theirs 'app/[id]/page.tsx'"):
            check("B pass (conflict, literal bracket path): " + cmd, passed(bash(cmd, conf)))
        check("B refuse (conflict tree): --ours with a mix of conflicted and dirty paths",
              refused(bash("git checkout --ours a.txt b.txt", conf)))
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
        check("B pass: git checkout -- a.txt when only b.txt is dirty (scoped to operands)",
              passed(bash("git checkout -- a.txt", clean_repo)))
        check("B pass: git restore a.txt when only b.txt is dirty",
              passed(bash("git restore a.txt", clean_repo)))
        check("B refuse: git checkout -- b.txt names the dirty file",
              refused(bash("git checkout -- b.txt", clean_repo)))
        check("B refuse: git checkout -- . (git status over '.' sees the dirty file)",
              refused(bash("git checkout -- .", clean_repo)))
        check("B pass (dirty): git checkout -b newbranch",
              passed(bash("git checkout -b newbranch", dirty_repo)))
        check("B pass (dirty): git clean -n -d -f (dry run)",
              passed(bash("git clean -ndf", dirty_repo)))
        check("B pass (untracked only): git reset --hard",
              passed(bash("git reset --hard", untr_repo)))
        check("B pass: not a git repo", passed(bash("git reset --hard", nongit)))
        check("B pass: gh pr merge in an exempt repo", passed(bash("gh pr merge 1", exrepo)))

        bad = os.path.join(tmp, "bad.toml")
        with open(bad, "w") as fh:
            fh.write("this is not toml\n")

        # Redirections are not operands (B1)
        for cmd in ("git restore docs/g.md 2> /dev/null",
                    "git checkout -- docs/g.md > /dev/null 2>&1", "git restore docs/g.md 2>/dev/null",
                    "git restore docs/g.md &> /dev/null", "git restore docs/g.md >| /dev/null"):
            check("B refuse (redirections): " + cmd, refused(bash(cmd, dirtydocs)))
        for cmd in ("git checkout --theirs a.txt 2>&1", "git checkout --theirs a.txt 2>/dev/null",
                    "git checkout --theirs a.txt > /dev/null", "git checkout --theirs a.txt 2> err.log"):
            check("B pass (redirections, mid-merge): " + cmd, passed(bash(cmd, conf)))
        check("B refuse (redirections): gh pr merge 1 2>&1", refused(bash("gh pr merge 1 2>&1", nongit)))
        check("B refuse (redirections): gh pr merge 1 &>/dev/null",
              refused(bash("gh pr merge 1 &>/dev/null", nongit)))
        check("B pass (redirections): a quoted > is not a redirect",
              passed(bash("echo 'a > b' \"2>&1\"", nongit)))

        # One path predicate: git matches globs, directories and `.`
        check("B refuse (N1): --theirs '*.txt' with an untracked file literally named *.txt",
              refused(bash("git checkout --theirs '*.txt'", globrepo)))
        check("B pass (N1 control): --theirs a.txt in the same repo",
              passed(bash("git checkout --theirs a.txt", globrepo)))
        # git's pathspec matches BOTH app/[id]/page.tsx (literal) and app/i/page.tsx (glob [id]),
        # and the second is only dirty, so resolving would also overwrite it: refuse.
        check("B refuse (brackets): --theirs 'app/[id]/page.tsx' when git also matches app/i/page.tsx",
              refused(bash("git checkout --theirs 'app/[id]/page.tsx'", brk)))
        check("B pass (brackets): literal bracket path conflicted, nothing else matching",
              passed(bash("git checkout --theirs 'app/[id]/page.tsx'", conf)))
        check("B pass: --theirs . when only conflicted files are dirty",
              passed(bash("git checkout --theirs .", onlyconf)))
        check("B pass: git restore --theirs . when only conflicted files are dirty",
              passed(bash("git restore --theirs .", onlyconf)))
        check("B refuse: --theirs . when a non-conflicted file is dirty",
              refused(bash("git checkout --theirs .", conf)))
        for cmd in ("git checkout -b docs", "git checkout -B docs", "git checkout --orphan docs",
                    "git checkout -b docs main", "git switch -c docs"):
            check("B pass (N2): new branch named like a dirty dir: " + cmd,
                  passed(bash(cmd, dirtydocs)))
        check("B pass (N3): git checkout main -- (branch switch, no paths)",
              passed(bash("git checkout main --", dirtydocs)))
        check("B pass (N3): git checkout docs -- where docs is a branch AND a dirty directory",
              passed(bash("git checkout docs --", branchdocs)))
        check("B refuse (N3 control): git checkout docs -- docs when the path part is named",
              refused(bash("git checkout docs -- docs", branchdocs)))
        check("B refuse: git checkout main -- docs/g.md still names the dirty path",
              refused(bash("git checkout main -- docs/g.md", dirtydocs)))
        check("B refuse: git checkout docs (bare directory operand holding a dirty file)",
              refused(bash("git checkout docs", dirtydocs)))
        check("B pass: git restore docs/h.md (clean sibling of the dirty file)",
              passed(bash("git restore docs/h.md", dirtydocs)))

        # A git failure is never read as clean
        check("fail safe: git restore with a pathspec outside the repo is refused",
              refused(bash("git restore /nonexistent-outside/x", dirtydocs)))
        check("fail safe: git status failing (corrupt index) refuses reset --hard",
              refused(bash("git reset --hard", corrupt)))
        check("fail safe: git status failing (corrupt index) refuses clean -fd",
              refused(bash("git clean -fd", corrupt)))
        check("fail safe: not a repo at all still passes", passed(bash("git restore x", nongit)))

        # The config file is protected (N6)
        cfgp = env["SESSION_NOTES_CONFIG"]
        cfgdir = os.path.dirname(cfgp)
        os.makedirs(os.path.join(tmp, "sub"), exist_ok=True)
        cfg_refused = {}
        for label, payload in (
                ("Edit", {"tool_name": "Edit", "tool_input": {"file_path": cfgp}, "cwd": tmp}),
                ("Write", {"tool_name": "Write", "tool_input": {"file_path": cfgp}, "cwd": tmp}),
                ("MultiEdit", {"tool_name": "MultiEdit", "tool_input": {"file_path": cfgp}, "cwd": tmp}),
                ("relative path", {"tool_name": "Edit", "tool_input": {"file_path": "../config.toml"},
                                   "cwd": os.path.join(tmp, "sub")}),
                ("apply_patch", {"tool_name": "apply_patch", "cwd": tmp, "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: config.toml\n@@\n-a\n+b\n*** End Patch\n"}}),
                ("apply_patch via shell", {"tool_name": "shell", "cwd": tmp, "tool_input": {
                    "command": ["bash", "-lc", "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: config.toml\n@@\n-a\n+b\n*** End Patch\nEOF"]}})):
            r = run(payload)
            check("config protected (Arm A): " + label, refused(r) and "user" in r.stderr, r.stderr)
        alias = os.path.join(tmp, "alias.toml")
        os.symlink(cfgp, alias)
        check("config protected: an edit through a symlink that points at it",
              refused(edit("Edit", alias)))
        real_cfg = os.path.join(tmp, "cfg_real.toml")
        link_cfg = os.path.join(tmp, "cfg_link.toml")
        with open(real_cfg, "w") as fh:
            fh.write("")
        os.symlink(real_cfg, link_cfg)
        lenv = dict(env, SESSION_NOTES_CONFIG=link_cfg)

        def run_env(e, payload):
            return subprocess.run([sys.executable, os.path.realpath(__file__)], env=e,
                                  input=json.dumps(payload), capture_output=True, text=True,
                                  timeout=30)
        for label, target in (("the symlink", link_cfg), ("its target", real_cfg)):
            r = run_env(lenv, {"tool_name": "Edit", "tool_input": {"file_path": target}, "cwd": tmp})
            check("config protected: config is a symlink, edit " + label, refused(r), r.stderr)
        r = run_env(lenv, {"tool_name": "Bash", "cwd": tmp,
                           "tool_input": {"command": "echo x >> " + real_cfg}})
        check("config protected: config is a symlink, shell append to its target", refused(r), r.stderr)
        # exempt_repos / exempt_paths cannot exempt the config
        cfgrepo = mkrepo("cfgrepo", ("x.txt",))
        exc_cfg = os.path.join(cfgrepo, "cfg.toml")
        with open(exc_cfg, "w") as fh:
            fh.write('[checkout_guard]\nexempt_repos = ["%s"]\nexempt_paths = ["%s/*"]\n'
                     % (cfgrepo, cfgrepo))
        sh(cfgrepo, "git", "add", "cfg.toml")
        sh(cfgrepo, "git", "commit", "-q", "-m", "cfg")
        eenv = dict(env, SESSION_NOTES_CONFIG=exc_cfg)
        r = run_env(eenv, {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(cfgrepo, "x.txt")},
                           "cwd": tmp})
        check("config protected (control): the exemption is live for other files in that repo",
              passed(r), r.stderr)
        r = run_env(eenv, {"tool_name": "Edit", "tool_input": {"file_path": exc_cfg}, "cwd": tmp})
        check("config protected: refused even when the config exempts its own repo and path",
              refused(r), r.stderr)
        r = run_env(eenv, {"tool_name": "Bash", "cwd": cfgrepo,
                           "tool_input": {"command": "gh pr merge 1"}})
        check("config protected (control): exempt repo still lets gh pr merge through",
              passed(r), r.stderr)
        # a config that does not parse must not make itself editable
        r = run_env(dict(env, SESSION_NOTES_CONFIG=bad), {
            "tool_name": "Edit", "tool_input": {"file_path": bad}, "cwd": tmp})
        check("config protected: refused while the config is unparseable (Edit)",
              refused(r), r.stderr)
        r = run_env(dict(env, SESSION_NOTES_CONFIG=bad), {
            "tool_name": "Bash", "cwd": tmp, "tool_input": {"command": "echo x >> " + bad}})
        check("config protected: refused while the config is unparseable (shell)",
              refused(r), r.stderr)
        r = run_env(dict(env, SESSION_NOTES_CONFIG=bad), {
            "tool_name": "Edit", "tool_input": {"file_path": os.path.join(main_repo, "a.txt")},
            "cwd": tmp})
        check("config protected (control): unparseable config still fails open elsewhere",
              r.returncode == 0 and "LOUD" in r.stderr, r.stderr)
        for cmd, where in (("echo x >> " + cfgp, tmp), ("echo x > " + cfgp, tmp),
                           ("echo x >" + cfgp, tmp), ("echo x 2>&1 >> " + cfgp, tmp),
                           ("cat > " + cfgp + " <<EOF\nexempt_repos = []\nEOF", tmp),
                           ("echo x | tee " + cfgp, tmp), ("echo x | tee -a " + cfgp, tmp),
                           ("sed -i '' s/a/b/ " + cfgp, tmp), ("sed -i.bak s/a/b/ " + cfgp, tmp),
                           ("perl -pi -e 's/a/b/' " + cfgp, tmp),
                           ("cp /tmp/x " + cfgp, tmp), ("mv /tmp/x " + cfgp, tmp),
                           ("install -m 600 /tmp/x " + cfgp, tmp), ("ln -sf /tmp/x " + cfgp, tmp),
                           ("rm " + cfgp, tmp), ("rm -f " + cfgp, tmp),
                           ("truncate -s 0 " + cfgp, tmp), ("unlink " + cfgp, tmp),
                           ("dd if=/tmp/x of=" + cfgp, tmp), ("mv " + cfgp + " /tmp/gone", tmp),
                           ("echo x >> config.toml", tmp), ("echo x >> ../config.toml", os.path.join(tmp, "sub")),
                           ("echo x >> ~/../config.toml", tmp), ("cd .. && echo x >> config.toml", os.path.join(tmp, "sub")),
                           ("sudo tee " + cfgp, tmp), ("bash -c 'echo x >> " + cfgp + "'", tmp),
                           ("true; echo x >> " + cfgp, tmp), ("echo x >> " + alias, tmp)):
            r = bash(cmd, where)
            check("config protected (shell): " + cmd.replace("\n", "\\n"),
                  refused(r) and "user" in r.stderr, r.stderr)
        for cmd in ("cat " + cfgp, "grep x " + cfgp, "cp " + cfgp + " /tmp/copy-of-config",
                    "less " + cfgp, "sed -n 1p " + cfgp, "echo hi > " + os.path.join(tmp, "other.txt"),
                    "tee " + os.path.join(tmp, "other.txt"), "cat " + cfgp + " > " + os.path.join(tmp, "o2.txt")):
            check("config protected (control, reads pass): " + cmd, passed(bash(cmd, tmp)))

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

        # The payloads Codex 0.171 really sends (recorded, scrubbed fixtures).
        fx = os.path.join(ROOT, "tests", "fixtures")

        def recorded(name, cwd, **tool_input):
            with open(os.path.join(fx, name)) as fh:
                pl = json.load(fh)
            pl["cwd"] = cwd
            pl["tool_input"].update(tool_input)
            return pl

        r = run(recorded("codex_pretooluse_apply_patch.json", main_repo,
                         command=patch))
        check("codex recorded: apply_patch on a tracked file of main refused", refused(r), r.stderr)
        pl = recorded("codex_pretooluse_apply_patch.json", main_repo)
        pl["tool_input"]["command"] = pl["tool_input"]["command"].replace("tracked.txt", "a.txt")
        r = run(pl)
        check("codex recorded: the fixture's own patch (path swapped) refused, names the file",
              refused(r) and "a.txt" in r.stderr, r.stderr)
        pl = recorded("codex_pretooluse_apply_patch.json", wt_on_main)
        pl["tool_input"]["command"] = pl["tool_input"]["command"].replace("tracked.txt", "a.txt")
        r = run(pl)
        check("codex recorded: apply_patch on a tracked file in a linked worktree that is on the default "
              "branch passes (so is_main_checkout is what lets it)", passed(r), r.stderr)
        r = run(recorded("codex_pretooluse_apply_patch.json", nongit))
        check("codex recorded: apply_patch outside a repo passes", passed(r), r.stderr)

        # B1: Codex trims each patch line, so a marker may be indented
        def patched(header_path, body_indent="", cwd=None, second=None):
            pl = recorded("codex_pretooluse_apply_patch.json", cwd or main_repo)
            text = "*** Begin Patch\n"
            if second:
                text += "*** Add File: fresh.txt\n+n\n"
            text += "%s*** Update File: %s\n@@\n-x\n+y\n*** End Patch\n" % (body_indent, header_path)
            pl["tool_input"]["command"] = text
            return run(pl)
        for label, ind in (("space", "   "), ("tab", "\t"), ("mixed", " \t ")):
            r = patched("a.txt", ind)
            check("codex recorded B1: %s-indented Update header on a tracked file refused" % label,
                  refused(r) and "a.txt" in r.stderr, r.stderr)
        r = patched("a.txt", "  ", second=True)
        check("codex recorded B1: an indented header after an earlier hunk refused", refused(r), r.stderr)
        r = patched(cfgp, "   ", cwd=tmp)
        check("codex recorded B1: an indented header naming the guard's own config file refused",
              refused(r) and "user" in r.stderr, r.stderr)
        r = patched("brand-new-%s.txt" % "x", "   ")
        check("codex recorded B1: an indented header on a new file still passes", passed(r), r.stderr)

        # N2: apply_patch through the shell in every spelling Codex intercepts
        body = "<<'EOF'\n" + patch + "EOF"
        for label, cmd, where in (
                ("applypatch alias", "applypatch " + body, main_repo),
                ("cd <dir> && apply_patch", "cd %s && apply_patch %s" % (os.path.basename(main_repo), body),
                 os.path.dirname(main_repo)),
                ("bash -lc as one string", "bash -lc \"apply_patch %s\"" % body.replace('"', '\\"'), main_repo)):
            r = run({"tool_name": "Bash", "cwd": where, "tool_input": {"command": cmd}})
            check("codex recorded N2: %s refused" % label, refused(r) and "a.txt" in r.stderr, r.stderr)
        r = run({"tool_name": "Bash", "cwd": os.path.dirname(wt1),
                 "tool_input": {"command": "cd %s && apply_patch %s" % (os.path.basename(wt1), body)}})
        check("codex recorded N2: cd into a linked worktree then apply_patch passes", passed(r), r.stderr)
        r = run(recorded("codex_pretooluse_shell.json", nongit, command="gh pr merge 1 --squash"))
        check("codex recorded: Bash `gh pr merge` refused", refused(r), r.stderr)
        r = run(recorded("codex_pretooluse_shell.json", dirty_repo, command="git reset --hard"))
        check("codex recorded: Bash `git reset --hard` on a dirty tree refused", refused(r), r.stderr)
        r = run(recorded("codex_pretooluse_shell.json", mkrepo("cx_clean"), command="git reset --hard"))
        check("codex recorded: Bash `git reset --hard` on a clean tree passes", passed(r), r.stderr)
        r = run(recorded("codex_pretooluse_shell.json", main_repo))
        check("codex recorded: the fixture's own `git status --short` passes", passed(r), r.stderr)

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
