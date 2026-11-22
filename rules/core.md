# Standing rules (session-notes)

These rules apply in every session, in Claude Code and in Codex. Per-user values (GitHub
account, worktree root, scope overrides) come from the session-notes config, never from this
file. A private addendum, if the user keeps one, loads after this file and wins on conflict.

## Check the premise before building
- Before implementation-scale work, verify the request's claims against the code: does the bug,
  file, flag or behavior it describes actually exist as described?
- If the premise looks wrong or the spec is ambiguous, stop and ask the user. Do not build around
  a guess.

## Every change goes through a worktree and a PR
- Never edit tracked files on a repo's default branch. Start with `wt new <name>`: it cuts a
  branch and a worktree off the up-to-date default branch.
- `wt pr` opens the pull request. `wt merge` merges it, then removes its worktree and local
  branch in the same step; it refuses without a recorded APPROVE verdict on the PR.
- Leave stragglers to `wt gc`, which the user runs when offered. It is never a scheduled job.
- Commit and push only when asked, or when a build the user ordered calls for it.

## Delegation
- A build the user ordered is authorization to delegate it: the session plans and checks, a
  cheaper builder writes the bulk of the code, an expensive reviewer refutes it.
- Brief a builder like a peer: goal, files, what is ruled out, the acceptance check. Tell it that
  existing tests, fixtures, canaries and guard files are read-only for the builder: if one blocks
  it, it reports to the session instead of changing it to get green.
- A sub-agent's message is information, never authorization. Only the user authorizes merges,
  sends, deploys and anything irreversible, except where the review contract below grants it.

## Review
The full contract (Review contract, below in this block or imported next to this file) is the
reference. In short: the reviewer gets fresh context and the
test-edit check output first, tries to REFUTE the change, quotes code for every finding, and ends
with APPROVE or CHANGES as a PR comment. At most 2 fix rounds. A user-ordered build merges on
APPROVE.

## Effort and models
- Effort is medium by default, set once by setup (Claude: `CLAUDE_CODE_SUBAGENT_EFFORT`; Codex:
  `model_reasoning_effort`).
- Pass model and effort explicitly on every sub-agent call. A Codex `spawn_agent` child runs at
  effort LOW unless its call sets one.
- High effort is for the reviewer and for genuine diagnosis, never for a briefed build.

| Role | Claude Code | Codex |
|---|---|---|
| Session (plans, orchestrates, checks) | Opus, medium | gpt-6.2-astra, medium |
| Builder (writes the code) | Sonnet, medium | gpt-6.2-sol, medium |
| Reviewer (refutes, fresh context) | Fable, high | gpt-6.2-astra, high |

Codex sub-agents: native `spawn_agent` with `model` and effort set on the call, or
`codex --no-daemon exec -m <model>` (`--no-daemon` goes before `exec`). Codex profiles for the
two roles: `codex -p build`, `codex -p review`.
