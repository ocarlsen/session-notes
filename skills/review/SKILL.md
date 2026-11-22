---
name: review
description: "Run the contract review on a pull request: a fresh-context reviewer tries to refute the change, and its verdict is posted on the PR so `wt merge` can read it. Use when the user says 'review PR N', '/review <PR>', or 'run the review on this PR'."
---

# /review: refute a PR, record the verdict

Implements `rules/review-contract.md`. The reviewer is a fresh sub-agent that gets the diff, the spec
and the repo, never your opinion of the change. The verdict is a PR comment, so it outlives this
session and `wt merge` can check it.

`$SESSION_NOTES` below is the session-notes checkout (set by `install.sh`). Run every command from
inside a checkout of the PR's repo.

## Step 1: checks pass, and the branch is rebased

Run the repo's checks (`make check`, or the repo's equivalent) and get them green. Rebase the
branch onto the default branch first and push, so the sha that gets approved is the sha that
merges. Do not start a review with failing checks.

## Step 2: prepare

```bash
python3 $SESSION_NOTES/bin/review.py prepare <PR>
```

It prints the prompt path, the head sha and the review round. The prompt has the test-edit check
output first, then the contract, the PR title and body as the spec, the repo path and sha, the
diff, and the reviewer's instructions.

If it prints `LAST REVIEW`, both fix rounds are used and this is the last review the contract
allows. If it prints `LIMIT REACHED`, stop and report to the user instead of starting another
round. `post CHANGES` prints the same stop line when it brings the CHANGES count past the limit.

## Step 3: spawn the reviewer

Use the Agent tool with the reviewer model from the rules model map (`rules/core.md`: Fable) and
`effort` set explicitly to `high` on the call. The prompt is one instruction: read the whole
prompt file, giving its path (real prompts are too large to paste), and follow it. Add nothing
else: no summary of the change, no "this looks done", no builder transcript.

## Step 4: post exactly what the reviewer returned

Write the reviewer's reply, verbatim, to a file. Its last line is `VERDICT: APPROVE` or
`VERDICT: CHANGES`. Then:

```bash
python3 $SESSION_NOTES/bin/review.py post <PR> <APPROVE|CHANGES> --body-file <file>
```

Post only the verdict the reviewer returned, never one of your own. `post` refuses when the PR head
moved since `prepare` (run `prepare` again and review the new head), when `prepare` was never run,
when the body is empty, or when the body's final line disagrees with the verdict. Add `--dry-run`
to see the comment without posting.

## Step 5: CHANGES

Hand the quoted findings to the builder. At most 2 fix rounds. If a round finds defects in the
previous round's fix, stop and report to the user: the scope is growing, not converging. After a
fix: checks, rebase, push, then back to Step 2 (the new head needs a fresh verdict).

## Step 6: APPROVE

`wt merge` reads the verdict: it merges only when the newest verdict by the configured account is
APPROVE at the PR's current head sha.
