#!/usr/bin/env python3
"""review.py -- prepare a reviewer prompt for a PR, and post the reviewer's verdict on it.

Run from inside a checkout of the PR's repo (a worktree is fine). Implements
rules/review-contract.md; the verdict marker and its parser live in bin/wt.py and are imported
here, never restated.

    review.py prepare <PR>
        Fetch the PR's head sha, title, body and diff, run the test-edit check on the diff, and
        write a reviewer prompt plus meta.json under the state dir
        (<state_dir>/review/<owner>-<repo>-<PR>/). Prints the prompt path, the head sha and the
        review round. Says so, and still exits 0, when this is the last review the contract's
        fix-round limit allows or the limit is already passed. From round 2 on it also runs the
        test-edit check on the range since the last reviewed head (the previous meta.json head,
        else the sha in the newest own verdict comment) and names that verdict comment.
    review.py post <PR> APPROVE|CHANGES --body-file FILE [--dry-run]
        Post the reviewer's reply as the verdict comment: first line
        `Review verdict: <V> <head sha>`, then the body. Refuses when the PR head moved since
        `prepare` (the reviewer read a different commit), when `prepare` was never run, when the
        body is empty, when its last non-empty line is not exactly `VERDICT: APPROVE` or
        `VERDICT: CHANGES`, or when that line disagrees with <V>. A CHANGES that brings the
        account's CHANGES verdicts past the fix-round limit prints a stop-and-report line.
        --dry-run prints the comment and posts nothing.
    review.py --selftest

The comment is posted with the token from `[wt].token_command` (the account whose verdicts
`wt merge` trusts). The test-edit check's extra globs come from `[review].test_globs`.

Exit codes: 0 ok; 1 fatal, no subcommand, or a refusal (no prepare, empty body, a body that does
not end in its own VERDICT line, verdict mismatch, PR not open); 2 a usage error (argparse: bad
verdict word, unknown flag); 3 token command failed; 4 the PR head moved since prepare. A failing
--selftest exits 1.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import notes_config  # noqa: E402
import test_edit_check  # noqa: E402
import wt  # noqa: E402

CONTRACT = os.path.join(os.path.dirname(HERE), "rules", "review-contract.md")
BINDIR = HERE                # where on_verdict_posted looks for live.py
DIFF_INLINE_CAP = 200 * 1024
FIX_ROUND_LIMIT = 2          # the contract allows at most this many fix rounds
_BODY_VERDICT = re.compile(r"^VERDICT: (%s|%s)$" % (wt.APPROVE, wt.CHANGES))

INSTRUCTIONS = """\
You are the reviewer. You have fresh context: no builder transcript and no opinion of this
change comes with this prompt. Your job is to REFUTE the change, not to grade it: find the
input, state or sequence that makes it wrong.

- Start with the test-edit check output at the top: any edit to a test, fixture or guard that
  would have caught this change is the first thing to examine.
- Read the files at the head sha for context beyond the diff, not only the diff.
- For EVERY finding quote the file, the line number and the lines themselves. A finding with no
  quote is not a finding.
- For each finding say what breaks, how to trigger it, and whether it blocks the merge.
- Verify the change against the spec below, including what it should refuse or leave alone.
- Do not post anything and do not change any file; report to the session that spawned you.

END your reply with a final line that is exactly one of these, and nothing after it:

VERDICT: APPROVE
VERDICT: CHANGES

