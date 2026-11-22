#!/usr/bin/env python3
"""wt.py -- one worktree per change, and a PR lifecycle that cannot merge unreviewed code.

Works in any git repo (run it anywhere inside one, including inside a worktree).

    wt.py new <name>                 worktree + branch <name> off origin/<default>
    wt.py pr [--title T] [--body B] [--dry-run]    push this worktree's branch, open a PR
    wt.py merge [name] [--dry-run]   verdict gate, squash-merge, remove worktree + branch
    wt.py list                       this repo's worktrees: dirty, ahead, PR state
    wt.py rm <name> [--force]        remove a worktree and its branch
    wt.py gc [--yes]                 list (and on a yes, remove) merged/closed stragglers
    wt.py install-hooks [repo] [--dry-run]   install the session-notes pre-commit (githooks/pre-commit)
    wt.py --selftest

WHERE a worktree goes, first rule that applies:
  1. the deepest configured scope that contains the repo and sets `worktree_root`:
     <worktree_root>/<name>
  2. [wt].root: <root>/<repo basename>/<name>
  3. the repo tracks CLAUDE.md or .claude/CLAUDE.md at its root: a hidden sibling container
     <repo parent>/.<repo basename>-worktrees/<name> (a worktree nested under the repo would
     also load the main checkout's CLAUDE.md)
  4. <repo>/.worktrees/<name>, with /.worktrees/ added once to <git-dir>/info/exclude

THE VERDICT GATE (the safety property of `merge`). A review is a PR comment whose FIRST line
is exactly `Review verdict: APPROVE <40-hex head sha>` or `Review verdict: CHANGES <sha>`.
Only comments written by the token's own GitHub login count (a stranger commenting on a
public repo cannot approve). The newest such comment must be APPROVE and its sha must equal
the PR's current head, so any push after the approval voids it. Anything else is refused.

GitHub access: every gh call goes through one function, with the token from
`[wt].token_command` exported as GH_TOKEN. The executable is $WT_GH (default `gh`).
`gc` removes worktrees, so it asks first and is meant to be run by a person, never from a
scheduled job.

Exit codes: 0 ok; 1 usage or fatal; 2 not in a worktree; 3 token command failed; 4 dirty
or unpushed; 5 the branch already has a MERGED PR; 6 verdict gate refused.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import notes_config  # noqa: E402

VERDICT_PREFIX = "Review verdict: "
APPROVE = "APPROVE"
CHANGES = "CHANGES"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_VERDICT_RE = re.compile(r"^%s(%s|%s) ([0-9a-f]{40})$" % (
    re.escape(VERDICT_PREFIX), APPROVE, CHANGES))
HOOK_MARKER = "# session-notes pre-commit shim"
EXCLUDE_LINE = "/.worktrees/"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Die(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code
        self.msg = msg


class GhError(Exception):
    pass


# ------------------------------------------------------------------ verdicts
def parse_verdict(body):
    """(verdict, sha) if the comment's FIRST line is exactly a verdict line, else None."""
    if not isinstance(body, str) or not body:
        return None
    first = body.split("\n", 1)[0].rstrip("\r")
    m = _VERDICT_RE.match(first)
    return (m.group(1), m.group(2)) if m else None


def verdict_line(verdict, sha):
    if verdict not in (APPROVE, CHANGES):
        raise ValueError("verdict must be %s or %s, got %r" % (APPROVE, CHANGES, verdict))
    if not isinstance(sha, str) or not _SHA.match(sha):
        raise ValueError("sha must be 40 lowercase hex characters, got %r" % (sha,))
    return "%s%s %s" % (VERDICT_PREFIX, verdict, sha)


def check_gate(comments, login, head):
    """(ok, message) for the verdict gate. comments: GitHub issue-comment dicts."""
    ordered = sorted(comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0))
    own, other = [], 0
    for c in ordered:
        v = parse_verdict(c.get("body"))
        if not v:
            continue
        if ((c.get("user") or {}).get("login") or "").lower() == login.lower():
            if c.get("created_at") and c.get("updated_at") and c["updated_at"] != c["created_at"]:
                return False, ("verdict comment %s was edited after it was posted; an edited "
                               "comment is not trusted, post a fresh verdict comment (and delete "
                               "the edited one)" % (c.get("id") or "?"))
            own.append(v)
        else:
            other += 1
    if not own:
        if other:
            return False, ("only verdicts from other authors found (%d); only comments by %s count"
                           % (other, login))
        return False, "no review verdict comment on the PR"
    verdict, sha = own[-1]
    if verdict != APPROVE:
        return False, "the newest verdict is %s, not %s" % (verdict, APPROVE)
    if sha != head:
        return False, "approved sha %s but the PR head is %s" % (sha, head)
    return True, "newest verdict by %s is %s at the current head" % (login, APPROVE)


