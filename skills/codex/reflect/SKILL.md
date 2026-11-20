---
name: reflect
description: "End-of-session reflection for Codex: checks for uncommitted work, reviews the session, and writes durable learnings to the session-notes Codex memory store (~/.codex/session-notes) and its RESUME note, never to ~/.claude. Use when the user says $reflect, 'reflect', 'save what we learned', or '$reflect consolidate' for a deep pass. Offer it once when a session is wrapping up ('thanks', 'done for now', 'that's all')."
---

# $reflect (Codex)

## First: find the checkout (do not trust the working folder)

Run this once and use the path it prints, as a literal absolute path, everywhere this skill or the
adapter says `$SESSION_NOTES`. Put `SESSION_NOTES_AGENT=codex` in front of every helper call
except `codex_save.py commit`, which must run with exactly its own text.

```bash
python3 -c "import os; p=os.path.realpath(os.path.expanduser(os.environ.get('CODEX_HOME','~/.codex')+'/skills/reflect')); print(p.rsplit('/skills/codex/',1)[0])"
```

If it fails, use `$SESSION_NOTES` when that holds a directory with `skills/codex/ADAPTER.md`.
Never fall back to a `bin/` or `skills/` in the current folder: a session-notes checkout there may
be an older version without the Codex code. If neither works, stop and say so.

1. Read `$SESSION_NOTES/skills/codex/ADAPTER.md`: the Codex substitutions.
2. Read `$SESSION_NOTES/skills/reflect/SKILL.md` in full, and follow it step by step with every
   substitution from the adapter applied. An argument of `consolidate` selects its consolidate
   mode.

Codex-specific points that matter most here:

- Stores come from `python3 $SESSION_NOTES/bin/notes_config.py --stores` (Codex mode). Create
  the store directory on its first write; an empty or missing store is the normal start.
- Steps 5 to 5b write through `codex_save.py` only (adapter, "Writing memory"): stage every
  memory file, index line and RESUME change, then run `python3 $SESSION_NOTES/bin/codex_save.py
  commit` once, as a command on its own with exactly that text. Commit takes and releases the
  reflect lock itself, so skip the skill's own acquire (Step 5) and release (Step 8).
- Nothing under `~/.claude` is read or written. If a step seems to need it, skip that step and
  say so in one line.
