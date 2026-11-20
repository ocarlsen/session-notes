# Codex substitutions (shared by the Codex refresh, reflect and rnr skills)

The Codex skills run the SAME procedure as the Claude Code skills in `skills/<name>/SKILL.md`
(one source of truth, so a fix there reaches both agents). Read that file and follow it, with
every substitution below applied. Where a substitution says "skip", skip the step silently.

`$SESSION_NOTES` below means the checkout path each Codex SKILL.md resolves first, from its own
symlink. Do not rely on the environment variable alone: a session started from Codex's agent
command center can run without it. Every helper call gets `SESSION_NOTES_AGENT=codex` in front,
or the helpers resolve Claude's stores. Never run a `bin/` script from the current folder.

## Two memories, never one

Codex keeps its OWN store under `~/.codex/session-notes/`. Never read, write, grep or list
anything under `~/.claude` (or the configured `paths.claude_home`), even to compare: the user
keeps the two agents' memories separate on purpose. The helpers refuse a Codex path under
the Claude home; do not work around that.

## Writing memory

Only the reflect procedure writes the store. When the user says "remember this" outside
`$reflect`, say it will be saved at the next `$reflect` (or run `$reflect` if they ask), and do
not write store files by hand: a hand-written MEMORY.md line skips the index helper's checks.
New index lines go through `append_memory_index.py`, never an editor or a heredoc.

## Reporting a refresh

Compare against the memory block the SessionStart hook injected at boot, or the last `$refresh`
in this session. If there was neither (an empty store at boot), everything the refresh finds is
NEW to this session: report each item as new, never "no material changes".

## Paths

| In the shared skill | In Codex |
|---|---|
| the config's stores (`paths.global_store`, a scope's `store`) | `python3 $SESSION_NOTES/bin/notes_config.py --stores` prints this cwd's `global` and `project` stores. `project` is the configured scope holding cwd, else the cwd's git root, else null (global only). |
| `--scope NAME` for append_memory_index.py | pass the index path instead: `append_memory_index.py <project or global store>/MEMORY.md "<line>"` |
| `<paths.claude_home>` | `<paths.agent_home>` (the Codex home, `~/.codex`) |
| CLAUDE.md (global and project) | AGENTS.md: global `~/.codex/AGENTS.md`; project = the AGENTS.md files from the git root down to cwd. Codex caps their combined size at 32 KiB and has no `@import`. |
| "loads via `@import` in CLAUDE.md"; "add the import or the store is an orphan" | the Codex SessionStart hook loads both stores' MEMORY.md and RESUME.md at startup, resume and after compaction. No import to add. A file over the hook's budget is named, not loaded, so keep MEMORY.md and RESUME.md short. |
| `$CLAUDE_CODE_SESSION_ID` | `$CODEX_THREAD_ID` (the helpers read it themselves in codex mode) |
| the transcript on disk | this conversation as you see it; Codex's transcript file format is not stable, so never parse it |

## Tools

| In the shared skill | In Codex |
|---|---|
| Read / Edit / Write tools | read with the shell (`cat`, `sed -n`), edit with apply_patch |
| invoke another skill via the Skill tool | do not chain: follow that skill's procedure inline, as the Codex rnr skill spells out |
| a command run backgrounded (`run_in_background: true`) | run it in the foreground in short slices: `reflect_lock.py wait --timeout 60`, repeated up to 15 times while it exits 2, saying once whose lock you are waiting on. Still never ask "wait or go?". After 15 slices, report a stuck holder and ask. |
| `session_peers.py`, `--peek`, ListAgents, SendMessage, the bus | skip: sibling peeks and messages are Claude Code and tmux only for now. Say nothing about siblings. |
| "Skill tool calls" when listing skills used (reflect Step 2b) | skills the user invoked as `$name` this session |
| `boot_budget.py`, `memory_hygiene_canary.py`, `skill_feedback.py canary` | skip them. In codex mode each prints a one-line skip and exits 0, so if one runs anyway, that line is not a finding. |
| `/reflect`, `/refresh`, `/rnr` in prose and replies | `$reflect`, `$refresh`, `$rnr` |
