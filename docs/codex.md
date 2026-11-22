# session-notes in Codex

The memory loop (`$refresh`, `$reflect`, `$rnr` and the boot-time load of the
index and RESUME) runs in OpenAI Codex, in both the CLI and the desktop app.
They share `~/.codex`, so one install covers both. Codex also gets the per-prompt hooks
Claude Code gets (time sense, the pane registry, bus mail, the em-dash guard); see Hooks.

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
2. Register the hooks in `~/.codex/hooks.json` (see Hooks below). An existing file is
   refreshed, not skipped: only the entries from this checkout are replaced, and every
   other entry and event is kept. The step shows a diff first and keeps a
   `.bak-session-notes` copy.
3. Append two tables to `~/.codex/config.toml` (keeping a `.bak-session-notes`
   copy). `shell_environment_policy.set` gives the skills `SESSION_NOTES` and
   `SESSION_NOTES_AGENT`. `sandbox_workspace_write.writable_roots` lets
   `$reflect` write `~/.codex/session-notes` from the CLI's default sandbox.
   If either table already exists, the text is printed instead.
4. Add [`AGENTS.snippet.md`](../AGENTS.snippet.md) to `~/.codex/AGENTS.md`. It tells
   Codex that only `$reflect` writes the store. Without it, a "remember this"
   turn makes Codex write memory files by hand before any skill loads.
5. Add two lines to `~/.codex/rules/default.rules` that approve exactly one
   command, `python3 <repo>/bin/codex_save.py commit`, so the desktop app saves
   memory without asking (see Saving memory below).

Then trust the new or changed hooks (next section).

## Hooks

`install.sh --codex` registers these in `~/.codex/hooks.json` (every command is prefixed
`SESSION_NOTES_AGENT=codex`):

| Event | Hooks |
|---|---|
| SessionStart | `codex_session_start.py` (boot load), `session_pane_registry.py` |
| UserPromptSubmit | `time_sense.py`, `session_bus_hook.py`, `session_pane_registry.py` |
| Stop | `session_bus_hook.py`, `session_pane_registry.py` |
| PostToolUse (`apply_patch`) | `em_dash_guard.py`, which checks only the lines a patch adds |

The output shape is Claude's (`hookSpecificOutput.additionalContext`), so the same scripts
serve both. The differences are small: Codex has no Notification event, so there is no
idle-prompt hook; `time_sense.py` tells Codex to run `$refresh` where it tells Claude
`/refresh`; and file edits arrive as `apply_patch` with the patch text.

**Refresh.** Rerunning the install updates the file in place through `bin/codex_hooks.py`.
It replaces only this repo's entries: a command with the `SESSION_NOTES_AGENT=codex` prefix
that runs one of the five scripts above from a `hooks/<script>` path (so entries from a moved
or removed checkout are cleaned up too). Everything else, including your private hooks, is
kept, and nothing is written when nothing changed, so a second run is a no-op. A file whose
`hooks` shape is unexpected is refused untouched. `python3 bin/codex_hooks.py merge --repo <checkout> --file
~/.codex/hooks.json --dry-run` shows the diff.

**Trust.** Codex runs a hook only after you trust it, and it trusts the exact hook
definition: a new hook, or any change to an entry (the command, the timeout, a limit),
may need trusting again, and until then the hook is silently skipped. Open `/hooks` in the
CLI, or Settings > Hooks in the desktop app, after every install that changed something.

## Sessions and the bus

With the hooks trusted and Codex started as `codex --no-daemon` (or with the shared daemon
turned off in config), bus mail arrives the way it does in Claude Code: `bus_nudge.py` types
its one fixed line into an idle pane, and the pane's UserPromptSubmit hook injects the mail,
fenced and attributed to its sender, on that prompt. Hooks run as children of the TUI only
in that mode, so they see `TMUX_PANE` and can say which pane they are in. `bus_nudge` checks
this per pane (a `--no-daemon` codex in the pane's process tree, or a pane-registry entry the
hook wrote from inside the pane). If a pane was nudged and still shows the mail unoffered on
a later tick, its hook did not run (usually an untrusted hook), and the next nudge is the
pull line below.

Without it (default daemon mode, where hooks run in the shared background process with no
`TMUX_PANE`), the fallback is the pull: `bus_nudge.py` types a different fixed line asking
the pane to run `session_bus.py inbox --nudged --as <session>:<created>`, and the mail
arrives as that command's output, again fenced and attributed. The loop reads the address
from tmux, because a Codex shell often cannot: the sandbox refuses the tmux socket, and a
resumed session (`codex resume`) runs commands in the daemon.

