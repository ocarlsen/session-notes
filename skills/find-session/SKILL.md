---
name: find-session
description: Find which Claude Code session discussed something - "which session did I tell about X", "where did we talk about Y", "find that conversation about Z". Searches live tmux sessions first, then every transcript on disk, and reports the tmux session name to attach to or the resume command for an ended one.
---

# find-session

The user remembers a conversation but not which pane it happened in. Find it.

Trigger on the intent, not the slash command: "which session did I tell about the
rate limiter", "where did we discuss the invoice totals", "find that conversation
about X" all mean this.

## Run the script, do not hand-roll the search

`$SESSION_NOTES` is the session-notes checkout (`install.sh` sets it in settings.json's `env`).

```
python3 $SESSION_NOTES/skills/find-session/find_session.py '<words>'            # live sessions
python3 $SESSION_NOTES/skills/find-session/find_session.py --all '<words>'      # + ended ones
python3 $SESSION_NOTES/skills/find-session/find_session.py --all --since 30 '<words>'
```

Useful flags: `--role user|assistant|any` (default any), `--limit N` hits per
session, `--raw` to pass a real regex, `--window N` for more context.

The filters that make it work live in the script, with the reason behind each
one in its docstring. A hand-written grep over the transcripts directory
reproduces none of them and returns mostly noise: every transcript carries the
memory index in its boot prefix, so common words match everything. Read the
docstring before changing anything there.

Live-session attribution needs the pane registry hook
(`hooks/session_pane_registry.py`) installed; without it every live pane is
reported as unattributed and only `--all` finds anything.

## The judgment part: this is what the skill is for

**The user's remembered wording is a paraphrase.** Someone recalls "the rate
limiter not matching the dashboard" and the real conversation contained neither
"dashboard" nor "not matching": it was a table of request counts. Search
the *subject* (one or two distinctive nouns), never their sentence.

**Widen in this order**, checking the result before each step:
1. the distinctive noun alone, live sessions
2. `--all --since 30`
3. `--all`, and drop to a single word
4. a number, an amount, or a proper noun from the memory instead of the topic

**A zero-hit live search is a real answer, not a failure.** Say the topic is not
in any live session, then offer the `--all` sweep rather than silently widening.

**Search both roles by default.** The thing the user remembers telling a session
is often only in the *reply*: they pasted a screenshot, or wrote four words.
Narrow to `--role user` only when they are looking for their own instruction
specifically.

**Report the destination, not the transcript.** The user wants to get back to the
conversation: name the tmux session and the attach command, or `claude --resume`
for an ended one, plus one or two lines of what was actually said there. Quote
only enough to confirm it is the right conversation.

**Heed the unattributed-pane note.** If the script reports live panes it could
not attribute, a live session may hold the answer and not have been searched:
say so and offer `--all`, rather than reporting a clean miss.

## Never persist what you read

Transcript content is judgment-only, and a transcript is more sensitive than a
pane snapshot. Never write results into memory files or any shared store, and
never quote another session's content into an outbound message. Reporting the
session name and a confirming line to the user is the whole deliverable.
