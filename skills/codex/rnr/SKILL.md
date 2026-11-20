---
name: rnr
description: "Refresh 'n' reflect for Codex: re-read the session-notes Codex store first, then decide whether to reflect and, if so, run the reflect procedure under the reflect lock. Use when the user says $rnr, 'refresh and reflect', or is wrapping up a long session. Optional argument: a refresh subject, or 'consolidate' for reflect."
---

# $rnr (Codex)

## First: find the checkout (do not trust the working folder)

Run this once and use the path it prints, as a literal absolute path, everywhere this skill or the
adapter says `$SESSION_NOTES`. Put `SESSION_NOTES_AGENT=codex` in front of every helper call
except `codex_save.py commit`, which must run with exactly its own text.

```bash
python3 -c "import os; p=os.path.realpath(os.path.expanduser(os.environ.get('CODEX_HOME','~/.codex')+'/skills/rnr')); print(p.rsplit('/skills/codex/',1)[0])"
```

If it fails, use `$SESSION_NOTES` when that holds a directory with `skills/codex/ADAPTER.md`.
Never fall back to a `bin/` or `skills/` in the current folder: a session-notes checkout there may
be an older version without the Codex code. If neither works, stop and say so.

Codex skills do not invoke each other, so this runs both procedures inline, in order:

1. Read `$SESSION_NOTES/skills/codex/ADAPTER.md`: the Codex substitutions.
2. Read `$SESSION_NOTES/skills/rnr/SKILL.md` for the chain, the verdict and the gate. Follow it
   with these changes:
   - Its Step 1 ("invoke the refresh skill"): follow `$SESSION_NOTES/skills/codex/refresh/SKILL.md`
     inline, passing the subject through.
   - Its Step 1b verdict: there is no sibling peek in Codex, so safety comes from the lock alone.
     `REFLECT: CAUTION` applies only when `python3 $SESSION_NOTES/bin/reflect_lock.py status`
     shows another holder.
   - Its Step 2 ("invoke /reflect"): follow `$SESSION_NOTES/skills/codex/reflect/SKILL.md`
     inline. On `CAUTION`, wait in the adapter's foreground 60-second slices, never
     backgrounded and never asking "wait or go?".
3. Never reverse the order: refresh first, reflect second.
