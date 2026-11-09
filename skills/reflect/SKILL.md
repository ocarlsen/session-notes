---
name: reflect
description: "End-of-session reflection skill that FIRST checks for uncommitted changes and offers to commit them, then reviews the conversation, extracts learnings, and updates memory files and CLAUDE.md at both project and global scope. Deduplicates memories across scopes: if the same pattern appears in multiple projects, it gets promoted to global. Use explicitly with /reflect, or /reflect consolidate for a deep cross-file pass. Claude should proactively offer to run /reflect when a session seems to be wrapping up (user says 'thanks', 'done for now', 'that's all', 'bye', 'good enough', 'let's stop here', or similar). Say: 'Before we wrap up, want me to run /reflect to capture anything worth remembering from this session?' Do not offer more than once per session."
---

# /reflect: session reflection and memory maintenance

## Modes

**`/reflect`**: default. Analyzes the current session only. Fast.

**`/reflect consolidate`**: also reads all existing memory files across project and global scopes
to prune stale entries, resolve contradictions, and promote duplicates to the appropriate level.

## Conventions

`$SESSION_NOTES` is the session-notes checkout (`install.sh` sets it in settings.json's `env`). Every helper below is
`python3 $SESSION_NOTES/bin/<script>.py`. If the variable is unset, find the checkout (the
directory holding `bin/notes_config.py`) and say so once.

This skill is scope-AGNOSTIC. It never hardcodes a scope's name or path: scopes come from the
session-notes config. Run

```bash
python3 $SESSION_NOTES/bin/notes_config.py
```

to print the resolved config as JSON: `paths.global_store` (the global memory store),
`paths.claude_home`, and `scopes`, each with its `name`, `root` (project folder) and `store`
(memory dir). A new scope is one config entry, never an edit here.

---

## Step 0: Check for uncommitted work, FIRST, before anything else

Run `git status --porcelain` in the cwd (skip if not a git repo). Clean tree: say so in one line
and proceed to Step 1. Dirty: surface it and resolve BEFORE reflecting. Two situations:

**(a) A per-session WORKTREE.** The tree is isolated, so everything dirty is this session's and
`git add -A` is fine. If the project works through PRs, offer that path rather than a direct
commit to the main branch. Ask: **"Commit (or open a PR with) this session's changes before
reflecting?"**

**(b) A SHARED checkout.** The dirty tree may contain other sessions' in-flight hunks. **Stage
only THIS session's files, never `git add -A`**; commit-forward only, never `checkout`/`reset`/
`restore` a shared file (that silently destroys a sibling's work). If a dirty file isn't clearly
yours, peek siblings first (`$SESSION_NOTES/bin/session_peers.py`; a file a live sibling is
visibly mid-flight on goes in the "other" bucket) and ask only about files still unattributed.
Present the split ("this session's" vs "other/unsure") with a per-concern commit plan and ask:
**"Commit these before reflecting?"**

Either way: on yes, do it; on "just reflect"/"skip", leave the tree untouched and proceed. Never
push unless the user asks for the push.

---

## Step 0b: Triage the work items THIS session filed

A session that finds problems and files them (issues, TODOs, backlog entries) is doing the right
thing. But a filed item that nobody names on the way out reads as handled when it is merely
recorded. So before reflecting, account for every item this session opened.

**This is a triage, NOT a mandate to finish them.** "Fix everything I filed before leaving"
would tax filing itself, and filing is the behavior we want. Nor is the backlog an agenda to
re-list each turn: this step covers only what THIS session created.

1. **Enumerate from the conversation**: every item this session's own tool calls created. If the
   project has a tracker you can query, confirm each one's current state there.
2. **Decide each one**, and act where the answer is "now":
   - **Small and self-contained**: offer to fix it in-session, with its size named.
   - **Already fixed in passing**: close it now rather than leaving a stale open item.
   - **Genuinely bigger, blocked, or a judgment call for the user**: leave open, and say so.
3. **Report every one in the exit summary** (Step 6), each with its size and why it is deferred.
   An item left open is fine; an item left open *silently* is the failure this step prevents.

This step is the ONE triage point. A per-reply guard that nags whenever a reply fails to name a
filed item reads only the current reply, so it fires on items discussed two replies earlier and
polices every reply when the intent is a check before EXIT. The accepted cost: a session that
ends without `/reflect` or `/rnr` gets no triage.

---

## Step 1: Determine scope paths

From the config printed above and the cwd:

- **Global memory dir**: `paths.global_store`.
- **Scope memory dir**: if the cwd is inside a configured scope's `root`, that scope's `store`
  (the deepest matching root wins). Otherwise there is no scope store: only global applies.
- **Project CLAUDE.md**: the folder-root `CLAUDE.md` of the scope root (or the cwd). Both root
  `CLAUDE.md` and `.claude/CLAUDE.md` auto-load; edit whichever exists, never create a second copy.
- **Global CLAUDE.md**: `<paths.claude_home>/CLAUDE.md`.

Note which paths currently exist; don't assume they all do.

**The global store is the canonical store.** Its `MEMORY.md` and `RESUME.md` load in every
session via native `@import` lines in the global `CLAUDE.md`. Default new memories there
regardless of cwd. Write to a scope store only when a memory is genuinely scope-local (detail
about one project must NOT load in other projects' sessions). A scope store loads only if that
folder's `CLAUDE.md` `@import`s it; if the import is missing, add it, or the store is a silent
orphan nothing ever reads.

---

## Step 2: Analyze the current session

Read the full conversation. Extract candidates across four memory types:

### Feedback (how to work with this user)
- **Corrections**: user said "no", "don't do that", pushed back on your approach
- **Approvals**: accepted a non-obvious choice, said "exactly", "perfect", "keep doing that"
- **Style signals**: tone, verbosity, format preferences that emerged

### User (who this person is)
- Role, expertise, or goals revealed for the first time or meaningfully updated

### Project (what's happening in this codebase/initiative)
- Goals, decisions, constraints, deadlines established this session, and the *why* behind them
- Convert relative dates to absolute

### Reference (where to find things)
- External systems, tools, URLs worth locating again

### CLAUDE.md candidates
- Conventions/workflows/constraints that should apply to *every* future session, not derivable
  from the code

### Repeat-directive check (do this for every FEEDBACK candidate)

Before treating a correction as NEW, grep the existing store (`MEMORY.md` AND `REFERENCE.md`
indexes, plus likely `feedback_*` files) for a prior version of the same directive. **A directive
the user has already issued is not a new memory: it is an internalization failure.** Handle it as
the highest-value signal in the session: strengthen the EXISTING rule (sharper trigger phrasing,
an example of the miss), consider promoting it from prose to a code guard if the project supports
one, and TELL the user it was a repeat: "you've said this before; here's what I changed so it
sticks."

### Then gap-filter against what's ALREADY durably recorded (BEFORE proposing)

A learning is only worth saving if it would otherwise be LOST. For each candidate, check whether
it's already on disk, and **DROP it if so**: a tracker entry, commit message, code comment, or
guard is self-documenting; don't restate it. Keep only the **gaps**: the *why*, the trade-offs,
the non-obvious gotcha, the preference expressed, things that exist ONLY in this conversation.
Half-captured (fact in the tracker, reasoning not)? Save only the missing half and **link** to the
record. It is a valid and common outcome to conclude there's little or nothing to add. Do NOT
manufacture memories to look thorough. Less, but only the gaps.

### Step 2b: Skill feedback

A skill's own output can only be corrected by the user who reads it, never recovered from memory
after the fact and never seen by a hook (a PostToolUse hook sees what the model wrote, not how the
user reacted to it). So capture it HERE, mechanically, every time:

1. **List the skills loaded this session** from the transcript itself: Skill tool calls, and
   user-typed `/<skill>` invocations. Not from memory of what seemed used. **/reflect itself is
   NOT counted** (it is the thing running this step, not a skill being reviewed by it).
2. **For each one, find the user messages that reacted to its output:**
   - **`edit`**: the user replaced its text with different text.
   - **`reject`**: the user rejected it outright.
   - **`approve`**: the user's message NAMES the specific non-obvious choice being approved
     ("exactly, no body needed for a pure rename"). A bare "ok", "thanks", or "looks good" is
     never a row: it carries no evidence of which choice it is approving.
   - Quote verbatim into `--before`/`--after`; never paraphrase. If the literal text isn't
     recoverable from the transcript, log nothing for that reaction rather than reconstruct it.
     Text that is multi-line, or happens to start with `-`, goes through `--before-file`/
     `--after-file` (a path, or `-` for stdin), never pasted raw onto the command line.
3. **Pick a habit slug** naming the pattern the reaction is evidence of (kebab-case, e.g.
   `no-trailing-summary`). Read that skill's ledger first
   (`<claude_home>/skill-feedback/<skill>.jsonl`) and reuse an existing slug for the same habit,
   so rows accumulate instead of fragmenting.
4. **A skill not under `<claude_home>/skills/<name>`** (a plugin's `ns:skill`, or a project's own
   `.claude/skills/`) still counts toward N below, but log nothing for it: never guess a
   `--skill-dir`. Only pass `--skill-dir` when the transcript itself shows the exact path (e.g. the
   Skill tool's own result named it).
5. **Write every row through the script, never a hand-typed JSON line**:
   ```bash
   python3 $SESSION_NOTES/bin/skill_feedback.py add <skill> --verdict edit|reject|approve \
     --habit <slug> --before "<literal skill output>" --after "<literal user text>"
   ```
   `session` defaults to `$CLAUDE_CODE_SESSION_ID`; pass `--date` only to backdate.
6. **Run `propose` for every skill that got a row this session**, not only one that looks
   qualifying by eye:
   ```bash
   python3 $SESSION_NOTES/bin/skill_feedback.py propose <skill>
   ```
   Note any qualifying habit it reports for Step 6. This is informational only here: `propose`
   never edits SKILL.md itself; that stays a separate, user-approved step outside `/reflect` (see
   docs/tools.md).

**CANARY, derived, never counted by feel:** run
```bash
python3 $SESSION_NOTES/bin/skill_feedback.py canary
```
and paste its printed line, verbatim, into Step 6: `skill feedback: N skills used, M rows logged`
(N = distinct skills invoked by Skill tool call or user-typed `/<skill>`, excluding `reflect`/`rnr`; M =
this session's row count summed across every ledger under `skill-feedback/`). It defaults to
`$CLAUDE_CODE_SESSION_ID` and locates its own transcript; pass `--transcript PATH` only if it
can't find one. This includes the `0 skills used, 0 rows logged` case, so a skipped capture is
visible rather than silently absent.

---

## Step 3: In consolidate mode, read existing memory files

Read `MEMORY.md` AND `REFERENCE.md` in both scopes, then the indexed files. For each existing
memory: **still true?** (contradicted/superseded this session?), **right scope?** (project-level
but actually universal, or appears in multiple projects?), **duplicate?** (same content as another
file across scopes?). Flag candidates for update, deletion, or promotion (project to global).

Run the hygiene canary rather than re-deriving its checks (orphans, dupes, frontmatter drift,
index bloat) by hand, so there is one definition of "hygienic"; consolidate then fixes what it
flags:

```bash
python3 $SESSION_NOTES/bin/memory_hygiene_canary.py
```

---

## Step 4: Summarize the changes, then apply them

Show a clear summary organized by scope, then **apply directly**; do NOT ask "proceed?". Memory is
reversible (and version-controlled if the store is a git repo), so a confirmation gate here is
pure friction. Only Step 0's commit offer stays a real confirmation (shared-tree safety).

```
## Proposed memory changes

### New: Global (<global store>)
- feedback_terse_responses.md: User prefers concise answers; no trailing summaries

### New: Project (<scope store>)
- project_auth_rewrite.md: Auth middleware rewrite is compliance-driven, not tech debt

### Updates / Promotions (project to global) / Deletions

### CLAUDE.md changes
- [global CLAUDE.md] No changes proposed
```

If the user interjects to adjust or skip something, honor it; otherwise apply as summarized.

---

## Step 5: Apply the changes

**FIRST, acquire the reflect lock** (serializes parallel reflects: always wait, never clobber).
Before ANY write in Steps 5 to 7:

```bash
python3 $SESSION_NOTES/bin/reflect_lock.py acquire
```

Exit 0 = acquired (or already yours). Exit 1 = held by another: do NOT ask "wait or go?". Say
whose lock you're waiting on, run `python3 $SESSION_NOTES/bin/reflect_lock.py wait`
**backgrounded** (`run_in_background: true`, never a foreground poll), and write only after it
exits 0 (acquired). Exit 2 = timed out: report it and ask; that is a genuinely stuck holder. The
helper breaks a stale lock (a crashed holder) loudly by itself. Acquiring is idempotent if `/rnr`
already took it. The lock is taken HERE (the pre-write chokepoint), not in Step 0, because Steps 0
to 4 can pause on the user longer than the stale-break window and analysis needs no exclusivity.
Hold it across Steps 5, 5b, 6, 7; release in Step 8. If aborting after acquiring, release before
ending the turn.

**Before writing: re-read, don't clobber (parallel sessions).** For every file you **update**
(RESUME.md, CLAUDE.md, an existing memory file): re-read it from disk immediately before writing
and MERGE your change into the current content: append/add, never regenerate a whole file from
your in-memory copy. New files write directly.

Write in this order:

1. **New memory files**, with standard frontmatter:
   ```markdown
   ---
   name: descriptive-kebab-slug
   description: One-line description used to decide relevance in future sessions
   metadata:
     type: user | feedback | project | reference
   ---

   [content body]
   ```
   (`metadata:` nesting matters; a top-level `type:` is format drift.) `feedback`/`project`:
   lead with the rule/fact, then **Why:** and **How to apply:** lines. `user`: factual, positive.
   `reference`: what it is and when to use it.

2. **Update MEMORY.md** in each affected scope:
   - **New entries go through the atomic append helper, never a hand-edit.** Two sibling sessions
     that both re-read, append in memory and write back will silently drop one entry; the helper
     makes re-read, append, write a single locked operation:
     ```bash
     python3 $SESSION_NOTES/bin/append_memory_index.py <path-to-MEMORY.md> "- [Title](file.md) - one-line hook"
     ```
     Idempotent on exact duplicates (exit 0 either way). It refuses (exit 2, before writing
     anything) an over-cap hook: the hook is the CURRENT RULE, and the incident or edge case
     belongs in the memory file's **body**. Shorten and retry.
   - **A `reference_*.md` entry (tool/vendor/service/machine lookup) routes to REFERENCE.md**
     beside MEMORY.md automatically. REFERENCE.md is never `@import`ed, so lookups stay out of
     the boot prefix. Pass `--pin` only when the entry is a boot-critical trap (a NEVER/ALWAYS
     rule, or a silent-false-result gotcha that bites before anyone would think to search):
     ```bash
     python3 $SESSION_NOTES/bin/append_memory_index.py <path-to-MEMORY.md> "- [Title](reference_x.md) - hook" --pin
     ```
     Default to no `--pin`; most `reference_*.md` facts are lookup-shaped, not traps.
   - **Removed/promoted entries** still need a direct edit (the helper only appends); apply the
     re-read-before-write discipline. A `reference_*.md` entry may live in either MEMORY.md or
     REFERENCE.md; check both before concluding it's gone.
   - **After any hand-edit of an index**, lint it (it checks only new or changed lines, against
     `--baseline <snapshot>` or the store's git HEAD; it refuses to run with neither):
     ```bash
     python3 $SESSION_NOTES/bin/memory_index_lint.py <path-to-MEMORY.md>
     ```

3. **Update CLAUDE.md files**: add sections/lines; don't remove existing content unless asked.

4. **Promotions**: copy to global, delete from project, update both MEMORY.md files.

5. **Deletions**: remove the file and its index line.

Then check the boot-prefix budget (exit 1 names each file over budget; trim the index or move
detail into memory bodies):

```bash
python3 $SESSION_NOTES/bin/boot_budget.py check
```

---

## Step 5b: Refresh the RESUME note (in-flight work)

RESUME notes are **folder-specific**: the global RESUME carries only cross-cutting deadlines and
hand-offs (one line each); each scope's own RESUME sits beside its store, `@import`ed by that
folder's CLAUDE.md. Refresh whichever scope(s) THIS session touched; touch the global one only
when a cross-cutting item changed. New scope: create its RESUME and add the `@import`.

- **Peek siblings before writing** (`python3 $SESSION_NOTES/bin/session_peers.py`): it protects
  the write race (merge around a sibling's fresh entry) and stale claims (don't persist "nothing
  in flight" about work a sibling is visibly mid-build on). Peek output is judgment-only; never
  quote or persist it.
- **Re-read before write**, then merge. Header stays the slim form: `<date> (newest entry below)`;
  never duplicate the summary into it.
- **Entries SHORT, 3 to 6 sentences, pointer-dense.** RESUME is boot-prefix cost in every
  session. What shipped/decided, plus PR/issue/memory-file refs, plus WATCH/NEXT. The full
  story lives in git, the tracker or memory: write it THERE, point HERE. Test: if a sentence
  doesn't change what the next session DOES, cut it.
- Keep a light checklist at most, and prune stale `[x]` items.
- **Over its cap:** when the RESUME is over its cap (`resume_bytes` in the config's
  `[budgets]`), hand-trim it in this step. `python3 $SESSION_NOTES/bin/boot_budget.py check` lists every
  file over its cap.
  - Move only FINISHED narrative (shipped, closed, superseded) into `RESUME-archive.md` beside
    that RESUME (same directory), verbatim and newest on top, never deleted outright.
  - NEVER move an open deadline, dated obligation, WAITING item, or anything in flight; those
    stay even if the file stays over the cap (then say so in the Step 6 report instead of
    cutting live items).
  - Re-read before writing (sibling race) and re-read after to confirm the open items survived.

The RESUME note is the resume channel: write what a future session needs to continue, since the
conversation itself does not carry across sessions.

---

## Step 6: Report what changed

Brief list: files created/updated/deleted and where; CLAUDE.md changes; promotions. One short
sentence per item. No paragraphs.

**Always include the Step 2b canary line**: `skill feedback: N skills used, M rows logged`, even
when both are zero (N/M derived as Step 2b describes, never counted by feel). If `propose` (Step
2b, item 6) found a qualifying habit for any skill, name it and the exact
`skill_feedback.py propose <skill>` command; do NOT edit that skill's SKILL.md here.

**Then the Step 0b triage, always, including when nothing was filed** ("no new items filed this
session" is one clause, and its absence is indistinguishable from forgetting to look). One line
per item this session opened: id, one-phrase gist, and **fixed in-session / closed / left open
because <reason + size>**.

---

## Step 7: Commit your store

For each memory store this reflect wrote to (global and scope): if the store directory is inside
a git repo, show `git -C <store> -c pager.diff=false diff --stat` plus the untracked files, and
offer to commit them with a one-line message. Stage only the files this reflect touched. **Never
push without the user's ask.** Not a git repo: skip silently.

---

## Step 8: Close with an exit-readiness line

**First, release the reflect lock** (`python3 $SESSION_NOTES/bin/reflect_lock.py release`)
before the closing line, never after or skipped.

Users often ask "ok to exit?" after a reflect; answer it pre-emptively, in one line, as the last
thing `/reflect` says. Default is **`Safe to exit`**. Flag only when exiting would actually
lose, kill or clobber something:

1. **Uncommitted work THIS session created** (the user chose "just reflect", or residual code):
   name the files, offer to commit now.
2. **A background job/agent/build/deploy this session started is still running**: exit kills it.
3. **An open worktree with un-merged work**: FYI, not a hard blocker.

Do NOT flag safe-by-design states: an uncommitted memory store the user declined to commit, or
another session's dirty files.

**Always emit exactly one closing line; the state changes the REGISTER, not whether the line
exists:**
- Clear (common case): **`Safe to exit`**, plain and unpadded.
- **`Before you exit: <what + why>`**: one of the cases above, concrete, with the one action that
  clears it. The warning register belongs to this case ONLY.

> Two axes, kept separate: whether the line exists, and how it is phrased. Dressing a clear
> state as a warning is noise; suppressing the clear-case line entirely leaves the user unable to
> tell "safe" from "forgot to check".

---

## What NOT to save

- Code patterns, conventions, architecture (read the code); git history (`git log`)
- In-progress task state, EXCEPT the short RESUME note (Step 5b), its designated home
- Things already in CLAUDE.md or the project's durable record (tracker, commit, guard): save the
  GAP the record omits, link instead of duplicating
- Peeked sibling-session content: judgment-only, never a memory/RESUME source

**The test**: would a future Claude instance be meaningfully better at helping this user if it
knew this, AND is it not already on disk somewhere it would look? **Capturing nothing is a fine
result; padding the store is not.**

---

## Memory file naming conventions

Descriptive, stable `snake_case`: `feedback_no_mock_databases.md`,
`user_go_expert_react_novice.md`, `project_auth_compliance_rewrite.md`,
`reference_grafana_oncall_dashboard.md`
