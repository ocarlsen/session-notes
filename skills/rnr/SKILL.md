---
name: rnr
description: "Refresh 'n' reflect: run /refresh then /reflect as one command. Use when the user says '/rnr', 'refresh and reflect', or is wrapping up a long-running session and wants context re-synced before memory is written. Refresh runs FIRST so /reflect writes memory from current disk state (RESUME/MEMORY may have been appended to by parallel sessions since boot) instead of the stale boot snapshot. Between the two steps /rnr forms its OWN reflect verdict and gates on it: RUN proceeds via the reflect lock, SKIP pauses for a one-tap confirm, CAUTION WAITS on the lock automatically (never asks 'wait or go?', never clobbers a sibling mid-reflect). A naked /refresh deliberately does none of this. Optional argument is passed to /refresh as the subject (e.g. '/rnr billing'); the special argument 'consolidate' is passed to /reflect instead."
---

# /rnr: refresh, then reflect

A thin chain over the two existing skills. Do NOT duplicate their logic here: invoke each via the
Skill tool so improvements to either skill flow through automatically.

`$SESSION_NOTES` below is the session-notes checkout (set by `install.sh`).

## Step 1: /refresh

Invoke the `refresh` skill. If the user gave an argument other than `consolidate`, pass it through
as the subject (e.g. `/rnr billing` becomes `/refresh billing`). Complete the refresh fully
(including any subject pickup) before moving on.

`/refresh` is a pure reload: it does NOT judge whether to reflect (deliberately, so a naked
`/refresh` mid-session stays a re-sync and nothing more). Forming that judgment is Step 1b's job,
here, using what the refresh just put in context.

## Step 1b: form the reflect verdict

The refresh has just surfaced both signals needed to judge whether `/reflect` is worth running
*right now*. Weigh two things:

1. **Is there anything worth persisting?** Did THIS session produce durable material (a new
   feedback rule, a decision, shipped/in-flight state worth a RESUME line, a corrected fact), or
   was it read-only Q&A / trivial edits with nothing to remember? Reflecting an empty session
   writes noise AND takes a needless turn at the shared stores.
2. **Is it safe to write right now?** Use refresh's Step 2b sibling peek: is a sibling session
   mid-`/reflect` (or actively editing RESUME / the memory store)? That's the clobber window. The
   merge-don't-clobber re-read helps, but if a sibling is actively reflecting, waiting is cleaner.
   Judge that from **scrollback only**, never from a sibling's bottom input line (often ghost
   autosuggestion) and never from the working glyph alone; refresh's Step 2b spells out why, and
   misreading it inflates `CAUTION` into false positives.

Emit exactly one verdict, with a one-clause why:

- **`REFLECT: RUN`**: durable material exists and no sibling is mid-reflect.
  (e.g. *"RUN: shipped the parser fix + a new feedback rule, no sibling reflecting."*)
- **`REFLECT: SKIP`**: nothing durable this session.
  (e.g. *"SKIP: read-only Q&A, nothing to persist."*)
  **A `SKIP` you have to CAVEAT is a narrow `RUN`.** If the verdict comes out as "SKIP, but the one
  thing I'd add is X", then X *is* the durable material and the honest verdict is `RUN` scoped to
  X. Emit `RUN` naming just that edit, rather than a SKIP the user has to override. Genuine `SKIP`
  has no such sentence available; if you can name the edit, you have already found the gap.
- **`REFLECT: CAUTION`**: there IS material but a sibling is mid-reflect / mid-RESUME-write.
  (e.g. *"CAUTION: sibling `alpha` reflecting now; will wait on the reflect lock."*)
  `CAUTION` is a **statement of what happens next, never a question**: the arbiter is the atomic
  reflect lock (`$SESSION_NOTES/bin/reflect_lock.py`), so two simultaneous `/rnr`s take turns
  without asking the user or negotiating over the bus. The peek only informs the wording; a bus
  line to the sibling is optional courtesy (it lands on their next prompt), not the mechanism.

## Step 2: /reflect, GATED on the verdict

`/rnr` exists to be run without thinking, so this step must NOT reflect unconditionally: a
reflexive reflect can write noise or clobber a sibling that's mid-`/reflect`. Honor the Step 1b
verdict:

- **`REFLECT: RUN`**: invoke `/reflect` normally. This is the common case. `/reflect` itself
  acquires the reflect lock at its pre-write chokepoint (its Step 5), so even a peek false-negative
  can't produce a clobber.
- **`REFLECT: SKIP`**: **do NOT auto-reflect. Pause and ask.** State the verdict and its one-clause
  why in one line, and let the user decide (a minimal affirmative runs `/reflect`; anything else
  ends the chain). This converts the reflex into a one-tap confirm and spares a pointless write.
- **`REFLECT: CAUTION`**: **WAIT, automatically; do NOT ask "wait or go?"** Offering "recommended:
  wait" as a choice only adds a round trip to the one safe answer. A `CAUTION` means a sibling
  looks mid-`/reflect`; the arbiter is the **reflect lock**, not the peek. Say in one line that
  you're waiting on `<sibling>`, then run `python3 $SESSION_NOTES/bin/reflect_lock.py wait`
  **backgrounded** (`run_in_background: true`, never a foreground poll that locks the user's
  pane) and, once it exits 0 (acquired), proceed to `/reflect` (which will see the lock already
  held by this session and keep it). The lock is atomic, so when two sessions `/rnr` at once and
  each sees the other mid-reflect (a mutual-caution standoff a peek alone cannot break), exactly
  one acquires and reflects first; the other's `wait` acquires the moment it's released:
  self-serializing, no user arbitration, no bus negotiation. If `wait` times out (exit 2, default
  15 min), report it and ask: that's a genuinely stuck holder, the one case that IS the user's
  call. A stale lock (crashed holder) is broken loudly by the helper itself.

When you do reflect: if the user's argument was `consolidate`, pass it through (`/rnr consolidate`
becomes `/reflect consolidate`); otherwise invoke plain `/reflect`. Reflect then operates on freshly
re-synced context; in particular, the RESUME merge-don't-clobber re-read is grounded in what's
actually on disk.

## Notes

- Order is deliberate: refresh first, reflect second. Never reverse it.
- **Both the verdict AND the gate live here on purpose.** `/refresh` is a reload with no wrap-up
  judgment, so a naked `/refresh` mid-session bypasses the check entirely; `/rnr` is the only path
  that judges, and it judges only because it is about to act on the answer. Don't push the verdict
  back into `/refresh`: re-syncing to keep working shouldn't ask about ending the session.
- If Step 1 fails or is interrupted (so Step 1b has no refreshed state to judge from), still ask
  whether to proceed with `/reflect` rather than silently skipping it: same pause-and-ask default.