APPROVE means nothing blocks the merge. CHANGES means at least one blocking finding.
"""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(owner, repo, pr):
    return "%s-%s-%s" % (owner, repo, pr)


def _review_dir(owner, repo, pr, cfg):
    return notes_config.state_path("review", _slug(owner, repo, pr), cfg=cfg)


def _pr_view(gh, main, pr, owner_repo=None):
    args = ["pr", "view", str(pr), "--json", "number,headRefOid,state,url,title,body"]
    if owner_repo:
        args += ["-R", owner_repo]
    try:
        view = gh.json(args, main)
    except wt.GhError as exc:
        raise wt.Die(1, "gh pr view failed: %s" % exc)
    if not isinstance(view, dict) or not wt._SHA.match(view.get("headRefOid") or ""):
        raise wt.Die(1, "gh pr view returned no usable head sha for PR %s" % pr)
    return view


def round_notice(changes):
    """The fix-round line for an account with `changes` CHANGES verdicts, or None."""
    if changes == FIX_ROUND_LIMIT:
        return ("LAST REVIEW: %d CHANGES verdicts exist, so both fix rounds the contract allows "
                "are used. This is the last review the contract allows; if it finds defects, stop "
                "and report to the user." % changes)
    if changes > FIX_ROUND_LIMIT:
        return ("LIMIT REACHED: %d CHANGES verdicts exist, past the contract's %d fix rounds. "
                "Stop and report to the user instead of starting another round."
                % (changes, FIX_ROUND_LIMIT))
    return None


def count_rounds(comments, login):
    """(prior verdicts by `login`, how many of those are CHANGES)."""
    own = [wt.parse_verdict(c.get("body")) for c in comments
           if ((c.get("user") or {}).get("login") or "").lower() == login.lower()]
    own = [v for v in own if v]
    return len(own), sum(1 for v in own if v[0] == wt.CHANGES)


# ------------------------------------------------------------------ prepare
def _git(repo, *args):
    """(rc, stdout) of a git command in `repo`; rc is None when git could not run."""
    try:
        r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return r.returncode, r.stdout.decode("utf-8", errors="replace")


def last_review_point(prev_meta_sha, comments, login, head):
    """(sha, verdict comment url) of the last reviewed head, or (None, None).

    The sha is the previous meta.json head when it differs from `head`, else the sha in the
    newest own verdict comment. The url is that newest own verdict comment's html_url.
    """
    newest = None
    for c in comments:
        if ((c.get("user") or {}).get("login") or "").lower() != login.lower():
            continue
        v = wt.parse_verdict(c.get("body"))
        if v:
            newest = (v[1], c.get("html_url"))
    url = newest[1] if newest else None
    for sha in (prev_meta_sha, newest[0] if newest else None):
        if sha and wt._SHA.match(sha) and sha != head:
            return sha, url
    return None, url


def since_last_review(repo, num, prev, head, url, globs):
    """Markdown sub-section: the test-edit check on `git diff prev..head`, or why it is absent.

    Never raises: any git trouble prints a warning and is said in the section itself.
    """
    title = "### Since the last reviewed head %s\n\n" % prev[:10]
    tail = ("\nThe prior verdict comment is %s . Check whether this fix introduced defects.\n"
            % url) if url else ""

    def unavailable(why):
        print("warning: no since-last-review check: %s" % why, file=sys.stderr)
        return title + ("The check on the range since that head could not be run: %s. Read the "
                        "range yourself with `git -C %s diff %s..%s`.\n%s"
                        % (why, repo, prev[:10], head[:10], tail))

    def have(sha):
        rc, _ = _git(repo, "cat-file", "-e", sha + "^{commit}")
        return rc == 0

    if not (have(prev) and have(head)):
        _git(repo, "fetch", "-q", "origin", "pull/%s/head" % num)
    if not have(prev) or not have(head):
        return unavailable("git does not have both commits locally")
    rc, _ = _git(repo, "merge-base", "--is-ancestor", prev, head)
    if rc == 1:
        return unavailable("the last reviewed head is not an ancestor of this head (history was "
                           "rewritten)")
    if rc != 0:
        return unavailable("git merge-base failed")
    rc, diff = _git(repo, "diff", "%s..%s" % (prev, head))
    if rc != 0:
        return unavailable("git diff failed")
    hits, counts = test_edit_check._scan_blocks(diff, extra_globs=globs)
    out = (test_edit_check.format_output(hits, counts) if hits else
           "The test-edit check found no edited tests, fixtures or guards and no removed "
           "assertions in the changes since that head. It is a heuristic, not proof.")
    return title + out + "\n" + tail


def build_prompt(check_output, contract, title, body, repo_path, sha, diff, diff_path):
    parts = ["# Review request\n"]
    parts.append("## Edits to tests, fixtures and guards (look at these first)\n\n%s\n" % check_output)
    parts.append("## The review contract\n\n%s\n" % contract.rstrip())
    parts.append("## Your instructions\n\n%s" % INSTRUCTIONS)
    parts.append("## The spec it implements (PR title and body)\n\nTitle: %s\n\n%s\n"
                 % (title, (body or "").strip() or "(empty PR body)"))
    parts.append("## Where the code is\n\nLocal repo: %s\nHead sha: %s\n\nRead files at that sha "
                 "with `git -C %s show %s:<path>` for context beyond the diff.\n"
                 % (repo_path, sha, repo_path, sha))
    if diff_path:
        parts.append("## The diff\n\nThe diff is large, so it is in a file: %s\nRead all of it.\n"
                     % diff_path)
    else:
        parts.append("## The diff\n\n```diff\n%s\n```\n" % diff.rstrip("\n"))
    return "\n".join(parts)


def cmd_prepare(args, cfg):
    main, _ = wt.repo_main()
    gh = wt.Gh(cfg)
    view = _pr_view(gh, main, args.pr)
    if view.get("state") != "OPEN":
        raise wt.Die(1, "PR #%s is %s, not OPEN" % (view["number"], view.get("state")))
    owner, repo = wt._owner_repo(view.get("url"))
    orp = "%s/%s" % (owner, repo)
    sha = view["headRefOid"]
    num = view["number"]
    try:
        diff = gh.run(["pr", "diff", str(num), "-R", orp], main)
        login = gh.run(["api", "user", "--jq", ".login"], main).strip()
        comments = gh.json(["api", "--paginate",
                            "repos/%s/issues/%s/comments" % (orp, num)], main)
    except UnicodeDecodeError:
        raise wt.Die(1, "PR #%s: the diff is not UTF-8 (gh output could not be decoded), so "
                        "prepare cannot build a prompt from it" % num)
    except wt.GhError as exc:
        raise wt.Die(1, "gh failed: %s" % exc)
    if not login:
        raise wt.Die(1, "gh api user returned no login")
    comments = comments if isinstance(comments, list) else []
    total, changes = count_rounds(comments, login)

    globs = list(cfg["review"]["test_globs"])
    hits, counts = test_edit_check._scan_blocks(diff, extra_globs=globs)
    check_output = (test_edit_check.format_output(hits, counts) if hits else
                    "The test-edit check found no edited tests, fixtures or guards and no removed "
                    "assertions in this diff. It scans removed lines in test-shaped paths and "
                    "removed asserts and check( calls, and it is a heuristic, not proof.")
    try:
        with open(CONTRACT, encoding="utf-8") as fh:
            contract = fh.read()
    except OSError as exc:
        raise wt.Die(1, "cannot read the review contract %s: %s" % (CONTRACT, exc))

    d = _review_dir(owner, repo, num, cfg)
    prev_meta_sha = None
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
            prev_meta_sha = json.load(fh).get("head_sha")
    except (OSError, ValueError, AttributeError):
        pass
    prev, prior_url = last_review_point(prev_meta_sha, comments, login, sha)
    if prev:
        check_output += "\n\n" + since_last_review(main, num, prev, sha, prior_url, globs)
    os.makedirs(d, exist_ok=True)
    diff_path = None
    if len(diff.encode("utf-8")) > DIFF_INLINE_CAP:
        diff_path = os.path.join(d, "diff.patch")
        with open(diff_path, "w", encoding="utf-8") as fh:
            fh.write(diff)
    prompt = build_prompt(check_output, contract, view.get("title") or "", view.get("body"),
                          main, sha, diff, diff_path)
    prompt_path = os.path.join(d, "prompt.md")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    meta = {"owner_repo": orp, "pr": str(num), "head_sha": sha, "prepared_at": _now()}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")
    print("prompt: %s" % prompt_path)
    print("head: %s" % sha)
    print("review round %d" % (total + 1))
    notice = round_notice(changes)
    if notice:
        print(notice)
    return 0


# ------------------------------------------------------------------ post
def on_verdict_posted(owner_repo, pr, verdict, sha, url_or_none, bindir=None):
    """The live-context call point: runs once after a verdict comment is posted.

    When `live.py` sits beside this file it records a short note through it; with no live.py
    it does nothing. Any failure prints a warning and never fails the post.
    """
    live = os.path.join(bindir or BINDIR, "live.py")
    if not os.path.isfile(live):
        return
    text = "review verdict %s on %s#%s at %s" % (verdict, owner_repo, pr, sha[:10])
    try:
        r = subprocess.run([sys.executable, live, "note", text], capture_output=True,
                           text=True, timeout=15)
        if r.returncode != 0:
            print("warning: live-context note failed (exit %d): %s"
                  % (r.returncode, (r.stderr or r.stdout).strip()), file=sys.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        print("warning: live-context note failed: %s" % exc, file=sys.stderr)


def strip_body_verdict(text, verdict):
    """Body without its final `VERDICT: X` line.

    Raises Die when the last non-empty line is not exactly `VERDICT: APPROVE` or
    `VERDICT: CHANGES` (the reviewer's instructions require it), or when X disagrees with
    `verdict`.
    """
    lines = text.rstrip().split("\n")
    last = lines[-1].rstrip("\r") if lines else ""
    m = _BODY_VERDICT.match(last) if last.strip() else None
    if not m:
        raise wt.Die(1, "the review body must end with exactly `VERDICT: %s` or `VERDICT: %s`; its "
                        "last non-empty line is %r. Do not post a reply that lacks it"
                        % (wt.APPROVE, wt.CHANGES, last.strip()))
    if m.group(1) != verdict:
        raise wt.Die(1, "the reviewer's own final line says %s but you are posting %s; "
                        "post only the verdict the reviewer returned" % (m.group(1), verdict))
    return "\n".join(lines[:-1]).strip()


def cmd_post(args, cfg):
    main, _ = wt.repo_main()
    gh = wt.Gh(cfg)
    view = _pr_view(gh, main, args.pr)
    owner, repo = wt._owner_repo(view.get("url"))
    orp = "%s/%s" % (owner, repo)
    num = view["number"]
    d = _review_dir(owner, repo, num, cfg)
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        prepared = meta["head_sha"]
    except (OSError, ValueError, KeyError):
        raise wt.Die(1, "no prepared review for %s#%s: run `review.py prepare %s` first"
                        % (orp, num, args.pr))
    if view["headRefOid"] != prepared:
        raise wt.Die(4, "the PR head is %s but the reviewer read %s; the head moved after "
                        "prepare. Run prepare again and get a fresh review of the new head" % (view["headRefOid"], prepared))
    try:
        with open(args.body_file, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise wt.Die(1, "cannot read --body-file %s: %s" % (args.body_file, exc))
    body = strip_body_verdict(raw, args.verdict)
    if not body:
        raise wt.Die(1, "the review body is empty; post the reviewer's findings, not a bare verdict")
    comment = "%s\n\n%s\n" % (wt.verdict_line(args.verdict, prepared), body)
    if args.dry_run:
        print("dry run, would post to %s#%s:" % (orp, num))
        print(comment, end="")
        return 0
    changes_after = None  # own CHANGES verdicts including this one, counted BEFORE posting
    if args.verdict == wt.CHANGES:
        try:
            login = gh.run(["api", "user", "--jq", ".login"], main).strip()
            comments = gh.json(["api", "--paginate",
                                "repos/%s/issues/%s/comments" % (orp, num)], main)
            _, prior = count_rounds(comments if isinstance(comments, list) else [], login)
            changes_after = prior + 1
        except wt.GhError as exc:
            print("warning: could not count CHANGES verdicts for the fix-round limit: %s" % exc,
                  file=sys.stderr)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="comment-", suffix=".md", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(comment)
        out = gh.run(["pr", "comment", str(num), "-R", orp, "--body-file", tmp], main)
    except wt.GhError as exc:
        raise wt.Die(1, "gh pr comment failed: %s" % exc)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    url = (out or "").strip().splitlines()[-1] if (out or "").strip() else None
    print("posted %s verdict at %s%s" % (args.verdict, prepared, (" " + url) if url else ""))
    on_verdict_posted(orp, num, args.verdict, prepared, url)
    if changes_after is not None and changes_after > FIX_ROUND_LIMIT:
        print(round_notice(changes_after))
    return 0


# ------------------------------------------------------------------ cli
def build_parser():
    ap = argparse.ArgumentParser(prog="review.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("prepare", help="write the reviewer prompt for a PR")
    p.add_argument("pr")
    q = sub.add_parser("post", help="post the reviewer's verdict on a PR")
    q.add_argument("pr")
    q.add_argument("verdict", choices=(wt.APPROVE, wt.CHANGES))
    q.add_argument("--body-file", required=True)
    q.add_argument("--dry-run", action="store_true")
    return ap


COMMANDS = {"prepare": cmd_prepare, "post": cmd_post}


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
        return COMMANDS[args.cmd](args, wt.load_cfg())
    except wt.Die as exc:
        print("review: %s" % exc.msg, file=sys.stderr)
        return exc.code


# ------------------------------------------------------------------ selftest
STUB = r'''#!/usr/bin/env python3
import json, os, shutil, sys
d = os.environ["RV_STUB_DIR"]
a = sys.argv[1:]
with open(os.path.join(d, "gh.log"), "a") as fh:
    fh.write(json.dumps(a) + "\n")
def canned(n):
    p = os.path.join(d, n)
    if os.path.exists(p):
        sys.stdout.flush(); sys.stdout.buffer.write(open(p, "rb").read()); return 0
    sys.stderr.write("stub: no canned file %s\n" % n); return 1
if a[:2] == ["pr", "view"]:
    sys.exit(canned("pr_view.json"))
if a[:2] == ["pr", "diff"]:
    sys.exit(canned("diff.patch"))
if a[:2] == ["api", "user"]:
    sys.exit(canned("login"))
if a[0] == "api" and a[-1].endswith("/comments"):
    sys.exit(canned("comments.json"))
if a[:2] == ["pr", "comment"]:
    shutil.copy(a[a.index("--body-file") + 1], os.path.join(d, "posted.md"))
    print("https://github.com/example/proj/pull/7#issuecomment-1"); sys.exit(0)
sys.stderr.write("stub: unhandled %s\n" % a); sys.exit(1)
'''


def selftest():
    import contextlib
    import io
    import shutil
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
        if not cond:
            fails.append(name)

    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as t0:
        T = os.path.realpath(t0)
        stub = os.path.join(T, "stub")
        os.makedirs(stub)
        gh_path = os.path.join(T, "gh-stub")
        with open(gh_path, "w") as fh:
            fh.write(STUB)
        os.chmod(gh_path, 0o755)
        cfgfile = os.path.join(T, "config.toml")
        os.environ.update(HOME=os.path.join(T, "home"), SESSION_NOTES_CONFIG=cfgfile,
                          WT_GH=gh_path, RV_STUB_DIR=stub, GIT_CONFIG_GLOBAL=os.devnull,
                          GIT_CONFIG_NOSYSTEM="1")
        for k in [k for k in os.environ if k.startswith("GIT_") and k not in
                  ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")]:
            del os.environ[k]
        os.makedirs(os.environ["HOME"])
        with open(cfgfile, "w") as fh:
            fh.write('[paths]\nstate_dir = "%s"\n[wt]\ntoken_command = "echo test-token"\n'
                     '[review]\ntest_globs = ["data/*.json"]\n' % os.path.join(T, "state"))
        repo = os.path.join(T, "proj")
        os.makedirs(repo)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        os.chdir(repo)
        global BINDIR
        saved_bindir = BINDIR
        BINDIR = os.path.join(T, "emptybin")  # the real post path must never reach a real live.py
        os.makedirs(BINDIR)

        sha1, sha2 = "a" * 40, "b" * 40
        diff_small = ("diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n"
                      "+++ b/tests/test_x.py\n@@ -10,1 +10,1 @@\n-    x = 1\n+    x = 2\n")

        def put(name, obj):
            with open(os.path.join(stub, name), "w") as fh:
                fh.write(obj if isinstance(obj, str) else json.dumps(obj))

        def view(sha, state="OPEN"):
            put("pr_view.json", {"number": 7, "headRefOid": sha, "state": state,
                                 "url": "https://github.com/example/proj/pull/7",
                                 "title": "Add widget", "body": "Spec: the widget must frob."})

        def verdict_comment(login, v, sha, cid):
            return {"id": cid, "created_at": "2026-01-0%dT00:00:00Z" % cid,
                    "updated_at": "2026-01-0%dT00:00:00Z" % cid, "user": {"login": login},
                    "body": wt.verdict_line(v, sha) + "\n\nfindings"}

        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
            return rc, out.getvalue(), err.getvalue()

        def gh_calls():
            p = os.path.join(stub, "gh.log")
            return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

        put("login", "reviewer-bot\n")
        put("diff.patch", diff_small)
        put("comments.json", [])
        view(sha1)
        sdir = os.path.join(T, "state", "review", "example-proj-7")

        # ---- prepare
        rc, out, err = run("prepare", "7")
        check("prepare ok", rc == 0, err)
        prompt = open(os.path.join(sdir, "prompt.md")).read() if os.path.exists(
            os.path.join(sdir, "prompt.md")) else ""
        check("prepare prints path, sha, round 1",
              os.path.join(sdir, "prompt.md") in out and sha1 in out and "review round 1" in out, out)
        i_check = prompt.find("tests/test_x.py: ")
        i_contract = prompt.find("# Review contract")
        check("test-edit output comes before the contract", 0 <= i_check < i_contract,
              (i_check, i_contract))
        check("prompt carries spec, sha, diff and the verdict instruction",
              "Spec: the widget must frob." in prompt and sha1 in prompt and "+    x = 2" in prompt
              and "VERDICT: APPROVE\nVERDICT: CHANGES" in prompt)
        meta = json.load(open(os.path.join(sdir, "meta.json")))
        check("meta.json", meta["head_sha"] == sha1 and meta["owner_repo"] == "example/proj"
              and meta["pr"] == "7" and meta["prepared_at"].endswith("Z"), meta)
        # an extra glob from config marks data/x.json as a test-shaped file
        put("diff.patch", "diff --git a/data/x.json b/data/x.json\n--- a/data/x.json\n"
                          "+++ b/data/x.json\n@@ -1,1 +1,1 @@\n-1\n+2\n")
        run("prepare", "7")
        check("[review].test_globs applied in prepare",
              "data/x.json: " in open(os.path.join(sdir, "prompt.md")).read())
        put("diff.patch", "diff --git a/bin/a.py b/bin/a.py\n--- a/bin/a.py\n+++ b/bin/a.py\n"
                          "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n")
        run("prepare", "7")
        check("a clean diff says so",
              "is a heuristic, not proof" in open(os.path.join(sdir, "prompt.md")).read())
        # diff spill
        big = "diff --git a/bin/a.py b/bin/a.py\n--- a/bin/a.py\n+++ b/bin/a.py\n@@ -1,1 +1,1 @@\n" \
              + "+" + "z" * (DIFF_INLINE_CAP + 10) + "\n"
        put("diff.patch", big)
        rc, out, err = run("prepare", "7")
        pr_text = open(os.path.join(sdir, "prompt.md")).read()
        check("an over-cap diff is written beside the prompt and referenced",
              os.path.exists(os.path.join(sdir, "diff.patch")) and os.path.join(sdir, "diff.patch")
              in pr_text and "zzzzzzzzzz" not in pr_text, err)
        put("diff.patch", diff_small)
        # rounds
        put("comments.json", [
            verdict_comment("reviewer-bot", wt.CHANGES, sha1, 1),
            verdict_comment("stranger", wt.CHANGES, sha1, 2),
            verdict_comment("stranger", wt.CHANGES, sha1, 3),
            verdict_comment("stranger", wt.CHANGES, sha1, 4),
            {"id": 5, "created_at": "2026-01-05T00:00:00Z", "updated_at": "2026-01-05T00:00:00Z",
             "user": {"login": "reviewer-bot"}, "body": "not a verdict"}])
        rc, out, err = run("prepare", "7")
        check("only own-login verdicts are counted", "review round 2" in out
              and "LIMIT" not in out, out)
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, sha1, 1),
                              verdict_comment("reviewer-bot", wt.CHANGES, sha1, 2)])
        rc, out, err = run("prepare", "7")
        check("two CHANGES: round 3 is announced as the last review allowed",
              "review round 3" in out and "LAST REVIEW" in out and "LIMIT REACHED" not in out, out)
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, sha1, 1)])
        rc, out, err = run("prepare", "7")
        check("one CHANGES: no fix-round notice", "LAST REVIEW" not in out and "LIMIT" not in out, out)
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, sha1, 1),
                              verdict_comment("reviewer-bot", wt.CHANGES, sha1, 2),
                              verdict_comment("reviewer-bot", wt.CHANGES, sha1, 3)])
        rc, out, err = run("prepare", "7")
        check("three CHANGES: loud limit warning, still exit 0", rc == 0 and "LIMIT REACHED" in out
              and "review round 4" in out, out)
        put("comments.json", [])
        view(sha1, state="CLOSED")
        rc, out, err = run("prepare", "7")
        check("prepare refuses a PR that is not open", rc == 1 and "not OPEN" in err, err)
        view(sha1)

        # prompt order: instructions right after the contract, before the spec and the diff
        pt = open(os.path.join(sdir, "prompt.md")).read()
        check("instructions come after the contract and before the spec and the diff",
              pt.find("# Review contract") < pt.find("## Your instructions")
              < pt.find("## The spec it implements") < pt.find("## The diff"))
        # a non-numeric PR argument: every later call uses the number gh reported
        put("comments.json", [])
        os.remove(os.path.join(stub, "gh.log"))
        shutil.rmtree(sdir)
        rc, out, err = run("prepare", "feature-branch")
        bf = os.path.join(T, "fb-body.md")
        with open(bf, "w") as fh:
            fh.write("Finding.\nVERDICT: CHANGES\n")
        rc2, out2, err2 = run("post", "feature-branch", wt.CHANGES, "--body-file", bf)
        if os.path.exists(os.path.join(stub, "posted.md")):
            os.remove(os.path.join(stub, "posted.md"))
        calls = gh_calls()
        after = []
        for c in calls:
            if c[:2] == ["pr", "view"]:
                after = []   # only what follows each pr view is checked
            else:
                after.append(c)
        check("calls after pr view use view[number], not the raw argument",
              rc == 0 and rc2 == 0 and len(after) >= 3
              and not any("feature-branch" in a for c in calls if c[:2] != ["pr", "view"] for a in c)
              and os.path.exists(os.path.join(sdir, "meta.json")), (rc, err, rc2, err2, calls))
        os.remove(os.path.join(stub, "gh.log"))

        # ---- since the last reviewed head (fix rounds): real commits in the temp repo
        def git(*a):
            subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com"]
                           + list(a), check=True, capture_output=True)

        def head_sha():
            return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()

        os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
        with open(os.path.join(repo, "tests", "test_y.py"), "w") as fh:
            fh.write("def t():\n    assert out == [1, 2]\n")
        git("add", "-A")
        git("commit", "-q", "-m", "c1")
        c1 = head_sha()
        with open(os.path.join(repo, "tests", "test_y.py"), "w") as fh:
            fh.write("def t():\n    assert out\n")
        git("commit", "-q", "-a", "-m", "c2")
        c2 = head_sha()
        put("diff.patch", diff_small)
        prior_url = "https://github.com/example/proj/pull/7#issuecomment-99"
        view(c2)

        def prompt_text():
            return open(os.path.join(sdir, "prompt.md")).read()

        # no prior meta, a prior own verdict at c1: sha and url come from the comment
        shutil.rmtree(sdir, ignore_errors=True)
        cm1 = verdict_comment("reviewer-bot", wt.CHANGES, c1, 1)
        cm1["html_url"] = prior_url
        put("comments.json", [cm1])
        rc, out, err = run("prepare", "7")
        pt = prompt_text()
        i_sub = pt.find("### Since the last reviewed head %s" % c1[:10])
        check("fix round, no prior meta: since-check from the verdict comment's sha",
              rc == 0 and 0 <= i_sub < pt.find("## The review contract")
              and "tests/test_y.py:2 [assertion]" in pt[i_sub:pt.find("## The review contract")]
              and prior_url in pt[i_sub:pt.find("## The review contract")], (rc, err, pt[:600]))
        # a previous meta.json head wins over the comment, and the sha differing is required
        shutil.rmtree(sdir)
        os.makedirs(sdir)
        with open(os.path.join(sdir, "meta.json"), "w") as fh:
            json.dump({"head_sha": c1}, fh)
        put("comments.json", [])
        rc, out, err = run("prepare", "7")
        pt = prompt_text()
        check("fix round, prior meta head: since-check runs, no url line without a verdict comment",
              "### Since the last reviewed head %s" % c1[:10] in pt
              and "tests/test_y.py:2 [assertion]" in pt and "prior verdict comment" not in pt, pt[:500])
        shutil.rmtree(sdir)
        os.makedirs(sdir)
        with open(os.path.join(sdir, "meta.json"), "w") as fh:
            json.dump({"head_sha": c1}, fh)
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, "d" * 40, 1)])
        rc, out, err = run("prepare", "7")
        check("the previous meta head wins over the verdict comment's sha",
              "### Since the last reviewed head %s" % c1[:10] in prompt_text())
        put("comments.json", [])
        rc, out, err = run("prepare", "7")  # meta now == head: nothing since, no sub-heading
        check("same head as the last prepare: no since-last-review section",
              "### Since the last reviewed head" not in prompt_text())
        shutil.rmtree(sdir)
        put("comments.json", [])
        rc, out, err = run("prepare", "7")
        check("round 1 (no meta, no verdict): no since-last-review section",
              "### Since the last reviewed head" not in prompt_text())
        # git cannot produce the range: a verdict at a sha this repo does not have
        shutil.rmtree(sdir)
        cm2 = verdict_comment("reviewer-bot", wt.CHANGES, "d" * 40, 1)
        cm2["html_url"] = prior_url
        put("comments.json", [cm2])
        rc, out, err = run("prepare", "7")
        pt = prompt_text()
        check("git cannot produce the range: warns, says so in the sub-heading, still exits 0",
              rc == 0 and "warning: no since-last-review check" in err
              and "### Since the last reviewed head %s" % ("d" * 10) in pt
              and "could not be run" in pt and prior_url in pt, (rc, err, pt[:600]))
        # the last reviewed head is not an ancestor (history rewritten)
        git("checkout", "-q", "-b", "other", c1)
        with open(os.path.join(repo, "tests", "z.py"), "w") as fh:
            fh.write("x = 1\n")
        git("add", "-A")
        git("commit", "-q", "-m", "c3")
        c3 = head_sha()
        git("checkout", "-q", "-")
        shutil.rmtree(sdir)
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, c3, 1)])
        rc, out, err = run("prepare", "7")
        check("a non-ancestor last head is said so, never a bogus range",
              rc == 0 and "not an ancestor" in prompt_text(), err)
        shutil.rmtree(sdir)
        put("comments.json", [])
        view(sha1)

        # a diff that is not UTF-8 dies with a clear message, not a traceback
        with open(os.path.join(stub, "diff.patch"), "wb") as fh:
            fh.write(b"diff --git a/x b/x\n+caf\xe9\n")
        rc, out, err = run("prepare", "7")
        check("a non-UTF-8 diff: clear Die naming the PR, exit 1",
              rc == 1 and "PR #7" in err and "diff is not UTF-8" in err and "Traceback" not in err,
              (rc, err))
        put("diff.patch", diff_small)
        run("prepare", "7")

        # strip_body_verdict: the reviewer's own last line must be exact
        for bad in ("**VERDICT: CHANGES**", "`VERDICT: CHANGES`", "VERDICT: CHANGES.",
                    "Verdict: CHANGES", " VERDICT: CHANGES", "VERDICT: CHANGES and more",
                    "no verdict line at all"):
            try:
                strip_body_verdict("Finding.\n\n" + bad + "\n", wt.APPROVE)
                got = "accepted"
            except wt.Die as exc:
                got = exc.msg
            check("a final line %r is refused, naming it" % bad,
                  got != "accepted" and repr(bad.strip()) in got, got)
        check("an exact final line is stripped",
              strip_body_verdict("Finding.\n\nVERDICT: APPROVE\n\n", wt.APPROVE) == "Finding.")

        # ---- post
        put("comments.json", [])
        run("prepare", "7")  # fresh meta at sha1
        body = os.path.join(T, "body.md")

        def write_body(text):
            with open(body, "w") as fh:
                fh.write(text)

        posted = os.path.join(stub, "posted.md")
        write_body("Finding 1: bin/a.py:3 `x = 1` breaks on empty input.\n\nVERDICT: CHANGES\n")
        rc, out, err = run("post", "7", wt.CHANGES, "--body-file", body, "--dry-run")
        check("dry run prints the comment and posts nothing", rc == 0 and wt.verdict_line(
            wt.CHANGES, sha1) in out and not os.path.exists(posted)
            and not any(c[:2] == ["pr", "comment"] for c in gh_calls()), (out, err))
        rc, out, err = run("post", "7", wt.CHANGES, "--body-file", body)
        text = open(posted).read() if os.path.exists(posted) else ""
        check("post ok and the VERDICT line is stripped", rc == 0 and "VERDICT:" not in text
              and "Finding 1" in text, (rc, err, text))
        first = text.split("\n", 1)[0]
        check("first line parses back to (verdict, sha) via wt.parse_verdict",
              wt.parse_verdict(text) == (wt.CHANGES, sha1) and first == wt.verdict_line(wt.CHANGES, sha1))
        cm = {"id": 9, "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-02-01T00:00:00Z",
              "user": {"login": "reviewer-bot"}}
        approve_text = None
        write_body("All good.\nVERDICT: APPROVE")
        rc, out, err = run("post", "7", wt.APPROVE, "--body-file", body)
        approve_text = open(posted).read()
        cm["body"] = approve_text
        check("gate accepts that APPROVE at its head", wt.check_gate([cm], "reviewer-bot", sha1)[0])
        check("gate refuses it at another head", not wt.check_gate([cm], "reviewer-bot", sha2)[0])
        # refusals
        os.remove(posted)
        write_body("All good.\nVERDICT: APPROVE\n")
        rc, out, err = run("post", "7", wt.CHANGES, "--body-file", body)
        check("a body VERDICT that disagrees is refused", rc == 1 and "says APPROVE" in err
              and not os.path.exists(posted), err)
        write_body("\n\nVERDICT: APPROVE\n")
        rc, out, err = run("post", "7", wt.APPROVE, "--body-file", body)
        check("an empty body is refused", rc == 1 and "empty" in err and not os.path.exists(posted), err)
        write_body("   \n")
        rc, out, err = run("post", "7", wt.APPROVE, "--body-file", body)
        check("a whitespace-only body is refused", rc == 1 and not os.path.exists(posted), err)
        write_body("Looks fine.\nVERDICT: APPROVE\n")
        view(sha2)
        rc, out, err = run("post", "7", wt.APPROVE, "--body-file", body)
        check("a stale head sha is refused", rc == 4 and "moved" in err and not os.path.exists(posted), err)
        view(sha1)
        shutil.rmtree(sdir)
        rc, out, err = run("post", "7", wt.APPROVE, "--body-file", body)
        check("missing meta is refused", rc == 1 and "run `review.py prepare" in err
              and not os.path.exists(posted), err)

        # ---- on_verdict_posted, with and without live.py (a temp layout, never the real bin/)
        lay = os.path.join(T, "layout")
        os.makedirs(lay)
        on_verdict_posted("example/proj", "7", wt.APPROVE, sha1, None, bindir=lay)
        check("no live.py: does nothing", os.listdir(lay) == [])
        with open(os.path.join(lay, "live.py"), "w") as fh:
            fh.write("import sys\nopen(sys.argv[0] + '.out', 'w').write(' '.join(sys.argv[1:]))\n")
        on_verdict_posted("example/proj", "7", wt.APPROVE, sha1, None, bindir=lay)
        got = open(os.path.join(lay, "live.py.out")).read() if os.path.exists(
            os.path.join(lay, "live.py.out")) else ""
        check("live.py present: called with note + short text",
              got == "note review verdict APPROVE on example/proj#7 at %s" % sha1[:10], got)
        with open(os.path.join(lay, "live.py"), "w") as fh:
            fh.write("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            on_verdict_posted("example/proj", "7", wt.CHANGES, sha1, None, bindir=lay)
        check("a failing live.py only warns", "warning" in err.getvalue() and "boom" in err.getvalue())
        # the whole post still succeeds when on_verdict_posted's writer fails
        # a real post with a failing live.py in the directory the post path uses
        view(sha1)
        run("prepare", "7")
        BINDIR = lay
        write_body("Finding.\nVERDICT: CHANGES\n")
        put("comments.json", [])
        rc, out, err = run("post", "7", wt.CHANGES, "--body-file", body)
        check("post exits 0 and warns when live.py fails",
              rc == 0 and "warning: live-context note failed" in err and "boom" in err, (rc, err))
        check("first CHANGES: no stop line", "LIMIT" not in out and "LAST REVIEW" not in out, out)
        # the count is taken BEFORE the post (prior own CHANGES + 1) and never re-read after it
        BINDIR = os.path.join(T, "emptybin")
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, sha1, i) for i in (1, 2)])
        os.remove(os.path.join(stub, "gh.log"))
        rc, out, err = run("post", "7", wt.CHANGES, "--body-file", body)
        calls = gh_calls()
        check("post CHANGES making the third prints the stop-and-report line",
              rc == 0 and "LIMIT REACHED: 3 CHANGES" in out, out)
        check("the count is read before the post, not after it",
              [c[0] for c in calls].count("api") == 2
              and calls[-1][:2] == ["pr", "comment"], calls)
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, sha1, 1)])
        rc, out, err = run("post", "7", wt.CHANGES, "--body-file", body)
        check("post CHANGES making the second prints no stop line, and not the last-review text",
              rc == 0 and "LIMIT" not in out and "LAST REVIEW" not in out, out)
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, sha1, 1)]
            + [verdict_comment("stranger", wt.CHANGES, sha1, i) for i in (2, 3, 4)])
        rc, out, err = run("post", "7", wt.CHANGES, "--body-file", body)
        check("other logins' CHANGES verdicts are not counted by post",
              rc == 0 and "LIMIT" not in out and "LAST REVIEW" not in out, out)
        write_body("Fine.\nVERDICT: APPROVE\n")
        put("comments.json", [verdict_comment("reviewer-bot", wt.CHANGES, sha1, i) for i in (1, 2, 3)])
        rc, out, err = run("post", "7", wt.APPROVE, "--body-file", body)
        check("post APPROVE never prints the stop line", rc == 0 and "LIMIT" not in out, out)

    BINDIR = saved_bindir
    os.chdir(saved_cwd)
    os.environ.clear()
    os.environ.update(saved_env)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