The desktop app has no tmux pane, so the bus does not reach it. The time-sense, pane-registry
(a no-op without tmux) and em-dash hooks do run there once trusted.

`bcast` reaches Codex panes too, but only when the input box is empty and idle (never an
approval prompt, a running turn or typed text). A bare slash command such as `/refresh`
goes to Claude panes only: Codex rejects one it does not know and leaves it typed in the box.

Sending FROM Codex works when Codex is started with `codex --no-daemon`, and each send is
approved to run outside the sandbox. `send` names the sender by asking tmux about its own
pane, so it needs both:
- **`--no-daemon`:** by default every Codex session runs its commands in one shared
  background process (`codex app-server`) that may have started outside tmux, so a command
  has no `TMUX_PANE` and cannot say which session is sending. With `--no-daemon`, commands
  run from the pane and keep it.
- **Approval outside the sandbox:** the sandbox refuses the tmux socket, so the lookup fails
  inside it. Approve the send when Codex asks, or add an allow rule for
  `<bin>/session_bus.py send` if you want Codex to send without asking. That rule also
  covers `send --all --broadcast-confirm`, which reaches every session at once.

To let the read-only session tools run without an approval prompt each time, add rules
like these to `~/.codex/rules/default.rules` (replace `<bin>` with the directory holding
`session_bus.py`, as an absolute path). Nothing that types into a pane is allowed:
`send-keys` and `session_bus.py send` still ask.

```
prefix_rule(pattern=["tmux", ["list-sessions","list-panes","display-message"]], decision="allow")
prefix_rule(pattern=["tmux", "capture-pane", "-p"], decision="allow")
prefix_rule(pattern=["python3", "<bin>/session_bus.py", ["inbox","peers"]], decision="allow")
```

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
  codex mode ignores both. `codex_save.py commit` takes it, waits up to 15
  minutes for another holder, and releases it.

## Saving memory

The desktop app gives each chat its own sandbox: writable in the project
folder, a scratch folder and `/tmp`, and nothing else. It ignores
`writable_roots` in `config.toml`, so any write to `~/.codex/session-notes` is
an approval prompt. A saved approval rule matches only an exact command line,
because the app runs every command as `/bin/zsh -lc "<command>"`.

So the skills save in two steps with `bin/codex_save.py`:

1. `stage` queues each change (a memory file, an index line, or a new
   RESUME.md) in `/tmp/session-notes-<uid>/<thread>/`. It writes only `/tmp`,
   so it runs in the sandbox without asking.
2. `commit` is always the same text, so the rule from step 5 of the install
   approves it. It takes the lock, applies the queue through the same helpers
   as before (`append_memory_index.py` for index lines), and releases the lock.

A RESUME change carries the hash of the file the model read
(`codex_save.py hash`). If another session wrote RESUME.md in between, commit
skips that change and says to re-read and stage again, instead of overwriting.
A command with anything in front of `commit` (a `cd`, an env prefix) does not
match the rule and falls back to an approval prompt.

## Limits

- The commands (`handoff_session`, `session_peers`, `bcast`) are Claude Code's; Codex can
  run them from its shell with approval (see above), but `$refresh` does not peek at
  sibling sessions.
- Bus mail rides the prompt hook only when the hook runs inside the pane (`codex --no-daemon`,
  or `daemon_auto_start = false` in `config.toml`); otherwise it is pulled.
  The desktop app has no pane, so it receives no bus mail.
- An untrusted hook does not run and says nothing. If time stamps or bus mail never appear,
  check `/hooks` first.
- `memory_hygiene_canary.py` checks Codex's own stores in codex mode (index and
  files agree, frontmatter, index size). Its `@import` checks are Claude Code
  only and are skipped. A store Codex has not saved to yet is not reported.
- Two checks are Claude Code only and print a one-line skip in codex mode:
  `boot_budget.py` (it measures Claude Code's boot prefix; the Codex hook caps
  its own output instead, and Codex has no budget check yet) and
  `skill_feedback.py canary` (Codex documents its transcript format as unstable).
- `nightly_check.py` runs the canary once per installed agent, each against its
  own stores, plus `boot_budget.py check` for Claude. On macOS,
  `launch_agents.py install nightly-check` runs it every night.
- Without the rule from install step 5, the desktop app asks once per save,
  for `codex_save.py commit`. "Allow similar commands" on that prompt saves
  the same rule.
- The boot load happens once per session, the same as on the Claude side. A
  session that is already open picks up another session's writes with
  `$refresh`.
