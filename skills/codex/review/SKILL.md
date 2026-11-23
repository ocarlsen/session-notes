---
name: review
description: "Run the contract review on a pull request in Codex: a fresh-context reviewer tries to refute the change, and its verdict is posted on the PR so `wt merge` can read it. Use when the user says $review, 'review PR N', or 'run the review on this PR'."
---

# $review (Codex)

## First: find the checkout (do not trust the working folder)

Run this once and use the path it prints, as a literal absolute path, everywhere this skill or the
adapter says `$SESSION_NOTES`. Put `SESSION_NOTES_AGENT=codex` in front of every helper call
except `codex_save.py commit`, which must run with exactly its own text.

```bash
python3 -c "import os; p=os.path.realpath(os.path.expanduser(os.environ.get('CODEX_HOME','~/.codex')+'/skills/review')); print(p.rsplit('/skills/codex/',1)[0])"
```

If it fails, use `$SESSION_NOTES` when that holds a directory with `skills/codex/ADAPTER.md`.
Never fall back to a `bin/` or `skills/` in the current folder: a session-notes checkout there may
be an older version without the Codex code. If neither works, stop and say so.

1. Read `$SESSION_NOTES/skills/codex/ADAPTER.md`: the Codex substitutions.
2. Read `$SESSION_NOTES/skills/review/SKILL.md` in full, and follow it with every substitution
   from the adapter applied. In particular:
   - Step 3 (the reviewer): spawn a native `spawn_agent` child with the model set explicitly to
     `gpt-6.2-astra` (the reviewer row of `rules/core.md`) and the reasoning effort set explicitly
     to HIGH on the call: a child defaults to effort LOW, and a review at LOW is not a review.
     The child's prompt is one instruction: read the whole prompt file, giving its path (real
     prompts are too large to paste), and follow it; add nothing else. The call, as confirmed
     by a live run in the Codex CLI: `model: "gpt-6.2-astra"`, `reasoning_effort: "high"`,
     `fork_turns: "none"` (the child starts with fresh context, never a fork of this one),
     a short `task_name`, and the one instruction as `message`. If the tool's schema differs,
     follow the schema and keep those three settings. Alternative:
     `codex --no-daemon exec -m gpt-6.2-astra ...` with the prompt file on stdin (`--no-daemon`
     goes before `exec`), and its final message is the reply.
   - Every `review.py` call keeps `SESSION_NOTES_AGENT=codex` in front, like the other helpers.
   - If `review.py` fails, stop and report the error. Never patch it, wrap it in a copy, or
     run a modified version from `/tmp` or anywhere else.
   - There is no memory-store write in this skill, so nothing goes through `codex_save.py`.
3. Post only the verdict the reviewer returned, and stop at Step 6's `wt merge` hand-off exactly
   as the shared skill says.
