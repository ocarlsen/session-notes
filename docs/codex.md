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

1. Link the four Codex skills (`$reflect`, `$refresh`, `$rnr`, `$review`) into `~/.codex/skills/`.
2. Write the hook launcher, `~/.local/bin/session-notes`: a small generated script with
   this checkout's path baked in (see Hooks below). It refuses to overwrite a file at that
   path that is not a session-notes launcher.
3. Register the hooks in `~/.codex/hooks.json`. Every command goes through the launcher.
   An existing file is refreshed, not skipped: only session-notes entries are replaced
   (old-form entries from any checkout too, so they do not pile up), and every other entry
   and event is kept. The step shows a diff first and keeps a `.bak-session-notes` copy.
4. Append two tables to `~/.codex/config.toml` (keeping a `.bak-session-notes`
   copy). `shell_environment_policy.set` gives the skills `SESSION_NOTES` and
   `SESSION_NOTES_AGENT`. `sandbox_workspace_write.writable_roots` lets
   `$reflect` write `~/.codex/session-notes` from the CLI's default sandbox.
   If either table already exists, the text is printed instead.
5. Write a marked block (`<!-- session-notes:begin ... -->` to
   `<!-- session-notes:end -->`) into `~/.codex/AGENTS.md`, rewritten on every
   install so an older install gets the newer sections too. It holds
   [`AGENTS.snippet.md`](../AGENTS.snippet.md) (only `$reflect` writes the store;
   without it a "remember this" turn makes Codex write memory files by hand before
   any skill loads), the standing rules `rules/core.md` and
   `rules/review-contract.md`, and an optional private addendum (config key
   `rules.local`). The same step sets a default model and effort in `config.toml`
   only where unset, and offers `build` and `review` profiles (`codex -p build`).
6. Add two lines to `~/.codex/rules/default.rules` that approve exactly one
   command, `python3 <repo>/bin/codex_save.py commit`, so the desktop app saves
   memory without asking (see Saving memory below).

Install from the main checkout, not from a linked git worktree: `install.sh --codex` (and
`codex_hooks.py merge` and `launcher`) refuse a worktree up front, before any step or prompt,
because `wt merge` deletes it.

Then trust the new or changed hooks (next section).

## Hooks

`install.sh --codex` registers these in `~/.codex/hooks.json`. Every command has the form
`SESSION_NOTES_AGENT=codex <launcher> hook <name>`, with the launcher's absolute path (the
desktop app's PATH lacks `~/.local/bin`):

