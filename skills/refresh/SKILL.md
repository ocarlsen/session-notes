---
name: refresh
description: Reload context that has gone stale in a long-running live session. Claude Code loads the CLAUDE.md hierarchy and @imports ONCE at session start; they do NOT re-read mid-session, so RESUME.md, MEMORY.md and other imported files drift stale as parallel sessions write to them. Use when the user says "/refresh", "reload context", "re-sync", "are you up to date", "did you see the latest", or suspects the session is working from a stale snapshot. With a subject argument (e.g. "/refresh billing"), ALSO looks for that subject in the reloaded files and live sibling sessions. Read-only and self-contained; it reloads and reports the delta, and does NOT judge whether /reflect should run next (that judgment lives in /rnr).
---

# /refresh: re-sync a live session to current disk state

Claude Code loads `CLAUDE.md` + `@imports` only at session start. This skill re-reads the memory
and context files from disk NOW so a long-running session stops working from a stale boot
snapshot.

`$SESSION_NOTES` below is the session-notes checkout (set by `install.sh`).

## Step 1: detect scope from the working directory

Print the resolved config:

```bash
python3 $SESSION_NOTES/bin/notes_config.py
```

`paths.global_store` is the global memory store. `scopes` lists each configured scope with its
`root` (project folder) and `store` (memory dir). The current scope is the configured scope whose
`root` contains the cwd (deepest root wins); none means bare/global. Every session ALSO reloads
the global store. Do not hardcode scope names or paths here: a new scope must refresh correctly
with zero skill edits.

## Step 2: re-read the files for that scope (use the Read tool on each that exists)

**Always (global store):**
- `<global_store>/MEMORY.md`
- `<global_store>/RESUME.md`

(Re-read MEMORY.md only, never REFERENCE.md: the cold tier is never `@import`ed, so it is not
boot-prefix state that can have gone stale.)

**Current scope, if any:**
- `<scope store>/MEMORY.md` and `<scope store>/RESUME.md`

**The nearest CLAUDE.md files:** walk up from the cwd to the home directory, plus the global
`<claude_home>/CLAUDE.md`, reading each folder-root `CLAUDE.md` (or `.claude/CLAUDE.md`) that
exists, and every file their `@import` lines name that is not already in the list above.

Read whatever is present; silently skip any path that doesn't exist (scopes evolve). These reads
put the CURRENT disk content into the conversation, overriding the stale start-of-session copy.

## Step 2b: peek live sibling sessions (the LIVE half; disk is the RECORDED half)

The files above are *reconstruction*: siblings write them only at decision points, so a sibling
mid-build is invisible on disk until it finishes. Complement the disk read with the direct live
view:

1. `python3 $SESSION_NOTES/bin/session_peers.py`: list every parallel Claude session in tmux
   (name, working/idle, cwd). Cheap; always run it. Outside tmux it lists nothing; say so only if
   it matters.
2. `--peek <name>` **selectively, on signal only**: a sibling that is WORKING, or whose name/cwd
   matches the `/refresh <subject>` argument. Each peek costs a screenful of context, so don't
   peek idle or irrelevant siblings.

Peek output is **judgment-only**: use it to know what's in flight and avoid colliding; never
persist it anywhere (it's a read of someone else's screen, not a record). Honest limit: this sees
only tmux-hosted sessions on this machine and only their visible screenful; headless jobs are
invisible to it.

**How to read a peek:** text on the input line at the bottom of the pane is **NOT evidence of
intent or a pending action**. It is either the user's typed-but-UNSENT input OR, just as often,
**GHOST AUTO-SUGGESTION**: the TUI's greyed autocomplete of a likely command that nobody typed and
nobody has pressed enter on. You cannot tell which from the peek, so **never attribute prompt-line
text to the user or treat it as "about to run"**: a `finish the reflect` or a queued `/rnr` at the
prompt has NOT executed and may be pure autosuggestion. A sibling is only actually mid-`/reflect`
if its *scrollback* shows reflect activity IN PROGRESS (skill launch, memory/RESUME edits); that is
the ONLY signal that counts. Likewise the "working" glyph alone does not mean mid-reflect; it often
just means a background agent is running while the foreground sits idle. Reading the prompt line
as intent inflates `CAUTION` verdicts in `/rnr` into false positives; judge the sibling's state
from scrollback plus the idle/working glyph only.

