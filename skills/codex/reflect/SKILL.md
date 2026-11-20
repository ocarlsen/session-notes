---
name: reflect
description: "End-of-session reflection for Codex: checks for uncommitted work, reviews the session, and writes durable learnings to the session-notes Codex memory store (~/.codex/session-notes) and its RESUME note, never to ~/.claude. Use when the user says $reflect, 'reflect', 'save what we learned', or '$reflect consolidate' for a deep pass. Offer it once when a session is wrapping up ('thanks', 'done for now', 'that's all')."
---

# $reflect (Codex)

1. Read `$SESSION_NOTES/skills/codex/ADAPTER.md`: the Codex substitutions.
2. Read `$SESSION_NOTES/skills/reflect/SKILL.md` in full, and follow it step by step with every
   substitution from the adapter applied. An argument of `consolidate` selects its consolidate
   mode.

Codex-specific points that matter most here:

- Stores come from `python3 $SESSION_NOTES/bin/notes_config.py --stores` (Codex mode). Create
  the store directory on its first write; an empty or missing store is the normal start.
- Take the reflect lock at its Step 5 (`python3 $SESSION_NOTES/bin/reflect_lock.py acquire`),
  wait in foreground 60-second slices if it is held, and release it in Step 8, even when you
  abort.
- Nothing under `~/.claude` is read or written. If a step seems to need it, skip that step and
  say so in one line.