| Event | Hooks |
|---|---|
| SessionStart | `codex_session_start` (boot load), `session_pane_registry` |
| UserPromptSubmit | `time_sense`, `session_bus_hook`, `merge_consent` (records the user's yes to a `merge #N?` question, read from the rollout) |
| Stop | `session_bus_hook` |
| PreToolUse (`Bash`, `apply_patch`) | `checkout_guard`: refuses an edit of a tracked file on the default branch of a main checkout (the patch text is read for its target paths, and a shell write such as a redirect, `tee`, `sed -i`, `cp`/`mv`/`rm` is judged the same way), a raw `gh pr merge`, and destructive git on a dirty tree. A refusal blocks the tool call and the model is shown the reason |
| PostToolUse (`apply_patch`) | `em_dash_guard`, which checks only the lines a patch adds |

**The launcher.** `python3 bin/codex_hooks.py launcher --repo <checkout>` writes the script
(`--path FILE` to put it elsewhere, `--dry-run` to print it). It runs
`<checkout>/hooks/<name>.py` with stdin, environment and arguments passed through;
`session-notes repo` prints the checkout it points at. Rewriting it with the same checkout
changes nothing.

The output shape is Claude's (`hookSpecificOutput.additionalContext`), so the same scripts
serve both. The differences are small: Codex has no Notification event, so there is no
idle-prompt hook; `time_sense.py` tells Codex to run `$refresh` where it tells Claude
`/refresh`; and file edits arrive as `apply_patch` with the patch text.

**Refresh.** Rerunning the install updates the file in place through `bin/codex_hooks.py`.
It replaces only this repo's entries: a command with the `SESSION_NOTES_AGENT=codex` prefix
that runs `<launcher> hook <name>` for one of the hooks above (the launcher may be at any
path), or the older form that ran `python3 <checkout>/hooks/<script>.py` (so entries from a
moved or removed checkout are migrated, not duplicated). Everything else, including your
private hooks, is kept, and nothing is written when nothing changed, so a second run is a
no-op. A file whose `hooks` shape is unexpected is refused untouched.
`python3 bin/codex_hooks.py merge --repo <checkout> --file ~/.codex/hooks.json --dry-run`
shows the diff.

**Trust.** Codex runs a hook only after you trust it, and it trusts the exact hook
definition: a new hook, or any change to an entry (the command, the timeout, a limit),
may need trusting again, and until then the hook is silently skipped. Open `/hooks` in the
CLI, or Settings > Hooks in the desktop app, after every install that changed something.

**Moving or removing a checkout.** hooks.json does not name a checkout, so its bytes are the
same whichever checkout installs: run `install.sh --codex` from the checkout that stays and
the launcher is rewritten and hooks.json is unchanged, so no hook needs trusting again. (A
running Codex session needs no restart either: the launcher is read on each hook run.) The
other things the installer writes still point at the checkout they were installed from: the
skill links, the `SESSION_NOTES` value in config.toml and the lines in `rules/default.rules`.
A later change moves them to the launcher; until then, fix them by hand when you move a
checkout. Until you reinstall, a missing checkout is survivable: an informational hook prints
one warning line and passes, so prompts keep working. A guard hook fails closed with exit 2.
For a PreToolUse guard (`checkout_guard`) that blocks the tool call. For
`em_dash_guard`, which is PostToolUse, the edit has already happened, and the model is told
the check did not run. `codex_hooks.py merge` warns about any entry, ours or not, whose script
or launcher is missing, or whose launcher's checkout lacks the script.

**The checkout guard.** Codex 0.171 sends `checkout_guard` a PreToolUse payload with
`tool_name` `Bash` and `tool_input.command` (a string) for a shell command, and `tool_name`
`apply_patch` with the whole patch text in `tool_input.command`; both carry `cwd`. The guard
exits 2 with one paragraph on stderr, and Codex then refuses the tool call and shows the model
that text ("Command blocked by PreToolUse hook: checkout_guard: ..."). The recorded payloads
(identifiers scrubbed) are `tests/fixtures/codex_pretooluse_*.json`, and the guard's selftest
runs against them. It is a new hook, so trust it once after installing (`/hooks`). To refuse
nothing it should not, exempt repos in the config's `[checkout_guard]` section; see
`docs/tools.md`.

**Known limits of the guard under Codex** (read from the Codex 0.171 source, not observed live):
- `write_stdin` sends no PreToolUse hook: text typed into a shell that is already running was
  judged when that shell started, so a `gh pr merge` or `git reset --hard` typed into it later is
  never shown to the guard.
- Only exit code 2 with text on stderr blocks a call. Any other failure of the hook is a failed
  hook and the call proceeds. In particular a launcher file that is gone makes the shell exit 127
  (the "guard hooks fail closed" rule covers a missing script or checkout behind a working
  launcher, not a missing launcher), and the hook's 10 second timeout is longer than one 8 second
  git call inside the guard, so a very slow `git status` on a large dirty tree can end as a timed-out
  hook and let the call through.
- Shell coverage of `apply_patch` is exactly: the direct `apply_patch` tool, the `applypatch`
  alias, literal `cd <dir> &&` (or `;`) chains in front of it, and `bash -c "..."` with the body
  inline. The patch is judged from BOTH the directory the tool call started in and the directory
  the guard tracks through `cd`, and refused if either refuses (the guard cannot resolve every
  `cd`: `cd "$PWD"`, a subshell, a failed `cd`, `popd`). The cost: `cd <linked worktree> &&
  apply_patch` from a session whose directory is the main checkout is refused, as it was before
  this change. Everything else through the shell is NOT covered, and was not before: `eval`,
  variables, `$'...'`, `printf` or a redirect into `apply_patch`, `xargs`, `cd --` or `cd -P`,
  `builtin cd`, and a heredoc body inside `bash -c "...\nEOF\n"`. Other shell writes
  (`sed -i`, redirects) are not covered either, as under Claude Code.

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

A staged handoff spec (`handoff_session.py` parks it with `send --staged`) is the exception:
`bus_nudge.py` types nothing into that pane until the human's own typed prompt delivers the
spec, so the pane stays idle. That delivery needs the bus hook to run in the pane: a Codex
pane started with `--no-daemon` and with its hooks trusted in `/hooks`. In the default daemon
mode, or with an untrusted hook, the typed prompt delivers nothing and the pull cannot show
the spec either, so the spec stays unoffered and the pane stays held. Other mail to that
session waits behind the spec, and everything expires after 24h; `bus_nudge` says so in its
HOLDING line. A spec parked by a plain `send` is not protected.

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
  inside it. Approve the send when Codex asks, or let `environment_render.py apply --target
  codex` add the allow rule for `<bin>/session_bus.py send` (see Auto-review below). That rule
  also covers `send --all --broadcast-confirm`, which reaches every session at once.

To let the read-only session tools run without an approval prompt each time, add rules
like these to `~/.codex/rules/default.rules` (replace `<bin>` with the directory holding
`session_bus.py`, as an absolute path). Nothing that types into a pane is allowed:
`send-keys` still asks, and `session_bus.py send` is allowed only by the marked rule that
`environment_render.py apply --target codex` writes.

```
prefix_rule(pattern=["tmux", ["list-sessions","list-panes","display-message"]], decision="allow")
prefix_rule(pattern=["tmux", "capture-pane", "-p"], decision="allow")
prefix_rule(pattern=["python3", "<bin>/session_bus.py", ["inbox","peers"]], decision="allow")
```

## Auto-review

Codex can route approval prompts to an automatic reviewer instead of you. Its `[auto_review]
policy` value is Markdown that REPLACES the whole bundled reviewer policy ("omitted rules drop
out of the replacement"; a blank value is ignored), so it cannot hold just your environment.
`bin/environment_render.py` builds the full policy: the bundled one, byte for byte, with the
entries of your environment file added at the end of its `## Environment Profile` section.

- **Source:** one private Markdown file (`environment.source`, default `environment.md` beside
  the config; start from `templates/environment.md`). The same file becomes Claude Code's
  `autoMode.environment`. Each non-blank line outside HTML comments is one entry.
- **Pinned policy:** `templates/codex-auto-review-policy.md` is Codex's
  `codex-rs/prompts/templates/guardian/policy.md` at tag `rust-v0.171.0` (Apache-2.0, OpenAI;
  see `NOTICE`). The script holds the tag and sha256 as constants and refuses to run if the
  file's hash differs.
- **What `apply --target codex` writes:** one marked block in `<codex_home>/config.toml`,
  placed after the leading top-level keys and before the first `[table]`:
  `sandbox_mode = "workspace-write"`, `approval_policy = "on-request"`,
  `approvals_reviewer = "auto_review"` and `auto_review.policy = """..."""`. It refuses, naming
  the line, if the file already sets any of those keys (or has an `[auto_review]` table)
  outside the block; resolve that by hand. It also keeps one marked block in
  `<codex_home>/rules/default.rules` with `prefix_rule(pattern=["python3",
  "<bin>/session_bus.py", "send"], decision="allow")` for this repo's `bin` (and
  `<claude_home>/bin` when it resolves to the same file). Lines outside the blocks are never
  touched. Every write takes a `.bak-environment-<timestamp>` copy first.
- **Worktrees:** `apply --target codex` refuses when this repo's `bin` is in a linked git
  worktree, because the allow rules would name a directory that is deleted when its branch
  merges; apply from the main checkout. `--allow-worktree` overrides that, for testing a branch.
  `diff` and `check` always run. Only the newest three `.bak-environment-*` copies of each file
  are kept.
- **Day to day:** `environment_render.py diff` shows what would change (silent when equal),
  `apply` asks and writes. `nightly_check.py` runs the same comparison for each installed
  target and reports drift.
- **Updating the pin:** `environment_render.py upstream` fetches the bundled policy at the
  installed Codex version's tag and reports when it differs from the vendored copy; the
  nightly check runs it too. To move to a newer policy: copy the new
  `policy.md` over `templates/codex-auto-review-policy.md`, review the diff, then set
  `POLICY_TAG` and `POLICY_SHA256` in `bin/environment_render.py` (`shasum -a 256` the file)
  and `apply` again.

## How the pieces map

- **Boot load.** AGENTS.md has no `@import`, so `hooks/codex_session_start.py`
  returns the global and project MEMORY.md and RESUME.md as
  `additionalContext` on `startup`, `resume` and `compact`. Codex spills hook
  output over the handler's `additionalContextLimit` to a file and shows only a
  preview. To keep that from happening silently, the hook caps itself at
  `agent.codex_boot_bytes` (12,000 characters) and replaces a file that does
  not fit with a line naming its path. The install sets the limit to 4,000
  tokens. After the memory files it adds the live-context block (`bin/live.py`, what
  other sessions just did) from whatever is left of that cap. With an empty store
  and no live entries the hook prints nothing. The live file sits outside both agent homes
  (`~/.local/state/session-notes/` by default), so Codex never reads or writes `~/.claude`.
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
2. `commit` is always the same text, so the rule from step 6 of the install
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
  sibling sessions. `handoff_session.py --cli codex` (the default when `SESSION_NOTES_AGENT=codex`)
  starts the new pane as `codex --no-daemon`; `--bypass` becomes
  `--dangerously-bypass-approvals-and-sandbox`, `--model` becomes `-m`, and `--codex-arg=ARG`
  passes any other Codex flag. A staged handoff (`--spec`) parks the spec on the bus and the
  pane stays idle; the UserPromptSubmit hook delivers it on the first prompt typed there, so
  the hooks must be trusted. There is no `$handoff` skill: Claude Code has none either, the
  command is the interface.
- Bus mail rides the prompt hook only when the hook runs inside the pane (`codex --no-daemon`,
  or `codex features disable daemon_auto_start`, which sets it under `[features]` in
  `config.toml`); otherwise it is pulled.
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
- Without the rule from install step 6, the desktop app asks once per save,
  for `codex_save.py commit`. "Allow similar commands" on that prompt saves
  the same rule.
- The boot load happens once per session, the same as on the Claude side. A
  session that is already open picks up another session's writes with
  `$refresh`.