### Step 2b-ii: on a DETECTED collision only, send ONE addressed message

The peek is a **read** by default and stays that way. But files a session appends to reach other
sessions at **boot**, not while they run: a running sibling gets no notice that a file changed.
So when the peek finds a **concrete live collision**, that sibling has *no other channel*, and one
addressed message is the right instrument:

```
python3 $SESSION_NOTES/bin/session_bus.py send --to <N> "<one line: the overlap + what you're about to do>"
```

**`--to`, never `--all`. One message. Silent in every other case.** With many sessions, a routine
"here's what I did" emit would put an injection on every pane's next prompt, exactly the kind of
never-silent recurring injection that trains everyone to ignore it.

**Counts as a collision:** a sibling mid-`/reflect` on the same RESUME / memory store you're about
to write; a sibling editing the same file, worktree or shared surface you're about to change; a
sibling about to redo work you just finished.

**Does NOT count:** a general activity summary; anything durable (that's `/reflect`); and *never* a
collision inferred from the sibling's bottom input line, which Step 2b already establishes is often
ghost autosuggestion.

**Honest limit, state it rather than oversell it:** the bus cannot preempt work in flight. A WORKING
sibling receives this on its **next prompt**, possibly after the colliding step already happened,
so the message informs its next move rather than stopping the current one. An IDLE sibling isn't
colliding with anything by definition, so it is not the target.

**Phrase it as information, not instruction.** It arrives on the recipient's side as a peer's
information that explicitly cannot authorize action, so an imperative would both misread the
channel and get discounted.

**Do NOT add an emit to `/reflect`.** A reflecting session is usually wrapping up, so it would open
a conversation it can't finish; and the bus self-expires and carries no authority, making it a
strictly worse carrier than the durable stores reflect already writes.

## Step 3: if a subject argument was given, pick it up

When the user passed a subject (e.g. `/refresh billing`), after reloading, search the files just
read and the scope's memory store (`grep -ril <subject> <store>`) for it, read the matching memory
files, and peek any sibling whose name or cwd matches. If nothing matches, say so in one line.

## Step 4: report concisely

Tell the user what was reloaded and surface anything that CHANGED versus what the session had been
assuming, especially a refreshed `RESUME`, new memory index lines, or (with a subject) what was
found. Include live siblings ONLY when relevant (a sibling mid-flight on something this session
touches, or on the subject); silence is the all-clear, so don't list idle siblings. Lead with the
delta, not a file list. If nothing material changed, say so in one line.

Then STOP. `/refresh` is a reload, not a wrap-up: do not judge whether `/reflect` should run next,
do not emit a `REFLECT:` verdict, and do not offer one. A naked `/refresh` mid-session is usually
someone re-syncing to keep working, and an unasked-for wrap-up prompt is noise. That judgment lives
in `/rnr`, which is where it is actually acted on.

## Notes

- Read-only and idempotent: `/refresh` never writes (the one exception is the Step 2b-ii bus
  message on a detected collision).
- `/refresh` never invokes `/reflect` and never recommends it. The sibling peek in Step 2b feeds
  collision-avoidance here and (when chained) `/rnr`'s gate; it is not a reflect recommendation.
- This does NOT re-trigger Claude Code's native @import machinery (there is no API for that
  mid-session); it achieves the same end by reading the same files into context directly.
- Sibling skills: `/reflect` (write durable learnings), `/rnr` (refresh, then a gated reflect).
