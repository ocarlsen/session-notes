#!/usr/bin/env python3
"""review.py -- prepare a reviewer prompt for a PR, and post the reviewer's verdict on it.

Run from inside a checkout of the PR's repo (a worktree is fine). Implements
rules/review-contract.md; the verdict marker and its parser live in bin/wt.py and are imported
here, never restated.

    review.py prepare <PR>
        Fetch the PR's head sha, title, body and diff, run the test-edit check on the diff, and
        write a reviewer prompt plus meta.json under the state dir
        (<state_dir>/review/<owner>-<repo>-<PR>/). Prints the prompt path, the head sha and the
        review round. Warns loudly, and still exits 0, when the contract's fix-round limit is
        already reached.
    review.py post <PR> APPROVE|CHANGES --body-file FILE [--dry-run]
        Post the reviewer's reply as the verdict comment: first line
        `Review verdict: <V> <head sha>`, then the body. Refuses when the PR head moved since
        `prepare` (the reviewer read a different commit), when `prepare` was never run, when the
        body is empty, or when the body's own final `VERDICT: X` line disagrees with <V>.
        --dry-run prints the comment and posts nothing.
    review.py --selftest

The comment is posted with the token from `[wt].token_command` (the account whose verdicts
`wt merge` trusts). The test-edit check's extra globs come from `[review].test_globs`.

Exit codes: 0 ok; 1 usage, fatal, or a refusal (no prepare, empty body, verdict mismatch, PR
not open); 3 token command failed; 4 the PR head moved since prepare.
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
_BODY_VERDICT = re.compile(r"^VERDICT: (%s|%s)\s*$" % (wt.APPROVE, wt.CHANGES))

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
- Verify the change against the spec above, including what it should refuse or leave alone.
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


def count_rounds(comments, login):
    """(prior verdicts by `login`, how many of those are CHANGES)."""
    own = [wt.parse_verdict(c.get("body")) for c in comments
           if ((c.get("user") or {}).get("login") or "").lower() == login.lower()]
    own = [v for v in own if v]
    return len(own), sum(1 for v in own if v[0] == wt.CHANGES)


# ------------------------------------------------------------------ prepare
def build_prompt(check_output, contract, title, body, repo_path, sha, diff, diff_path):
    parts = ["# Review request\n"]
    parts.append("## Edits to tests, fixtures and guards (look at these first)\n\n%s\n" % check_output)
    parts.append("## The review contract\n\n%s\n" % contract.rstrip())
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
    parts.append("## Your instructions\n\n%s" % INSTRUCTIONS)
    return "\n".join(parts)


def cmd_prepare(args, cfg):
    main, _ = wt.repo_main()
    gh = wt.Gh(cfg)
    view = _pr_view(gh, main, args.pr)
    if view.get("state") != "OPEN":
        raise wt.Die(1, "PR #%s is %s, not OPEN" % (args.pr, view.get("state")))
    owner, repo = wt._owner_repo(view.get("url"))
    orp = "%s/%s" % (owner, repo)
    sha = view["headRefOid"]
    try:
        diff = gh.run(["pr", "diff", str(args.pr), "-R", orp], main)
        login = gh.run(["api", "user", "--jq", ".login"], main).strip()
        comments = gh.json(["api", "--paginate",
                            "repos/%s/issues/%s/comments" % (orp, args.pr)], main)
    except wt.GhError as exc:
        raise wt.Die(1, "gh failed: %s" % exc)
    if not login:
        raise wt.Die(1, "gh api user returned no login")
    comments = comments if isinstance(comments, list) else []
    total, changes = count_rounds(comments, login)

    globs = list(cfg["review"]["test_globs"])
    hits, counts = test_edit_check._scan_blocks(diff, extra_globs=globs)
    check_output = (test_edit_check.format_output(hits, counts) if hits else
                    "No edits to existing tests, fixtures or guards, and no removed assertions, "
                    "were found in this diff.")
    try:
        with open(CONTRACT, encoding="utf-8") as fh:
            contract = fh.read()
    except OSError as exc:
        raise wt.Die(1, "cannot read the review contract %s: %s" % (CONTRACT, exc))

    d = _review_dir(owner, repo, args.pr, cfg)
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
    meta = {"owner_repo": orp, "pr": str(args.pr), "head_sha": sha, "prepared_at": _now()}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")
    print("prompt: %s" % prompt_path)
    print("head: %s" % sha)
    print("review round %d" % (total + 1))
    if changes > FIX_ROUND_LIMIT:
        print("LIMIT REACHED: %d CHANGES verdicts already exist, so the contract's %d fix rounds "
              "are used. Stop and report to the user instead of starting another round."
              % (changes, FIX_ROUND_LIMIT))
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
    """Body without its final `VERDICT: X` line. Raises Die when X disagrees with `verdict`."""
    lines = text.rstrip().split("\n")
    if lines and lines[-1].strip():
        m = _BODY_VERDICT.match(lines[-1].rstrip("\r"))
        if m:
            if m.group(1) != verdict:
                raise wt.Die(1, "the reviewer's own final line says %s but you are posting %s; "
                                "post only the verdict the reviewer returned" % (m.group(1), verdict))
            lines = lines[:-1]
    return "\n".join(lines).strip()


def cmd_post(args, cfg):
    main, _ = wt.repo_main()
    gh = wt.Gh(cfg)
    view = _pr_view(gh, main, args.pr)
    owner, repo = wt._owner_repo(view.get("url"))
    orp = "%s/%s" % (owner, repo)
    d = _review_dir(owner, repo, args.pr, cfg)
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        prepared = meta["head_sha"]
    except (OSError, ValueError, KeyError):
        raise wt.Die(1, "no prepared review for %s#%s: run `review.py prepare %s` first"
                        % (orp, args.pr, args.pr))
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
        print("dry run, would post to %s#%s:" % (orp, args.pr))
        print(comment, end="")
        return 0
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="comment-", suffix=".md", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(comment)
        out = gh.run(["pr", "comment", str(args.pr), "-R", orp, "--body-file", tmp], main)
    except wt.GhError as exc:
        raise wt.Die(1, "gh pr comment failed: %s" % exc)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    url = (out or "").strip().splitlines()[-1] if (out or "").strip() else None
    print("posted %s verdict at %s%s" % (args.verdict, prepared, (" " + url) if url else ""))
    on_verdict_posted(orp, args.pr, args.verdict, prepared, url)
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
        sys.stdout.write(open(p).read()); return 0
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
              "No edits to existing tests" in open(os.path.join(sdir, "prompt.md")).read())
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
        check("two CHANGES: round 3, no limit warning yet", "review round 3" in out
              and "LIMIT" not in out, out)
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

        # ---- post
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
        write_body("Looks fine.\n")
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
        check("on_verdict_posted never raised above", True)

    BINDIR = saved_bindir
    os.chdir(saved_cwd)
    os.environ.clear()
    os.environ.update(saved_env)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
