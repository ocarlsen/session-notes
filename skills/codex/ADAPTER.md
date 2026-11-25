# Codex substitutions (shared by the Codex refresh, reflect and rnr skills)

The Codex skills run the SAME procedure as the Claude Code skills in `skills/<name>/SKILL.md`
(one source of truth, so a fix there reaches both agents). Read that file and follow it, with
every substitution below applied. Where a substitution says "skip", skip the step silently.

`$SESSION_NOTES` below means the checkout path each Codex SKILL.md resolves first, from its own
symlink. Do not rely on the environment variable alone: a session started from Codex's agent
command center can run without it. Every helper call gets `SESSION_NOTES_AGENT=codex` in front,
or the helpers resolve Claude's stores. The one exception is `codex_save.py commit` (see Writing
memory), which sets codex mode itself and must run with exactly its own text. Never run a `bin/` script from the current folder.

## Two memories, never one

Codex keeps its OWN store under `~/.codex/session-notes/`. Never read, write, grep or list
anything under `~/.claude` (or the configured `paths.claude_home`), even to compare: the user
keeps the two agents' memories separate on purpose. The helpers refuse a Codex path under
the Claude home; do not work around that.

## Writing memory

Only the reflect procedure writes the store. When the user says "remember this" outside
`$reflect`, say it will be saved at the next `$reflect` (or run `$reflect` if they ask).

Every write to the store goes through `codex_save.py`, in two steps, and nothing else: no
apply_patch, editor, heredoc or `append_memory_index.py` call into `~/.codex/session-notes`, and
no `reflect_lock.py acquire`/`release` of your own. The split is what keeps the desktop app from
asking for approval on every write.

1. **Stage** each change (runs inside the sandbox; it only writes `/tmp`):
   - a memory file: `SESSION_NOTES_AGENT=codex python3 $SESSION_NOTES/bin/codex_save.py stage --store global --file NAME.md` with the whole file on stdin (`<<'EOF'`)
   - its index line: `SESSION_NOTES_AGENT=codex python3 $SESSION_NOTES/bin/codex_save.py stage --store global --index-line "- [Title](NAME.md) - hook"`
   - RESUME.md: first `... codex_save.py hash --store project` (or `global`) when you READ it,
     then `... codex_save.py stage --store project --resume --base <that hash>` with the whole new
     file on stdin.
   `--store project` is this folder's project store; `global` is the global one.
2. **Commit**, as a command on its own, with exactly this text and nothing before or after it (no
   `cd`, no env prefix, no `&&`), so the saved approval rule matches:
   `python3 $SESSION_NOTES/bin/codex_save.py commit`
   It waits for the reflect lock if another session holds it, applies the queue in order, and
   releases the lock. A line starting `SKIPPED` means RESUME changed since you read it: re-read,
   merge, stage again, commit again.

## Reporting a refresh

Compare against the memory block the SessionStart hook injected at boot, or the last `$refresh`
in this session. If there was neither (an empty store at boot), everything the refresh finds is
NEW to this session: report each item as new, never "no material changes".

## Paths

| In the shared skill | In Codex |
|---|---|
| the config's stores (`paths.global_store`, a scope's `store`) | `python3 $SESSION_NOTES/bin/notes_config.py --stores` prints this cwd's `global` and `project` stores. `project` is the configured scope holding cwd, else the cwd's git root, else null (global only). |
| `append_memory_index.py`, writing a memory file, editing RESUME.md | `codex_save.py stage` then `codex_save.py commit` (see Writing memory) |
| `reflect_lock.py acquire` / `wait` / `release` | none: `codex_save.py commit` takes, waits for and releases the lock |
| `<paths.claude_home>` | `<paths.agent_home>` (the Codex home, `~/.codex`) |
| CLAUDE.md (global and project) | AGENTS.md: global `~/.codex/AGENTS.md`; project = the AGENTS.md files from the git root down to cwd. Codex stops adding project files once their combined size reaches `project_doc_max_bytes` (32 KiB by default; the global file does not count toward it) and has no `@import`. |
| "loads via `@import` in CLAUDE.md"; "add the import or the store is an orphan" | the Codex SessionStart hook loads both stores' MEMORY.md and RESUME.md at startup, resume and after compaction. No import to add. A file over the hook's budget is named, not loaded, so keep MEMORY.md and RESUME.md short. |
| `$CLAUDE_CODE_SESSION_ID` | `$CODEX_THREAD_ID` (the helpers read it themselves in codex mode) |
| the transcript on disk | this conversation as you see it; Codex's transcript file format is not stable, so never parse it |

## Tools

| In the shared skill | In Codex |
|---|---|
| Read / Edit / Write tools | read with the shell (`cat`, `sed -n`), edit with apply_patch |
| invoke another skill via the Skill tool | do not chain: follow that skill's procedure inline, as the Codex rnr skill spells out |
| a command run backgrounded (`run_in_background: true`) | run it in the foreground. `codex_save.py commit` waits up to 15 minutes for the lock by itself; if it exits 2 (timeout), report the stuck holder and ask. Never ask "wait or go?". |
| `session_peers.py`, `--peek`, ListAgents, SendMessage, the bus | skip: sibling peeks and messages are Claude Code and tmux only for now. Say nothing about siblings. |
| "Skill tool calls" when listing skills used (reflect Step 2b) | skills the user invoked as `$name` this session |
| `boot_budget.py`, `skill_feedback.py canary` | skip them. In codex mode each prints a one-line skip and exits 0, so if one runs anyway, that line is not a finding. |
| `memory_hygiene_canary.py` | run it as written: in codex mode it checks Codex's own stores. |
| `/reflect`, `/refresh`, `/rnr` in prose and replies | `$reflect`, `$refresh`, `$rnr` |
