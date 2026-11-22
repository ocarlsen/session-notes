# Review contract (session-notes)

How a change gets reviewed before it merges. The flow is: a cheap builder writes it, the
session runs its own checks, then an expensive reviewer with fresh context tries to break it.

## Before the reviewer starts
1. The session runs the repo's checks (tests, leak scan, linters) and they pass.
2. The session runs the test-edit check (a scan of the diff for edits to tests, fixtures and
   guard files) on the PR diff and puts its output FIRST in the
   reviewer's prompt. A change that edits the tests, fixtures or guards that would catch it is the
   first thing the reviewer looks at.
3. The reviewer gets the diff, the spec it implements and the repo, not the session's opinion of
   the change. Fresh context means no builder transcript and no "this looks done".

## What the reviewer does
- REFUTE, do not grade. The job is to find the input, state or sequence that makes the change
  wrong, not to score it.
- Quote the code for every finding: file, line, and the lines themselves. A finding with no quote
  is not a finding.
- Each finding says what goes wrong and how to trigger it, and whether it blocks.
- End with exactly one verdict: APPROVE (nothing blocking) or CHANGES (at least one blocking
  finding).

## Recording the verdict
The verdict is a comment on the PR, so it outlives the session. Its first line is exactly one of:

    Review verdict: APPROVE <full 40-character head sha>
    Review verdict: CHANGES <full 40-character head sha>

followed by the findings. The sha is the PR head commit the reviewer read.

`wt merge` refuses unless the NEWEST verdict comment posted by the configured GitHub account is
APPROVE and its sha equals the PR's current head sha. So any push after an approval needs a fresh
review (a sha cannot be backdated the way a commit time can), and a verdict from any other account
is ignored (on a public repo anyone can comment). A verdict comment that was edited after it was
posted is refused too, and it keeps blocking the merge until it is deleted: post a new comment
instead of editing one. The marker strings are defined once, in `bin/wt.py`; this file quotes
them.

## Fix rounds
- CHANGES goes back to the builder with the quoted findings. At most 2 fix rounds.
- If a round finds defects in the previous round's fix, stop and report to the user instead of
  starting a third round: the scope is growing, not converging.
- Small non-blocking nits on the session's own change get a follow-up PR in the same session,
  not a note in a ledger.

## Merging
Rebase onto the default branch and run the checks BEFORE asking for review, so the approved sha is
the one that merges. A build the user ordered merges on that APPROVE. If the default branch moves
and a rebase is needed after the verdict, the head sha changes and the change needs a fresh
verdict from the reviewer. The session posts only the verdict the reviewer returned, never one
of its own. Anything whose acceptance needs a live run the user types (for example a Codex
change checked in both the CLI and the desktop app) waits for that run.
