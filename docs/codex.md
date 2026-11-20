# session-notes in Codex

The memory loop (`$refresh`, `$reflect`, `$rnr` and the boot-time load of the
index and RESUME) runs in OpenAI Codex, in both the CLI and the desktop app.
They share `~/.codex`, so one install covers both.

## Two agents, two memories

Codex keeps its own store under `~/.codex/session-notes/`. It starts empty and
never reads or writes `~/.claude`, and Claude never touches the Codex store.
One `~/.config/session-notes/config.toml` serves both agents.
`SESSION_NOTES_AGENT=codex` (which the install sets for Codex) tells each tool
which side it is on:

| | Claude Code | Codex |
|---|---|---|
| global store | `~/.claude/projects/<home>/memory` | `~/.codex/session-notes/global` |
| project store | a configured scope's store | `~/.codex/session-notes/projects/<encoded root>` |
| project root | the configured scope | the configured scope, else the cwd's git root |
| reflect lock and state | `~/.claude/session-notes/state` | `~/.codex/session-notes/state` |
| session id | `CLAUDE_CODE_SESSION_ID` | `CODEX_THREAD_ID` |
| boot load | `@import` in CLAUDE.md | the SessionStart hook |
| instruction files | CLAUDE.md | AGENTS.md |

In codex mode a resolved store under `paths.claude_home` is a config error, not
a warning. `python3 bin/notes_config.py --stores` prints the stores for the
current folder.

## Install

```sh
sh install.sh --codex --dry-run   # see what it would do
sh install.sh --codex
```

It asks `[y/N]` before each step:

1. Link the three Codex skills into `~/.codex/skills/`.
2. Write `~/.codex/hooks.json` with the SessionStart hook, if the file does not
   exist yet. An existing file is never edited, and the block is printed so you
   can merge it in.
3. Append two tables to `~/.codex/config.toml` (keeping a `.bak-session-notes`
   copy). `shell_environment_policy.set` gives the skills `SESSION_NOTES` and
   `SESSION_NOTES_AGENT`. `sandbox_workspace_write.writable_roots` lets
   `$reflect` write `~/.codex/session-notes` from the default sandbox. If
   either table already exists, the text is printed instead.

Then trust the hook: `/hooks` in the CLI, or Settings > Hooks in the app. Codex
trusts a hook by its hash, so an edited hook needs trusting again.

## How the pieces map

- **Boot load.** AGENTS.md has no `@import`, so `hooks/codex_session_start.py`
  returns the global and project MEMORY.md and RESUME.md as
  `additionalContext` on `startup`, `resume` and `compact`. Codex spills hook
  output over the handler's `additionalContextLimit` to a file and shows only a
  preview. To keep that from happening silently, the hook caps itself at
  `agent.codex_boot_bytes` (12,000 characters) and replaces a file that does
  not fit with a line naming its path. The install sets the limit to 4,000
  tokens. With an empty store the hook prints nothing.
- **Skills.** `skills/codex/<name>/SKILL.md` reads the shared Claude Code skill
  and applies the substitutions in `skills/codex/ADAPTER.md`, so a fix to
  `/reflect` reaches both agents. Codex skills do not call each other, so
  `$rnr` runs the refresh and reflect procedures inline.
- **Reflect lock.** The same lock, keyed on `CODEX_THREAD_ID`. A Codex launched
  from a Claude pane inherits `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID`, and
  codex mode ignores both. The skills wait on the lock in 60-second
  foreground slices, because Codex has no background shell call.

## Limits

- The commands (`handoff_session`, `session_peers`, `bcast`) and the bus are
  Claude Code only, so `$refresh` does not peek at sibling sessions.
- `skill_feedback.py canary` reads Claude Code transcripts and refuses in
  codex mode, because Codex documents its transcript format as unstable.
- The boot load happens once per session, the same as on the Claude side. A
  session that is already open picks up another session's writes with
  `$refresh`.