# ------------------------------------------------------------------ git / gh
def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Die(1, "git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def gout(cwd, *args):
    return git(cwd, *args).stdout.strip()


class Gh:
    def __init__(self, cfg):
        self.cmd = cfg["wt"]["token_command"]
        self._token = None

    def token(self):
        if self._token is None:
            r = subprocess.run(self.cmd, shell=True, capture_output=True, text=True)
            tok = r.stdout.strip()
            if r.returncode != 0 or not tok:
                raise Die(3, "wt.token_command=%r failed (exit %d, %s); set it to a command "
                             "that prints a GitHub token" % (
                                 self.cmd, r.returncode, "empty output" if not tok else "see stderr"))
            self._token = tok
        return self._token

    def run(self, args, cwd):
        env = dict(os.environ, GH_TOKEN=self.token())
        exe = os.environ.get("WT_GH", "gh")
        try:
            r = subprocess.run([exe] + list(args), cwd=cwd, env=env, capture_output=True, text=True)
        except OSError as exc:
            raise GhError("cannot run %s: %s" % (exe, exc))
        if r.returncode != 0:
            raise GhError((r.stderr or r.stdout).strip() or "%s exited %d" % (exe, r.returncode))
        return r.stdout

    def json(self, args, cwd):
        out = self.run(args, cwd)
        dec, i, items = json.JSONDecoder(), 0, []
        while i < len(out):
            while i < len(out) and out[i].isspace():
                i += 1
            if i >= len(out):
                break
            try:
                obj, i = dec.raw_decode(out, i)
            except ValueError as exc:
                raise GhError("gh printed unreadable JSON: %s" % exc)
            items.append(obj)
        if len(items) == 1:
            return items[0]
        flat = []  # paginated arrays arrive concatenated
        for it in items:
            flat.extend(it if isinstance(it, list) else [it])
        return flat


def load_cfg():
    try:
        return notes_config.load()
    except notes_config.ConfigError as exc:
        raise Die(1, "config: %s" % exc)


# ------------------------------------------------------------------ repo + worktrees
def repo_main(cwd=None):
    cwd = cwd or os.getcwd()
    r = git(cwd, "rev-parse", "--git-common-dir", check=False)
    if r.returncode != 0:
        raise Die(1, "not inside a git repository: %s" % cwd)
    common = r.stdout.strip()
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    common = os.path.realpath(common)
    if os.path.basename(common) != ".git":
        raise Die(1, "bare repositories are not supported (%s)" % common)
    return os.path.dirname(common), common


def default_branch(main):
    r = git(main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    name = r.stdout.strip()
    if r.returncode == 0 and name.startswith("origin/"):
        return name[len("origin/"):]
    return "main"


def worktree_path(main, name, cfg):
    real = os.path.realpath(main)
    best = None
    for s in cfg["scopes"]:
        if s.get("worktree_root") and notes_config._under(real, s["root"]):
            if best is None or len(os.path.realpath(s["root"])) > len(os.path.realpath(best["root"])):
                best = s
    if best:
        return os.path.join(best["worktree_root"], name)
    if cfg["wt"]["root"]:
        return os.path.join(cfg["wt"]["root"], os.path.basename(real), name)
    tracked = git(main, "ls-files", "--", "CLAUDE.md", ".claude/CLAUDE.md").stdout.strip()
    if tracked:
        return os.path.join(os.path.dirname(real), ".%s-worktrees" % os.path.basename(real), name)
    return os.path.join(real, ".worktrees", name)


def ensure_exclude(common):
    path = os.path.join(common, "info", "exclude")
    text = open(path).read() if os.path.exists(path) else ""
    if EXCLUDE_LINE in [l.strip() for l in text.splitlines()]:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(("" if not text or text.endswith("\n") else "\n") + EXCLUDE_LINE + "\n")


def list_worktrees(main):
    out = gout(main, "worktree", "list", "--porcelain")
    blocks, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    res = []
    for i, b in enumerate(blocks):
        br = b.get("branch", "")
        res.append({"path": b["worktree"], "head": b.get("HEAD", ""), "main": i == 0,
                    "missing": "prunable" in b or not os.path.isdir(b["worktree"]),
                    "branch": br[len("refs/heads/"):] if br.startswith("refs/heads/") else ""})
    return res


def find_worktree(main, name):
    wts = [w for w in list_worktrees(main) if not w["main"]]
    if name is None:
        here = git(os.getcwd(), "rev-parse", "--show-toplevel", check=False).stdout.strip()
        if not here or os.path.realpath(here) == os.path.realpath(main):
            raise Die(2, "not inside a worktree; run from one or pass a name")
        for w in wts:
            if os.path.realpath(w["path"]) == os.path.realpath(here):
                return w
        raise Die(2, "not inside a worktree of this repo")
    for w in wts:
        if w["branch"] == name or os.path.basename(w["path"]) == name:
            return w
    raise Die(1, "no worktree named %r (see: wt.py list)" % name)


def is_dirty(path):
    return bool(gout(path, "status", "--porcelain"))


def has_remote_ref(main, branch):
    return git(main, "show-ref", "--verify", "--quiet", "refs/remotes/origin/" + branch,
               check=False).returncode == 0


def unpushed(main, path, branch, any_remote=False):
    """Commits in HEAD that exist on no remote (any_remote) or not on origin/<branch>."""
    if not any_remote and has_remote_ref(main, branch):
        spec = ["origin/%s..HEAD" % branch]
    else:
        spec = ["HEAD", "--not", "--remotes"]
    return int(gout(path, "rev-list", "--count", *spec) or 0)


def merged_heads(gh, main, branch):
    """headRefOid of every MERGED PR for this branch, or None if gh cannot say."""
    try:
        prs = gh.json(["pr", "list", "--head", branch, "--state", "merged",
                       "--json", "number,headRefOid", "--limit", "20"], main)
    except (GhError, Die):
        return None
    return [p.get("headRefOid") for p in prs if isinstance(p, dict)]


def remove_worktree(main, path, branch, force=False):
    git(main, "worktree", "remove", *(["--force"] if force else []), path)
    if branch:
        git(main, "branch", "-D", branch)


def ff_main(main, default):
    cur = gout(main, "branch", "--show-current")
    if cur != default:
        return "main checkout not fast-forwarded: it is on %r, not %r" % (cur, default)
    if is_dirty(main):
        return "main checkout not fast-forwarded: it has uncommitted changes"
    before = gout(main, "rev-parse", "HEAD")
    r = git(main, "merge", "--ff-only", "origin/" + default, check=False)
    if r.returncode != 0:
        return "main checkout not fast-forwarded: %s" % (r.stderr or r.stdout).strip()
    after = gout(main, "rev-parse", "HEAD")
    return "main checkout fast-forwarded to origin/%s" % default if after != before \
        else "main checkout already up to date with origin/%s" % default


# ------------------------------------------------------------------ hooks
def hook_script():
    return os.path.join(os.path.dirname(HERE), "githooks", "pre-commit")


def build_shim(script, repo):
    # Generated text (paths) only ever goes into single-quoted assignments; the message is
    # static text plus variable expansions, so a path is never re-evaluated by the shell.
    # repo_q holds the repo path already shell-quoted, so the printed repair line can be
    # pasted as is even when the path has spaces.
    return ("#!/bin/sh\n%s\n"
            "# script-path: %s\n"
            "script=%s\n"
            "repo_q=%s\n"
            "if [ ! -f \"$script\" ]; then\n"
            "  printf '%%s\\n' \"session-notes pre-commit: $script is missing (the session-notes "
            "checkout moved or was deleted); the commit is refused. Repair: "
            "python3 <session-notes>/bin/wt.py install-hooks $repo_q\" >&2\n"
            "  exit 1\n"
            "fi\n"
            "python3 \"$script\" \"$@\" || exit $?\n"
            "prev=\"$(dirname \"$0\")/pre-commit.pre-session-notes\"\n"
            "if [ -x \"$prev\" ]; then exec \"$prev\" \"$@\"; fi\n"
            "exit 0\n") % (HOOK_MARKER, script, shlex.quote(script), shlex.quote(shlex.quote(repo)))


def install_hooks(repo=None, out=sys.stdout, dry_run=False):
    main, common = repo_main(repo)
    say = (lambda m: print(("would: " if dry_run else "") + m, file=out))
    r = git(main, "config", "--get", "core.hooksPath", check=False)
    if r.returncode == 0 and r.stdout.strip():
        print("%s manages its own hooks via core.hooksPath=%s; the session-notes pre-commit was "
              "not installed" % (main, r.stdout.strip()), file=out)
        return 0
    script = hook_script()
    if not os.path.isfile(script):
        raise Die(1, "githooks/pre-commit not found next to this checkout: %s" % script)
    hooks = os.path.join(common, "hooks")
    target = os.path.join(hooks, "pre-commit")
    prev = target + ".pre-session-notes"
    chained, repaired = False, False
    if os.path.lexists(target):
        if os.path.islink(target) and not os.path.exists(target):
            raise Die(1, "%s is a dangling symlink; remove it or point it at a real hook, then "
                         "re-run (nothing was changed)" % target)
        text = open(target, errors="replace").read()
        if HOOK_MARKER in text:
            m = re.search(r"^# script-path: (.*)$", text, re.M)
            rec = m.group(1) if m else None
            if rec == script and os.path.isfile(rec):
                print("session-notes pre-commit already installed in %s (left as is)" % hooks, file=out)
                return 0
            repaired = True
        else:
            if os.path.lexists(prev):
                raise Die(1, "%s already exists; move it aside, then re-run (the existing "
                             "pre-commit was not touched)" % prev)
            chained = True
    elif os.path.lexists(prev):
        chained = True
    if dry_run:
        say("%s the session-notes pre-commit in %s%s" % (
            "repair" if repaired else "install", hooks,
            "; move the existing hook to pre-commit.pre-session-notes and chain to it" if chained
            and os.path.lexists(target) else ""))
        return 0
    os.makedirs(hooks, exist_ok=True)
    if chained and os.path.lexists(target):
        os.rename(target, prev)
    tmp = target + ".wt-tmp"
    with open(tmp, "w") as fh:
        fh.write(build_shim(script, main))
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    if repaired:
        print("repaired the session-notes pre-commit in %s (now runs %s)" % (hooks, script), file=out)
    else:
        print("installed the session-notes pre-commit in %s%s" % (
            hooks, "; the previous hook moved to pre-commit.pre-session-notes and still runs"
            if chained else ""), file=out)
    return 0


def hook_present(common):
    t = os.path.join(common, "hooks", "pre-commit")
    return os.path.isfile(t) and HOOK_MARKER in open(t, errors="replace").read()


# ------------------------------------------------------------------ commands
def cmd_new(args, cfg):
    name = args.name
    if not NAME_RE.match(name) or git(os.getcwd(), "check-ref-format", "--branch", name,
                                      check=False).returncode != 0:
        raise Die(1, "bad name %r: letters, digits, . _ - only, no slash, starting alphanumeric" % name)
    main, common = repo_main()
    default = default_branch(main)
    path = worktree_path(main, name, cfg)
    if os.path.lexists(path):
        raise Die(1, "%s already exists" % path)
    if git(main, "show-ref", "--verify", "--quiet", "refs/heads/" + name, check=False).returncode == 0:
        raise Die(1, "branch %r already exists" % name)
    r = git(main, "fetch", "origin", check=False)
    if r.returncode != 0:
        raise Die(1, "git fetch origin failed: %s" % r.stderr.strip())
    if cfg["wt"]["root"] == "" and path.startswith(os.path.join(os.path.realpath(main), ".worktrees") + os.sep):
        ensure_exclude(common)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    git(main, "worktree", "add", path, "-b", name, "origin/" + default)
    if not hook_present(common):
        try:
            install_hooks(main, out=sys.stderr)
        except (Die, OSError) as exc:
            print("warning: pre-commit hook not installed: %s" % getattr(exc, "msg", exc),
                  file=sys.stderr)
    print("worktree %s on branch %s (from origin/%s)" % (path, name, default), file=sys.stderr)
    print(path)
    return 0


def _repo_and_branch():
    main, common = repo_main()
    default = default_branch(main)
    top = git(os.getcwd(), "rev-parse", "--show-toplevel", check=False).stdout.strip()
    if not top or os.path.realpath(top) == os.path.realpath(main):
        raise Die(2, "not inside a worktree (this is the main checkout)")
    branch = gout(top, "branch", "--show-current")
    if not branch or branch == default:
        raise Die(2, "the worktree is not on a non-default branch (on %r, default %r)"
                  % (branch, default))
    return main, default, top, branch


def cmd_pr(args, cfg):
    main, default, top, branch = _repo_and_branch()
    if is_dirty(top):
        raise Die(4, "the worktree has uncommitted changes; commit them first (wt does not "
                     "auto-commit)")
    title = args.title or gout(top, "log", "-1", "--format=%s")
    push = ["git", "push", "-u", "origin", branch]
    create = ["gh", "pr", "create", "--head", branch, "--base", default, "--title", title,
              "--body", args.body or ""]
    if args.dry_run:
        print("dry run, nothing pushed or created (the merged-PR check needs the network and "
              "is skipped):")
        print("  " + shlex.join(push))
        print("  " + shlex.join(create))
        return 0
    gh = Gh(cfg)
    merged = None
    try:
        merged = gh.json(["pr", "list", "--head", branch, "--state", "merged",
                          "--json", "number,url", "--limit", "5"], top)
    except GhError as exc:
        print("warning: could not check for a merged PR (%s); continuing" % exc, file=sys.stderr)
    if merged:
        raise Die(5, "branch %r already has a MERGED PR (%s); the branch name is spent, start "
                     "a new one with: wt.py new <name>" % (branch, merged[0].get("url") or merged[0].get("number")))
    r = subprocess.run(push, cwd=top)
    if r.returncode != 0:
        raise Die(1, "git push failed")
    try:
        out = gh.run(create[1:], top)
    except GhError as exc:
        raise Die(1, "gh pr create failed: %s" % exc)
    print(out.strip())
    return 0


def _owner_repo(url):
    m = re.match(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/\d+", url or "")
    if not m:
        raise Die(1, "cannot read owner/repo from PR url %r" % (url,))
    return m.group(1), m.group(2)


def cmd_merge(args, cfg):
    main, common = repo_main()
    default = default_branch(main)
    w = find_worktree(main, args.name)
    branch, path = w["branch"], w["path"]
    if not branch or branch == default:
        raise Die(1, "%s is not on a non-default branch" % path)
    r = git(main, "fetch", "origin", check=False)
    if r.returncode != 0:
        print("warning: git fetch origin failed (%s)" % r.stderr.strip(), file=sys.stderr)
    if is_dirty(path):
        raise Die(4, "%s has uncommitted changes; commit and push, then retry" % path)
    head = gout(path, "rev-parse", "HEAD")
    remote = git(main, "rev-parse", "--verify", "-q", "refs/remotes/origin/" + branch, check=False).stdout.strip()
    if remote != head:
        raise Die(4, "local HEAD %s is not origin/%s (%s); push first" % (
            head[:10], branch, remote[:10] or "branch not on origin"))
    gh = Gh(cfg)
    try:
        view = gh.json(["pr", "view", branch, "--json", "number,headRefOid,state,url"], main)
        owner, repo = _owner_repo(view.get("url"))
        login = gh.run(["api", "user", "--jq", ".login"], main).strip()
        comments = gh.json(["api", "--paginate",
                            "repos/%s/%s/issues/%s/comments" % (owner, repo, view["number"])], main)
    except GhError as exc:
        raise Die(1, "gh failed: %s" % exc)
    if not login:
        raise Die(1, "gh api user returned no login")
    if view.get("state") != "OPEN":
        raise Die(1, "PR #%s is %s, not OPEN" % (view["number"], view.get("state")))
    if view.get("headRefOid") != head:
        raise Die(4, "PR head %s is not local HEAD %s" % (view.get("headRefOid"), head))
    ok, msg = check_gate(comments if isinstance(comments, list) else [], login, head)
    if not ok:
        raise Die(6, "verdict gate refused PR #%s: %s" % (view["number"], msg))
    print("verdict gate passed: %s" % msg)
    merge = ["gh", "pr", "merge", str(view["number"]), "--squash", "--delete-branch",
             "--match-head-commit", head, "-R", "%s/%s" % (owner, repo)]
    if args.dry_run:
        print("dry run, would do:")
        print("  " + shlex.join(merge))
        print("  git worktree remove %s" % shlex.quote(path))
        print("  git branch -D %s" % shlex.quote(branch))
        print("  fast-forward the main checkout to origin/%s if it is on %s and clean" % (default, default))
        return 0
    try:
        gh.run(merge[1:], main)
    except GhError as exc:
        raise Die(1, "gh pr merge failed: %s" % exc)
    now = git(path, "rev-parse", "HEAD", check=False).stdout.strip()
    if now != head or is_dirty(path):
        print("merged PR #%s, but %s was NOT removed: it has new work since the gated head "
              "(%s). Branch %s kept." % (view["number"], path,
                                         "HEAD moved" if now != head else "uncommitted changes", branch))
    else:
        remove_worktree(main, path, branch)
        print("merged PR #%s; removed %s and branch %s" % (view["number"], path, branch))
    print(ff_main(main, default))
    return 0


def _pr_state(gh, main, branch):
    try:
        prs = gh.json(["pr", "list", "--head", branch, "--state", "all",
                       "--json", "number,state,headRefOid", "--limit", "20"], main)
    except (GhError, Die):
        return None
    return prs if isinstance(prs, list) else []


def _summary(prs):
    if prs is None:
        return "?"
    if not prs:
        return "none"
    return "/".join(sorted({p.get("state", "?").lower() for p in prs}))


def cmd_list(args, cfg):
    main, common = repo_main()
    gh, note = Gh(cfg), None
    rows = 0
    for w in list_worktrees(main):
        if w["main"]:
            continue
        b = w["branch"]
        if w["missing"]:
            print("%-20s %-20s %s  missing; if it is really gone, run wt rm %s --force" % (
                os.path.basename(w["path"]), b or "(detached)", w["path"],
                os.path.basename(w["path"])))
            rows += 1
            continue
        prs = _pr_state(gh, main, b) if note is None and b else None
        if prs is None and b and note is None:
            note = "PR state unavailable (gh or token command failed)"
        ahead = unpushed(main, w["path"], b) if b else 0
        print("%-20s %-20s %s  %s  ahead=%d  pr=%s" % (
            os.path.basename(w["path"]), b or "(detached)", w["path"],
            "dirty" if is_dirty(w["path"]) else "clean", ahead, _summary(prs)))
        rows += 1
    if not rows:
        print("no worktrees besides the main checkout")
    if note:
        print("note: " + note, file=sys.stderr)
    return 0


def cmd_rm(args, cfg):
    main, common = repo_main()
    w = find_worktree(main, args.name)
    branch, path = w["branch"], w["path"]
    if not args.force and not w["missing"]:
        if is_dirty(path):
            raise Die(4, "%s has uncommitted changes (--force to discard them)" % path)
        if unpushed(main, path, branch):
            heads = (merged_heads(Gh(cfg), main, branch) or []) if branch else []
            if gout(path, "rev-parse", "HEAD") not in heads:
                raise Die(4, "%s has commits on no remote branch (%s) and no merged PR "
                             "covers them (--force to discard)" % (
                                 path, "detached HEAD" if not branch else "branch %r" % branch))
    if w["missing"]:
        if not args.force:
            raise Die(4, "the directory of worktree %s is missing or no longer a worktree (an "
                         "unmounted volume?); its "
                         "record may hold a staged index. If it is really gone, re-run with "
                         "--force to remove just this record" % path)
        r = git(main, "worktree", "remove", "--force", path, check=False)
        if r.returncode != 0:
            raise Die(1, "git worktree remove --force %s failed: %s. If something else now sits at "
                         "that path, the record is stale: check it, then run git worktree prune "
                         "(which removes EVERY stale record)" % (path, (r.stderr or r.stdout).strip()))
        print("removed the record of missing worktree %s (branch %s kept)" % (path, branch or "-"))
        return 0
    remove_worktree(main, path, branch, force=args.force)
    print("removed %s and branch %s" % (path, branch or "(detached)"))
    return 0


def cmd_gc(args, cfg):
    main, common = repo_main()
    gh = Gh(cfg)
    git(main, "fetch", "--prune", "origin", check=False)
    remove, skipped = [], []
    for w in list_worktrees(main):
        if w["main"]:
            continue
        if w["missing"]:
            print("missing: %s; if it is really gone, run wt rm %s --force" % (
                w["path"], os.path.basename(w["path"])))
            continue
        if not w["branch"]:
            continue  # detached: no PR or remote signal says it is finished
        b, path = w["branch"], w["path"]
        prs = _pr_state(gh, main, b) or []
        states = {p.get("state") for p in prs}
        reason = None
        if "OPEN" not in states and states & {"MERGED", "CLOSED"}:
            reason = "PR %s" % "/".join(sorted(s.lower() for s in states if s in ("MERGED", "CLOSED")))
        elif (git(main, "config", "--get", "branch.%s.remote" % b, check=False).returncode == 0
              and not has_remote_ref(main, b)):
            reason = "remote branch is gone"
        if not reason:
            continue
        if is_dirty(path):
            skipped.append((path, b, "uncommitted changes"))
            continue
        covered = gout(path, "rev-parse", "HEAD") in [p.get("headRefOid") for p in prs
                                                        if p.get("state") == "MERGED"]
        if not covered and unpushed(main, path, b, any_remote=True):
            skipped.append((path, b, "commits not on any remote branch"))
            continue
        remove.append((path, b, reason))
    for path, b, why in skipped:
        print("skipped  %s (%s): %s" % (path, b, why))
    if not remove:
        print("nothing to remove")
        return 0
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    for path, b, why in remove:
        print("straggler %s (%s): %s" % (path, b, why))
        if args.yes:
            go = True
        elif tty:
            go = input("remove %s? [y/N] " % path).strip().lower() in ("y", "yes")
        else:
            go = False
        if go:
            remove_worktree(main, path, b)
            print("removed  %s" % path)
    if not args.yes and not tty:
        print("not a terminal and no --yes: listed only, nothing removed")
    return 0


def cmd_install_hooks(args, cfg):
    return install_hooks(args.repo, dry_run=args.dry_run)


# ------------------------------------------------------------------ cli
def build_parser():
    ap = argparse.ArgumentParser(prog="wt.py", description="worktree + PR lifecycle")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("new")
    p.add_argument("name")
    p = sub.add_parser("pr")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("merge")
    p.add_argument("name", nargs="?")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("list")
    p = sub.add_parser("rm")
    p.add_argument("name")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("gc")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("install-hooks")
    p.add_argument("repo", nargs="?")
    p.add_argument("--dry-run", action="store_true")
    return ap


COMMANDS = {"new": cmd_new, "pr": cmd_pr, "merge": cmd_merge, "list": cmd_list, "rm": cmd_rm,
            "gc": cmd_gc, "install-hooks": cmd_install_hooks}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_usage(sys.stderr)
        return 1
    try:
        return COMMANDS[args.cmd](args, load_cfg())
    except Die as exc:
        print("wt: %s" % exc.msg, file=sys.stderr)
        return exc.code


# ------------------------------------------------------------------ selftest
STUB = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["WT_STUB_DIR"]
a = sys.argv[1:]
with open(os.path.join(d, "gh.log"), "a") as fh:
    fh.write(json.dumps({"argv": a, "token": os.environ.get("GH_TOKEN")}) + "\n")
def canned(*names):
    for n in names:
        p = os.path.join(d, n)
        if os.path.exists(p):
            sys.stdout.write(open(p).read())
            return 0
    sys.stderr.write("stub: no canned file for %s\n" % (names,))
    return 1
if a[:2] == ["pr", "list"]:
    st = a[a.index("--state") + 1]
    head = a[a.index("--head") + 1]
    if os.path.exists(os.path.join(d, "fail_list")):
        sys.stderr.write("stub: gh pr list failed\n"); sys.exit(1)
    for n in ("pr_list_%s_%s.json" % (st, head), "pr_list_%s.json" % st):
        if os.path.exists(os.path.join(d, n)):
            sys.exit(canned(n))
    print("[]"); sys.exit(0)
if a[:2] == ["pr", "view"]:
    sys.exit(canned("pr_view.json"))
if a[:2] == ["api", "user"]:
    sys.exit(canned("login"))
if a[0] == "api" and a[-1].endswith("/comments"):
    sys.exit(canned("comments.json"))
if a[:2] == ["pr", "create"]:
    print("https://github.com/example/proj/pull/7"); sys.exit(0)
if a[:2] == ["pr", "merge"]:
    h = os.path.join(d, "merge_hook")
    if os.path.exists(h):
        os.system(open(h).read())
    sys.exit(0)
sys.stderr.write("stub: unhandled %s\n" % a); sys.exit(1)
'''


def selftest():
    import tempfile
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
        if not cond:
            fails.append(name)

    here = os.path.realpath(__file__)
    with tempfile.TemporaryDirectory() as t0:
        T = os.path.realpath(t0)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        stubdir = os.path.join(T, "stub")
        os.makedirs(stubdir)
        gh_path = os.path.join(T, "gh-stub")
        with open(gh_path, "w") as fh:
            fh.write(STUB)
        os.chmod(gh_path, 0o755)
        cfgfile = os.path.join(T, "config.toml")
        env.update(HOME=os.path.join(T, "home"), SESSION_NOTES_CONFIG=cfgfile, WT_GH=gh_path,
                   WT_STUB_DIR=stubdir, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        os.makedirs(env["HOME"])
        os.environ.update({k: env[k] for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")})

        def write_cfg(extra="", cap=1024, public=(), terms=None):
            with open(cfgfile, "w") as fh:
                fh.write('[wt]\ntoken_command = "echo test-token"\nsize_cap_kb = %d\n' % cap)
                fh.write("public_repos = [%s]\n" % ", ".join('"%s"' % p for p in public))
                if terms:
                    fh.write('leak_terms = "%s"\n' % terms)
                fh.write(extra)

        def sh(args, cwd, stdin=None):
            r = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
            return r.returncode, r.stdout, r.stderr

        def g(cwd, *a):
            rc, out, err = sh(["git"] + list(a), cwd)
            if rc != 0:
                raise RuntimeError("git %s: %s" % (a, err))
            return out.strip()

        def wt(cwd, *a):
            return sh([sys.executable, here] + list(a), cwd)

        def log():
            p = os.path.join(stubdir, "gh.log")
            return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

        def reset_stub():
            for f in os.listdir(stubdir):
                os.remove(os.path.join(stubdir, f))

        def canned(name, obj):
            with open(os.path.join(stubdir, name), "w") as fh:
                fh.write(obj if isinstance(obj, str) else json.dumps(obj))

        def mkrepo(name, tracked_claude=False, with_origin=True):
            r = os.path.join(T, name)
            os.makedirs(r)
            g(r, "init", "-q")
            g(r, "symbolic-ref", "HEAD", "refs/heads/main")
            open(os.path.join(r, "README"), "w").write("hi\n")
            if tracked_claude:
                open(os.path.join(r, "CLAUDE.md"), "w").write("x\n")
            g(r, "add", "-A")
            g(r, "commit", "-q", "-m", "init", "--no-verify")
            if with_origin:
                o = os.path.join(T, name + ".git")
                g(T, "init", "-q", "--bare", o)
                g(r, "remote", "add", "origin", o)
                g(r, "push", "-q", "-u", "origin", "main")
                g(r, "remote", "set-head", "origin", "main")
            return r

        def commit(wtp, fname, text="x\n", msg="c"):
            open(os.path.join(wtp, fname), "w").write(text)
            g(wtp, "add", fname)
            return sh(["git", "commit", "-q", "-m", msg], wtp)

        write_cfg()
        R = mkrepo("proj")

        # parse_verdict / verdict_line
        sha = "a" * 40
        check("parse: approve", parse_verdict("Review verdict: APPROVE " + sha + "\nbody") == (APPROVE, sha))
        check("parse: changes, CRLF", parse_verdict("Review verdict: CHANGES %s\r\nx" % sha) == (CHANGES, sha))
        check("parse: not first line", parse_verdict("hello\nReview verdict: APPROVE " + sha) is None)
        check("parse: leading space/quote", parse_verdict(" Review verdict: APPROVE " + sha) is None
              and parse_verdict("> Review verdict: APPROVE " + sha) is None)
        check("parse: short or upper sha", parse_verdict("Review verdict: APPROVE abc123") is None
              and parse_verdict("Review verdict: APPROVE " + "A" * 40) is None)
        check("parse: trailing junk", parse_verdict("Review verdict: APPROVE %s extra" % sha) is None)
        check("parse: lowercase verdict", parse_verdict("Review verdict: approve " + sha) is None)
        check("parse: empty/None", parse_verdict("") is None and parse_verdict(None) is None)
        check("verdict_line round trip", parse_verdict(verdict_line(APPROVE, sha)) == (APPROVE, sha))
        try:
            verdict_line("MAYBE", sha)
            check("verdict_line rejects bad verdict", False)
        except ValueError:
            check("verdict_line rejects bad verdict", True)

        # location rule (pure)
        cfg0 = notes_config.load(cfgfile)
        check("location: default nested", worktree_path(R, "x", cfg0) == os.path.join(R, ".worktrees", "x"))
        R2 = mkrepo("tracked", tracked_claude=True, with_origin=False)
        check("location: tracked CLAUDE.md -> hidden sibling",
              worktree_path(R2, "x", cfg0) == os.path.join(T, ".tracked-worktrees", "x"))
        cfg1 = dict(cfg0, wt=dict(cfg0["wt"], root=os.path.join(T, "wts")))
        check("location: [wt].root", worktree_path(R, "x", cfg1) == os.path.join(T, "wts", "proj", "x"))
        cfg2 = dict(cfg1, scopes=[{"name": "a", "root": T, "worktree_root": os.path.join(T, "sw-outer")},
                                  {"name": "b", "root": R, "worktree_root": os.path.join(T, "sw-inner")},
                                  {"name": "c", "root": R, "worktree_root": ""}])
        check("location: deepest scope with worktree_root wins over [wt].root",
              worktree_path(R, "x", cfg2) == os.path.join(T, "sw-inner", "x"))

        # new
        rc, out, err = wt(R, "new", "feat1")
        P1 = os.path.join(R, ".worktrees", "feat1")
        check("new: creates the worktree and prints its path", rc == 0 and out.strip() == P1
              and os.path.isdir(P1), (rc, out, err))
        check("new: branch from origin/main", g(P1, "branch", "--show-current") == "feat1")
        ex = open(os.path.join(R, ".git", "info", "exclude")).read().splitlines()
        check("new: exclude line added once", ex.count(EXCLUDE_LINE) == 1)
        check("new: hook installed on first sight",
              hook_present(os.path.join(R, ".git")), err)
        rc, out, err = wt(R, "new", "feat1")
        check("new: refuses an existing path/branch", rc == 1, (rc, err))
        rc, out, err = wt(R, "new", "bad/name")
        check("new: refuses a slash", rc == 1)
        wt(R, "new", "feat2")
        ex = open(os.path.join(R, ".git", "info", "exclude")).read().splitlines()
        check("new: exclude idempotent", ex.count(EXCLUDE_LINE) == 1)
        check("new: main checkout stays clean (worktrees excluded)", gout(R, "status", "--porcelain") == "")
        rc, out, err = wt(P1, "new", "fromwt")
        check("new: works from inside a worktree",
              rc == 0 and out.strip() == os.path.join(R, ".worktrees", "fromwt"), (rc, out, err))
        rc, out, err = wt(R, "rm", "fromwt")
        check("rm: fresh clean worktree removes", rc == 0 and not os.path.exists(os.path.join(R, ".worktrees", "fromwt")), err)

        # pr
        r = commit(P1, "a.txt", "a\n", "add a")
        check("hook lets a clean commit through", r[0] == 0, r)
        reset_stub()
        rc, out, err = wt(P1, "pr", "--dry-run", "--title", "My change")
        check("pr --dry-run prints push + create, runs nothing remote",
              rc == 0 and "git push -u origin feat1" in out and "gh pr create" in out
              and "'My change'" in out and not log()
              and g(R, "ls-remote", "--heads", "origin", "feat1") == "", (rc, out, err))
        open(os.path.join(P1, "dirty.txt"), "w").write("d\n")
        rc, out, err = wt(P1, "pr", "--title", "t")
        check("pr refuses a dirty tree (4)", rc == 4 and not log(), (rc, err))
        os.remove(os.path.join(P1, "dirty.txt"))
        rc, out, err = wt(R, "pr")
        check("pr refuses outside a worktree (2)", rc == 2, (rc, err))
        canned("pr_list_merged.json", [{"number": 3, "url": "https://github.com/example/proj/pull/3"}])
        rc, out, err = wt(P1, "pr", "--title", "t")
        check("pr refuses a branch with a MERGED PR (5)", rc == 5 and "--state" in log()[0]["argv"]
              and "merged" in log()[0]["argv"] and g(R, "ls-remote", "--heads", "origin", "feat1") == "",
              (rc, err, log()))
        reset_stub()
        open(os.path.join(stubdir, "fail_list"), "w").write("1")
        rc, out, err = wt(P1, "pr", "--title", "Real title")
        check("pr: gh lookup failure warns and proceeds", rc == 0 and "warning" in err
              and "pull/7" in out and g(R, "ls-remote", "--heads", "origin", "feat1") != "", (rc, out, err))
        check("pr: token exported to gh", all(e["token"] == "test-token" for e in log()))
        check("pr: create carries the title",
              any(e["argv"][:2] == ["pr", "create"] and "Real title" in e["argv"] for e in log()))

        # merge gate
        head = g(P1, "rev-parse", "HEAD")
        view = {"number": 7, "headRefOid": head, "state": "OPEN",
                "url": "https://github.com/example/proj/pull/7"}

        def setup_gate(comments, login="me", v=view):
            reset_stub()
            canned("pr_view.json", v)
            canned("login", login + "\n")
            canned("comments.json", comments)

        def cm(login, body, ts, i):
            return {"id": i, "created_at": ts, "user": {"login": login}, "body": body}

        setup_gate([])
        rc, out, err = wt(P1, "merge")
        check("merge refuses with no verdict (6)", rc == 6 and "no review verdict" in err, (rc, err))
        setup_gate([cm("me", verdict_line(APPROVE, head), "2026-01-01T00:00:00Z", 1),
                    cm("me", verdict_line(CHANGES, head), "2026-01-02T00:00:00Z", 2)])
        rc, out, err = wt(P1, "merge")
        check("merge refuses when the newest is CHANGES (6)", rc == 6 and "newest verdict is CHANGES" in err, (rc, err))
        setup_gate([cm("me", verdict_line(APPROVE, "b" * 40), "2026-01-01T00:00:00Z", 1)])
        rc, out, err = wt(P1, "merge")
        check("merge refuses an APPROVE on a stale sha (6)", rc == 6 and "approved sha" in err, (rc, err))
        setup_gate([cm("stranger", verdict_line(APPROVE, head), "2026-01-01T00:00:00Z", 1)])
        rc, out, err = wt(P1, "merge")
        check("merge refuses an APPROVE from another author (6)", rc == 6 and "other authors" in err, (rc, err))
        setup_gate([cm("me", verdict_line(CHANGES, head), "2026-01-01T00:00:00Z", 1),
                    cm("stranger", verdict_line(APPROVE, head), "2026-01-02T00:00:00Z", 2)])
        rc, out, err = wt(P1, "merge")
        check("a stranger's newer APPROVE does not override my CHANGES (6)", rc == 6, (rc, err))
        check("refusals never reached gh pr merge", not any(e["argv"][:2] == ["pr", "merge"] for e in log()))

        ok_comments = [cm("me", "looks fine\n", "2026-01-01T00:00:00Z", 1),
                       cm("me", verdict_line(APPROVE, head) + "\nnotes", "2026-01-02T00:00:00Z", 2)]
        setup_gate(ok_comments)
        rc, out, err = wt(P1, "merge", "--dry-run")
        check("merge --dry-run checks the gate and changes nothing",
              rc == 0 and "gh pr merge 7 --squash --delete-branch --match-head-commit %s -R example/proj" % head in out
              and os.path.isdir(P1) and not any(e["argv"][:2] == ["pr", "merge"] for e in log()), (rc, out, err))
        rc, out, err = wt(R, "merge", "feat1")
        merge_calls = [e["argv"] for e in log() if e["argv"][:2] == ["pr", "merge"]]
        check("merge passes with a correct APPROVE, by name from the main checkout",
              rc == 0 and merge_calls == [["pr", "merge", "7", "--squash", "--delete-branch",
                                           "--match-head-commit", head, "-R", "example/proj"]], (rc, out, err, merge_calls))
        check("merge removed the worktree and the local branch",
              not os.path.exists(P1) and g(R, "branch", "--list", "feat1") == "")
        check("merge reports the main checkout fast-forward state",
              "main checkout" in out and ("fast-forwarded" in out or "up to date" in out), out)

        # merge: dirty / unpushed
        P2 = os.path.join(R, ".worktrees", "feat2")
        commit(P2, "b.txt")
        reset_stub()
        rc, out, err = wt(P2, "merge")
        check("merge refuses unpushed commits (4)", rc == 4 and "not origin/feat2" in err and not log(), (rc, err))
        g(P2, "push", "-q", "-u", "origin", "feat2")
        open(os.path.join(P2, "d.txt"), "w").write("d\n")
        rc, out, err = wt(P2, "merge")
        check("merge refuses a dirty tree (4)", rc == 4 and "uncommitted" in err, (rc, err))
        os.remove(os.path.join(P2, "d.txt"))

        # merge: main checkout not fast-forwarded when dirty
        head2 = g(P2, "rev-parse", "HEAD")
        setup_gate([cm("me", verdict_line(APPROVE, head2), "2026-01-02T00:00:00Z", 2)], v=dict(view, headRefOid=head2))
        open(os.path.join(R, "stray.txt"), "w").write("s\n")
        rc, out, err = wt(P2, "merge")
        check("merge leaves a dirty main checkout alone and says so",
              rc == 0 and "not fast-forwarded" in out, (rc, out, err))
        os.remove(os.path.join(R, "stray.txt"))

        # list / gc / rm
        def pushed_wt(name, files=True):
            wt(R, "new", name)
            p = os.path.join(R, ".worktrees", name)
            if files:
                commit(p, name + ".txt", name + "\n")
                g(p, "push", "-q", "-u", "origin", name)
            return p, g(p, "rev-parse", "HEAD")

        gm, gm_sha = pushed_wt("gcmerged")
        gd, gd_sha = pushed_wt("gcdirty")
        gu, gu_sha = pushed_wt("gcunpushed")
        go_, go_sha = pushed_wt("gcopen")
        open(os.path.join(gd, "wip.txt"), "w").write("w\n")
        commit(gu, "more.txt", "m\n", "local only")
        reset_stub()
        mrg = lambda sha_: [{"number": 9, "state": "MERGED", "headRefOid": sha_}]
        canned("pr_list_all_gcmerged.json", mrg(gm_sha))
        canned("pr_list_all_gcdirty.json", mrg(gd_sha))
        canned("pr_list_all_gcunpushed.json", mrg("c" * 40))
        canned("pr_list_all_gcopen.json", [{"number": 10, "state": "OPEN", "headRefOid": go_sha}])
        rc, out, err = wt(R, "list")
        check("list shows worktrees with dirty and PR state",
              rc == 0 and "gcdirty" in out and "dirty" in out and "pr=merged" in out and "pr=open" in out, out)
        rc, out, err = wt(R, "gc")
        check("gc without a TTY and without --yes only lists",
              rc == 0 and "gcmerged" in out and os.path.isdir(gm) and "listed only" in out, (rc, out, err))
        check("gc skips a dirty and an unpushed straggler, says why",
              "skipped" in out and "uncommitted changes" in out and "commits not on any remote branch" in out, out)
        check("gc never lists an OPEN PR's worktree", "straggler %s" % go_ not in out, out)
        rc, out, err = wt(R, "gc", "--yes")
        check("gc --yes removes only the clean merged one",
              rc == 0 and not os.path.exists(gm) and os.path.isdir(gd) and os.path.isdir(gu)
              and os.path.isdir(go_) and g(R, "branch", "--list", "gcmerged") == "", (rc, out, err))
        rc, out, err = wt(R, "rm", "gcdirty")
        check("rm refuses a dirty worktree (4)", rc == 4, (rc, err))
        rc, out, err = wt(R, "rm", "gcunpushed")
        check("rm refuses unpushed commits (4)", rc == 4, (rc, err))
        rc, out, err = wt(R, "rm", "gcunpushed", "--force")
        check("rm --force removes it", rc == 0 and not os.path.exists(gu), (rc, err))
        rc, out, err = wt(R, "rm", "nope")
        check("rm unknown name fails (1)", rc == 1)

        # B1: a push (new work) landing around the merge keeps the worktree
        rw, rw_sha = pushed_wt("racewt")
        setup_gate([cm("me", verdict_line(APPROVE, rw_sha), "2026-01-02T00:00:00Z", 2)],
                   v=dict(view, headRefOid=rw_sha))
        canned("merge_hook", "echo late > %s" % shlex.quote(os.path.join(rw, "late.txt")))
        rc, out, err = wt(rw, "merge")
        check("merge keeps a worktree that gained work during the merge, and says so",
              rc == 0 and os.path.isdir(rw) and "NOT removed" in out
              and g(R, "branch", "--list", "racewt") != "", (rc, out, err))
        check("merge pins the gated head with --match-head-commit",
              any(e["argv"][:2] == ["pr", "merge"] and e["argv"][e["argv"].index("--match-head-commit") + 1] == rw_sha
                  for e in log()))
        os.remove(os.path.join(stubdir, "merge_hook"))

        # B2: detached HEAD with unpushed commits
        dw, _ = pushed_wt("detwt")
        commit(dw, "lost.txt", "l\n", "unpushed on a detached head")
        g(dw, "checkout", "-q", "--detach")
        rc, out, err = wt(R, "rm", "detwt")
        check("rm refuses a detached worktree holding unpushed commits (4)",
              rc == 4 and "detached" in err and os.path.isdir(dw), (rc, out, err))
        rc, out, err = wt(R, "rm", "detwt", "--force")
        check("rm --force removes the detached worktree", rc == 0 and not os.path.exists(dw), (rc, err))

        # N2: a hand-deleted worktree directory
        import shutil
        gw, _ = pushed_wt("ghostwt")
        gx, _ = pushed_wt("ghostx")
        shutil.rmtree(gw)
        shutil.rmtree(gx)
        rc, out, err = wt(R, "list")
        check("list shows missing worktrees as their own rows and does not crash",
              rc == 0 and "ghostwt" in out and "wt rm ghostwt --force" in out, (rc, out, err))
        rc, out, err = wt(R, "gc", "--yes")
        wl = g(R, "worktree", "list")
        check("gc --yes only lists missing worktrees, never removes them",
              rc == 0 and "missing: " in out and "wt rm ghostwt --force" in out
              and "ghostwt" in wl and "ghostx" in wl, (rc, out, err))
        rc, out, err = wt(R, "rm", "ghostwt")
        check("rm of a missing worktree without --force is refused (4)", rc == 4 and "--force" in err, (rc, err))
        rc, out, err = wt(R, "rm", "ghostwt", "--force")
        wl = g(R, "worktree", "list")
        check("rm --force removes only that one record, the sibling stays",
              rc == 0 and "ghostwt" not in wl and "ghostx" in wl, (rc, err, wl))
        wt(R, "rm", "ghostx", "--force")

        # N3: hook install failing inside `new` is a warning, the path still prints
        R13 = mkrepo("newwarn")
        h13 = os.path.join(R13, ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(h13), exist_ok=True)
        os.symlink(os.path.join(T, "nowhere"), h13)
        rc, out, err = wt(R13, "new", "w3")
        check("new: a hook-install failure warns, still prints the path, exit 0",
              rc == 0 and out.strip() == os.path.join(R13, ".worktrees", "w3") and "warning" in err, (rc, out, err))

        # N4: an edited verdict comment is refused
        edited = dict(cm("me", verdict_line(APPROVE, sha), "2026-01-01T00:00:00Z", 5),
                      updated_at="2026-01-03T00:00:00Z")
        ok_, msg_ = check_gate([edited], "me", sha)
        check("gate refuses an edited verdict comment", not ok_ and "edited" in msg_, msg_)
        ok_, msg_ = check_gate([dict(edited, updated_at=edited["created_at"])], "me", sha)
        check("gate accepts an unedited one (updated_at == created_at)", ok_, msg_)

        # token command failure
        write_cfg()
        with open(cfgfile, "w") as fh:
            fh.write('[wt]\ntoken_command = "exit 1"\n')
        rc, out, err = wt(P2 if os.path.isdir(P2) else go_, "merge")
        check("failing token_command is a loud exit 3 naming the knob",
              rc == 3 and "wt.token_command" in err, (rc, err))
        with open(cfgfile, "w") as fh:
            fh.write('[wt]\ntoken_command = "true"\n')
        rc, out, err = wt(go_, "merge")
        check("empty token output is exit 3", rc == 3, (rc, err))
        write_cfg()

        # install-hooks
        R3 = mkrepo("fresh", with_origin=False)
        rc, out, err = wt(T, "install-hooks", R3)
        hp = os.path.join(R3, ".git", "hooks", "pre-commit")
        check("install-hooks: fresh install writes an executable shim",
              rc == 0 and os.path.isfile(hp) and os.access(hp, os.X_OK) and HOOK_MARKER in open(hp).read(), (rc, out, err))
        before = open(hp).read()
        rc, out, err = wt(T, "install-hooks", R3)
        check("install-hooks: idempotent", rc == 0 and "already installed" in out and open(hp).read() == before)
        check("install-hooks: no .pre-session-notes created on re-run", not os.path.exists(hp + ".pre-session-notes"))

        R4 = mkrepo("chain", with_origin=False)
        h4 = os.path.join(R4, ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(h4), exist_ok=True)
        flag = os.path.join(T, "old-hook-ran")
        with open(h4, "w") as fh:
            fh.write("#!/bin/sh\necho ran >> %s\nexit 0\n" % shlex.quote(flag))
        os.chmod(h4, 0o755)
        rc, out, err = wt(T, "install-hooks", R4)
        check("install-hooks: existing hook moved aside and chained",
              rc == 0 and os.path.exists(h4 + ".pre-session-notes") and "still runs" in out, (rc, out, err))
        r = commit(R4, "x.txt")
        check("chained: the old hook still runs on commit", r[0] == 0 and os.path.exists(flag), r)
        with open(h4 + ".pre-session-notes", "w") as fh:
            fh.write("#!/bin/sh\necho old-hook-says-no\nexit 1\n")
        r = commit(R4, "y.txt")
        check("chained: the old hook's failure fails the commit",
              r[0] != 0 and "old-hook-says-no" in (r[1] + r[2]), r)
        R5 = mkrepo("taken", with_origin=False)
        h5 = os.path.join(R5, ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(h5), exist_ok=True)
        open(h5, "w").write("#!/bin/sh\nexit 0\n")
        open(h5 + ".pre-session-notes", "w").write("#!/bin/sh\n# other\n")
        rc, out, err = wt(T, "install-hooks", R5)
        check("install-hooks: refuses when the chained name is taken, leaves the hook",
              rc == 1 and open(h5).read() == "#!/bin/sh\nexit 0\n", (rc, err))
        R6 = mkrepo("managed", with_origin=False)
        g(R6, "config", "core.hooksPath", "myhooks")
        rc, out, err = wt(T, "install-hooks", R6)
        check("install-hooks: core.hooksPath repo is skipped, nothing written",
              rc == 0 and "core.hooksPath=myhooks" in out and not os.path.exists(os.path.join(R6, ".git", "hooks", "pre-commit"))
              and g(R6, "config", "core.hooksPath") == "myhooks", (rc, out, err))

        # pre-commit checks
        R7 = mkrepo("guarded", with_origin=False)
        wt(T, "install-hooks", R7)
        fake_key = "sk-ant-" + "a1b2c3d4" * 3
        r = commit(R7, "k.txt", "key = %s\n" % fake_key)
        check("pre-commit catches an API key", r[0] != 0 and "k.txt:1" in r[1] + r[2] and "secret" in r[1] + r[2], r)
        g(R7, "reset", "-q")
        r = commit(R7, "p.txt", "-----BEGIN RSA PRIVATE" + " KEY-----\nabc\n")
        check("pre-commit catches a PEM header", r[0] != 0 and "PEM" in r[1] + r[2], r)
        g(R7, "reset", "-q")
        write_cfg(cap=1)
        r = commit(R7, "big.bin", "z" * 3000)
        check("pre-commit catches an oversize file", r[0] != 0 and "size_cap_kb" in r[1] + r[2], r)
        g(R7, "reset", "-q")
        write_cfg()
        terms = os.path.join(T, "terms.txt")
        open(terms, "w").write("# private\nzebracorp\n")
        write_cfg(terms=terms)
        r = commit(R7, "t.txt", "ZebraCorp ships %s\n" % chr(0x2014))
        check("leak term ignored when the repo is not marked public (and no em-dash rule)", r[0] == 0, r)
        r = commit(R7, "t2.txt", "zebracorp again\n")
        g(R7, "reset", "-q", "--hard", "HEAD~1")
        write_cfg(public=[R7], terms=terms)
        r = commit(R7, "t3.txt", "about zebracorp\n")
        check("leak term caught when the repo is public", r[0] != 0 and "t3.txt:1" in r[1] + r[2]
              and "private term" in r[1] + r[2], r)
        g(R7, "reset", "-q")
        r = commit(R7, "t4.txt", "fine %s text\n" % chr(0x2014))
        check("public repo: em dash and clean text pass (house style is not generic)", r[0] == 0, r)
        write_cfg(public=[R7], terms=os.path.join(T, "missing-terms.txt"))
        r = commit(R7, "t5.txt", "ok\n")
        check("public repo with a missing terms file fails loudly",
              r[0] != 0 and "does not exist" in r[1] + r[2], r)

        # B3: the shim fails closed when its script is gone, and install repairs it
        write_cfg()
        R9 = mkrepo("moved", with_origin=False)
        wt(T, "install-hooks", R9)
        h9 = os.path.join(R9, ".git", "hooks", "pre-commit")
        with open(h9, "w") as fh:
            fh.write(build_shim("/nonexistent/session-notes/githooks/pre-commit", R9))
        os.chmod(h9, 0o755)
        r = commit(R9, "m.txt")
        check("shim with a missing script refuses the commit, naming the path and the repair",
              r[0] != 0 and "/nonexistent/session-notes/githooks/pre-commit is missing" in r[1] + r[2]
              and "install-hooks" in r[1] + r[2], r)
        Rsp = mkrepo("with space", with_origin=False)
        wt(T, "install-hooks", Rsp)
        hsp = os.path.join(Rsp, ".git", "hooks", "pre-commit")
        with open(hsp, "w") as fh:
            fh.write(build_shim("/nonexistent/session-notes/githooks/pre-commit", Rsp))
        os.chmod(hsp, 0o755)
        r = commit(Rsp, "s.txt")
        check("repair line quotes a repo path with a space",
              r[0] != 0 and ("install-hooks " + shlex.quote(Rsp)) in r[1] + r[2], r)
        rc, out, err = wt(T, "install-hooks", R9)
        check("install-hooks repairs a shim whose recorded path is gone", rc == 0 and "repaired" in out, (rc, out, err))
        r = commit(R9, "m.txt")
        check("commit works again after the repair", r[0] == 0, r)
        rc, out, err = wt(T, "install-hooks", R9)
        check("repair is idempotent when the path matches", rc == 0 and "already installed" in out, (rc, out))
        with open(h4, "w") as fh:
            fh.write(build_shim("/nonexistent/x/pre-commit", R4))
        rc, out, err = wt(T, "install-hooks", R4)
        check("repair keeps the chain to pre-commit.pre-session-notes",
              rc == 0 and "repaired" in out and os.path.exists(h4 + ".pre-session-notes")
              and "pre-commit.pre-session-notes" in open(h4).read(), (rc, out, err))
        R10 = mkrepo("dangling", with_origin=False)
        h10 = os.path.join(R10, ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(h10), exist_ok=True)
        os.symlink(os.path.join(T, "nowhere"), h10)
        rc, out, err = wt(T, "install-hooks", R10)
        check("a dangling-symlink pre-commit is refused cleanly (no traceback), untouched",
              rc == 1 and "dangling symlink" in err and "Traceback" not in err and os.path.islink(h10), (rc, err))

        # shim: a hostile repo path is never evaluated by the shell
        evil = os.path.join(T, "d$(touch PWNED)")
        os.makedirs(evil)
        R14 = mkrepo("x", with_origin=False)
        evil_repo = os.path.join(evil, "r")
        shutil_ = __import__("shutil")
        shutil_.copytree(R14, evil_repo, symlinks=True)
        h14 = os.path.join(evil_repo, ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(h14), exist_ok=True)
        with open(h14, "w") as fh:
            fh.write(build_shim("/nonexistent/session-notes/githooks/pre-commit", evil_repo))
        os.chmod(h14, 0o755)
        r = commit(evil_repo, "e.txt")
        check("shim: a repo path with $(...) is printed literally, never executed",
              r[0] != 0 and "d$(touch PWNED)" in r[1] + r[2] and "install-hooks" in r[1] + r[2]
              and not os.path.exists(os.path.join(evil_repo, "PWNED")) and not os.path.exists(os.path.join(T, "PWNED"))
              and not any("PWNED" in f and not f.startswith("d$") for f in os.listdir(evil)), r)

        # B4: --dry-run writes nothing, through wt.py and through install.sh in either arg order
        R11 = mkrepo("dry", with_origin=False)
        h11 = os.path.join(R11, ".git", "hooks", "pre-commit")
        rc, out, err = wt(T, "install-hooks", "--dry-run", R11)
        check("install-hooks --dry-run writes nothing", rc == 0 and "would:" in out and not os.path.exists(h11), (rc, out, err))
        inst = os.path.join(os.path.dirname(HERE), "install.sh")
        for order in (["--dry-run", "--git-hooks", R11], ["--git-hooks", R11, "--dry-run"]):
            rc, out, err = sh(["sh", inst] + order, T)[0:3]
            check("install.sh %s writes nothing" % " ".join(order[:1] + order[2:]),
                  rc == 0 and "would:" in out and not os.path.exists(h11), (rc, out, err))
        rc, out, err = sh(["sh", inst, "--git-hooks", ""], T)
        check("install.sh --git-hooks with an empty repo is a usage error (2)", rc == 2 and "non-empty" in err, (rc, err))
        rc, out, err = sh(["sh", inst, "--git-hooks", R11], T)
        check("install.sh --git-hooks installs", rc == 0 and os.path.isfile(h11), (rc, out, err))

        # B5: type change, failed diff, unreadable blob
        R8 = mkrepo("typechg", with_origin=False)
        wt(T, "install-hooks", R8)
        os.symlink("README", os.path.join(R8, "lnk"))
        g(R8, "add", "lnk")
        r = sh(["git", "commit", "-q", "-m", "link"], R8)
        os.remove(os.path.join(R8, "lnk"))
        r = commit(R8, "lnk", "key = %s\n" % fake_key)
        check("pre-commit catches a key in a symlink replaced by a file (type change)",
              r[0] != 0 and "lnk:1" in r[1] + r[2], r)
        nongit = os.path.join(T, "nongit")
        os.makedirs(nongit)
        rc, out, err = sh([sys.executable, hook_script()], nongit)
        check("hook fails loudly when git diff fails", rc == 1 and "git diff --cached failed" in out, (rc, out, err))
        R15 = mkrepo("gitlink", with_origin=False)
        wt(T, "install-hooks", R15)
        g(R15, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/lib" % ("1" * 40))
        r = sh(["git", "commit", "-q", "-m", "add submodule pointer"], R15)
        check("a staged gitlink (submodule) passes the hook", r[0] == 0, r)
        R12 = mkrepo("blobless", with_origin=False)
        wt(T, "install-hooks", R12)
        open(os.path.join(R12, "f.txt"), "w").write("content to lose\n")
        g(R12, "add", "f.txt")
        oid = g(R12, "rev-parse", ":f.txt")
        objp = os.path.join(R12, ".git", "objects", oid[:2], oid[2:])
        os.chmod(objp, 0o644)
        os.remove(objp)
        r = sh(["git", "commit", "-q", "-m", "x"], R12)
        check("an unreadable staged blob is a finding, not a skip",
              r[0] != 0 and "could not be read" in r[1] + r[2], r)

    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
