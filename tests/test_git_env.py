#!/usr/bin/env python3
"""Run the self-tests the way a git hook does, and prove they leave the repo alone.

git runs pre-commit with GIT_DIR (and friends) exported, pointing at the repo being
committed to. A self-test that runs `git -C <tmpdir> init` / `commit` for a throwaway
repo then operates on the REAL repo instead, because GIT_DIR overrides -C: it re-inits
it (setting core.bare) and commits its fixture onto the branch being committed. That
happened once from a worktree. This test points GIT_DIR at a sentinel repo, runs the
whole self-test suite, and fails if the sentinel's HEAD, branch or config changed.

    python3 tests/test_git_env.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")}).stdout.strip()


# Identities the self-tests give their throwaway repos. Seen in this repo's OWN config means a
# self-test once ran against it (before the GIT_DIR fix one did, and every later commit was
# authored "Selftest"): the damage outlives the bug, so check for it directly.
TEST_IDENTITIES = {"test@example.com"}


def main():
    own = git(ROOT, "config", "--local", "--get", "user.email")
    if own in TEST_IDENTITIES:
        print("FAIL this repo's own git config has a self-test identity (user.email = %s);" % own)
        print("  commits would be authored by it. Fix: git config --local user.email <you>, and user.name.")
        return 1
    with tempfile.TemporaryDirectory() as d:
        sentinel = os.path.join(d, "sentinel")
        os.makedirs(sentinel)
        git(sentinel, "init", "-q")
        git(sentinel, "config", "user.email", "test@example.com")
        git(sentinel, "config", "user.name", "Sentinel")
        with open(os.path.join(sentinel, "keep.txt"), "w") as fh:
            fh.write("sentinel\n")
        git(sentinel, "add", "keep.txt")
        git(sentinel, "commit", "-q", "-m", "sentinel")
        before = (git(sentinel, "rev-parse", "HEAD"), git(sentinel, "config", "--get", "core.bare"),
                  git(sentinel, "ls-files"))
        env = dict(os.environ)
        env["GIT_DIR"] = os.path.join(sentinel, ".git")
        env["GIT_INDEX_FILE"] = os.path.join(sentinel, ".git", "index")
        run = subprocess.run(["sh", os.path.join(ROOT, "tests", "run_selftests.sh")], env=env,
                             capture_output=True, text=True)
        after = (git(sentinel, "rev-parse", "HEAD"), git(sentinel, "config", "--get", "core.bare"),
                 git(sentinel, "ls-files"))
    if before != after:
        print("FAIL self-tests run from a git hook changed the repo they were run in:")
        print("  before (HEAD, core.bare, files): %r" % (before,))
        print("  after:                           %r" % (after,))
        return 1
    if run.returncode != 0:
        print("FAIL the self-test suite itself failed with GIT_DIR set:")
        print("\n".join(run.stdout.splitlines()[-15:]))
        return 1
    print("PASS self-tests leave the hook's repo untouched (GIT_DIR set)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
