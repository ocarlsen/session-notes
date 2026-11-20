---
name: refresh
description: "Reload stale context in a long-running Codex session: re-read the session-notes Codex memory store (MEMORY.md, RESUME.md, global and project) and the AGENTS.md chain from disk, and report what changed since the session started. Use when the user says $refresh, 'reload context', 're-sync', 'are you up to date'. With a subject ('$refresh billing') also searches the store for it. Read-only."
---

# $refresh (Codex)

## First: find the checkout (do not trust the working folder)

Run this once and use the path it prints, as a literal absolute path, everywhere this skill or the
adapter says `$SESSION_NOTES`. Put `SESSION_NOTES_AGENT=codex` in front of every helper call.

```bash
python3 -c "import os; p=os.path.realpath(os.path.expanduser(os.environ.get('CODEX_HOME','~/.codex')+'/skills/refresh')); print(p.rsplit('/skills/codex/',1)[0])"
```

If it fails, use `$SESSION_NOTES` when that holds a directory with `skills/codex/ADAPTER.md`.
Never fall back to a `bin/` or `skills/` in the current folder: a session-notes checkout there may
be an older version without the Codex code. If neither works, stop and say so.

1. Read `$SESSION_NOTES/skills/codex/ADAPTER.md`: the Codex substitutions.
2. Read `$SESSION_NOTES/skills/refresh/SKILL.md` in full, and follow it with every substitution
   from the adapter applied. In particular:
   - Step 1: the stores are `python3 $SESSION_NOTES/bin/notes_config.py --stores`.
   - Step 2: read `<global>/MEMORY.md`, `<global>/RESUME.md`, `<project>/MEMORY.md`,
     `<project>/RESUME.md` (each that exists), then `~/.codex/AGENTS.md` and each AGENTS.md
     from the git root down to cwd. Compare against what the session-start memory block (the
     SessionStart hook's context) or an earlier `$refresh` showed.
   - Step 2b (sibling peek and message): skip.
3. Report the delta and stop, exactly as its Step 4 says. A `$refresh` never writes.
