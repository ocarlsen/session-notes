# session-notes: the full toolkit

The README covers the default install: two skills and three commands. This page is everything else in the repo, and the reference for the default pieces too. `sh install.sh --all` links all of it.

## The mental model

```
                     every session loads (native CLAUDE.md imports)
  ~/.claude/CLAUDE.md ──@──> global store: MEMORY.md (index) + RESUME.md
                                           + one .md file per memory
  ~/code/app/CLAUDE.md ─@──> app's store:  MEMORY.md + RESUME.md + files
         (loads only in sessions started under ~/code/app)

  /reflect   writes durable lessons to the right store (lock-serialized)
  /refresh   reloads the stores mid-session (imports are read once at boot)
  /rnr       refresh, then reflect

  handoff_session.py --name worker --spec "..."
       lead ──launches──> worker      (parent recorded at startup)
       session_peers.py --tree        lead
                                        worker
       session_peers.py --me          parent: lead (live | exited)

  bcast "pause and post status"  ──>  every Claude pane, prefixed and logged
```

- **Stores.** A store is a folder: `MEMORY.md` is a one-line-per-memory index
  that loads at boot, `RESUME.md` is "where we left off", and each memory is
  its own file with frontmatter, read on demand. An index line for a
  `reference_*.md` file goes to `REFERENCE.md` instead (a cold index that is
  never imported, so lookups cost nothing at boot) unless written with
  `--pin`; search it before using an unfamiliar tool. The global store holds only
  facts that apply everywhere; each project keeps its own, so one project's
  detail never costs boot context in another.
- **RESUME.md** is the only channel from one session to the next. A
  conversation does not carry over; whatever the next session needs to pick
  up has to be written there.
- **Budgets.** Everything imported at boot competes for the same prefix.
  `boot_budget.py` keeps each file under a ceiling, and
  `memory_hygiene_canary.py` checks that index and files agree.

## Talking between sessions: native first, the bus for two jobs

For one session telling another something, use Claude Code's
[native cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging.md)
(v2.1.224 and later). It labels the message as coming from another session,
not from you, and starts a turn on an idle recipient.

`session_bus.py` predates it and stays for two jobs native messaging does
not cover:

- **Staged handoffs.** A native message to an idle session starts a turn.
  A staged spec must not: it waits on the bus until a human types in the
  worker's pane, and that keystroke is the authorization.
- **Mail from outside a session.** A script (a CI watcher, a cron job) can
  call `session_bus.append()` with a tmux session name. On this machine the
  inbox sockets are per process (observed), and the docs give no way for a
  process outside any session to find a session by name. (Claude Code's
  Channels, a research preview, pushes external events into sessions
  launched with it; the bus predates it and addresses sessions by tmux
  name.)

Bus mail lands on the recipient's next prompt, through a hook, fenced and
tagged with the sender. `bus_nudge.py` wakes an idle recipient by typing one
fixed line (never the message) into its empty input box.

## What is in the box

| Piece | Files | What it does |
| --- | --- | --- |
| Config | `bin/notes_config.py`, `config.example.toml` | One TOML file for paths, scopes, boot budgets and time zone. No hardcoded directories. |
| Scoped memory | `skills/reflect`, `skills/rnr`, `skills/refresh`, `skills/find-session`; `bin/reflect_lock.py`, `append_memory_index.py`, `memory_index_lint.py`, `boot_budget.py`, `memory_hygiene_canary.py` | Write, reload and check memory stores. A lock serializes concurrent reflects; the index writer refuses over-long lines; the budget and canary report drift. |
| Skill feedback | `bin/skill_feedback.py`; a ledger `<skill dir>/feedback.jsonl` beside each skill | Logs how the user corrects a skill's own output (edit / reject / approve, quoted verbatim), captured by `/reflect` (Step 2b). `propose` groups unresolved rows by habit and reports which have enough evidence (default: 3+ rows from 2+ sessions) to act on; it never edits a SKILL.md itself. |
| Session coordination | `bin/session_peers.py`, `session_lineage.py`, `handoff_session.py`, `bcast.py`; `hooks/session_pane_registry.py` | List and peek at sibling sessions, launch a worker session and record who launched it, show the lineage tree, broadcast a line you type to every session. |
| Bus | `bin/session_bus.py`, `bus_nudge.py`; `hooks/session_bus_hook.py` | Staged specs and mail from scripts, addressed by tmux session name, delivered at least once; wake idle recipients. |
| Senses | `hooks/time_sense.py` | Stamps the first prompt, and any prompt after a long gap, with the current time; after a long idle it tells the model its context may be stale. |
| Example guard | `hooks/em_dash_guard.py` | A harmless PostToolUse hook showing the pattern: it flags em dashes in text the model just wrote and asks for a rewrite. |

Every script has a `--selftest`; `make test` runs them all with `HOME`
pointed at a temp dir, plus smoke tests that run each hook the way Claude
Code does.

## Skill feedback: skills that improve from being corrected

A skill's output gets edited, rejected, or approved by the user every time it runs; that reaction
is the only signal that says whether the skill is doing the right thing, and it exists only in the
conversation, not in any hook. `/reflect` (Step 2b) reads the transcript for those reactions and
logs each one, verbatim, through `bin/skill_feedback.py add` into `<skill dir>/feedback.jsonl`, one
JSON row per line, tagged with a short `habit` slug naming the pattern it is evidence of.

`bin/skill_feedback.py propose <skill>` groups unresolved rows by habit and reports which ones
have enough evidence (by default: 3+ edit/reject rows from 2+ distinct sessions) to act on, with
exact row citations, the before/after text, any `approve` rows for the same habit as "keep"
examples, and a ready commit trailer. `propose` never writes to SKILL.md: the proposal is HELD for
the model to draft and the user to approve.

`add` and `applied` (the two commands that write) refuse before touching the ledger if its
directory sits inside a git work tree that doesn't already ignore it, naming the work tree and
pointing at `.gitignore` or `--skill-dir`; a real install's skill dir is usually a symlink pointing
INTO this checkout (`install.sh` links `~/.claude/skills/<name>` here), which is exactly the case
this repo's own `/skills/*/feedback.jsonl` rule covers. Even a gitignored ledger inside a clone is
still lost to `git clean -fdx`, so a ledger worth keeping belongs somewhere that command won't
touch.

**Before shipping a proposed edit, run a blind A/B.** Generate the same few paragraphs twice, once
under the current SKILL.md (`git show HEAD:<path>/SKILL.md`) and once under the proposed version,
strip anything that labels which is which, and have the user (or a second model) pick without
knowing. Ship the edit only if the proposed version wins; a habit that only looks better to the
model that proposed it isn't a fix.

Try it against the synthetic example (a tiny made-up `commit-message` skill under the example
claude_home, `home/.claude/skills/` inside `examples/`, not something `install.sh` ever links):

```sh
SESSION_NOTES_CONFIG=examples/config.toml python3 bin/skill_feedback.py propose commit-message
```

That ledger already has one qualifying habit (`no-trailing-summary`: 3 rows across 2 sessions)
and one still-tentative one (`imperative-mood-subject`: 1 row), so the command's two sections
both have something to show.

## Five-minute quickstart

Against the synthetic example store in `examples/`, without touching your own
`~/.claude`:

```sh
git clone https://github.com/ocarlsen/session-notes
cd session-notes
make test                                   # all self-tests + hook smoke tests

cp -R examples /tmp/notes-demo              # play on a copy; the checkout stays clean
export SESSION_NOTES_CONFIG=/tmp/notes-demo/config.toml
python3 bin/notes_config.py                 # the resolved config
python3 bin/boot_budget.py report           # what loads at boot, against budgets
python3 bin/memory_hygiene_canary.py        # silent and exit 0 = store is consistent
python3 bin/append_memory_index.py --scope recipes-app \
  "- [Fractions parse exactly](project_fractions.md) - use Fraction, never float"
python3 bin/memory_hygiene_canary.py        # now flags the entry: no project_fractions.md yet
```

That last flag is the point: the index and the files must agree, and the
canary says so the moment they do not.

Then, inside tmux with a couple of Claude Code sessions open:

```sh
python3 bin/session_peers.py                # roster: session, pane, idle/working, cwd
python3 bin/session_peers.py --peek worker  # visible screen of the worker session
python3 bin/session_peers.py --tree         # who launched whom, children indented under parents
python3 bin/session_peers.py --me           # this session's parent and whether it is still live
python3 bin/handoff_session.py --name worker --spec "add range parsing to parser.py"
python3 bin/bcast.py -n "check the bus"      # dry run: targets and exact keystrokes
```

`handoff_session.py` starts `worker` idle with the spec parked on the bus;
it shows up in `worker`'s context when someone types there. To have idle
recipients of bus mail woken instead, run `python3 bin/bus_nudge.py` in its
own tmux session. A session launched with `handoff_session.py` records its parent at
startup (tmux `-e` env read by the pane-registry hook), which is what `--tree`
and `--me` read; a session started any other way shows as a root. Recording needs
tmux 3.2 or later (`new-session -e`); on an older tmux the launch still works, without a parent.
Broadcasting to every session with `send --all` also needs
`--broadcast-confirm`, on purpose.

## Installing, in full

```sh
sh install.sh --dry-run                     # see what it would do
sh install.sh --all --bin-dir ~/.local/bin  # every skill, and every tool onto PATH
```

`install.sh` only creates symlinks (skills into `~/.claude/skills/`, tools into
the `--bin-dir` you name) and never overwrites a real file. It changes
`settings.json` and `CLAUDE.md` only on a `[y/N]` yes (`bin/setup_claude.py`
does the merge, keeps a `.bak-session-notes` copy, and adds nothing twice);
otherwise it prints both for you to paste. The same yes adds `SESSION_NOTES`
(the checkout, which the skills use to find the tools) to `settings.json`'s `env`. Set
`CLAUDE_CONFIG_DIR` (or `CLAUDE_HOME`) if your Claude Code directory is not
`~/.claude`. Copy
`config.example.toml` to `~/.config/session-notes/config.toml` and list your
project scopes there.

**Uninstall:** remove the symlinks `install.sh` created, by hand, there is no
`--uninstall` flag. For each skill, `rm` `${CLAUDE_HOME:-~/.claude}/skills/<name>`
(one per directory under `skills/`). If you passed `--bin-dir`, `rm` each
linked tool name (`session_peers`, `session_bus`, `bcast`, `bus_nudge`,
`handoff_session`, `reflect_lock`, `append_memory_index`, `memory_index_lint`,
`boot_budget`, `memory_hygiene_canary`, `notes_config`) from that directory.
The hooks and the `CLAUDE.md` lines come out by hand too (the
`.bak-session-notes` copies are the files as they were before the first install): delete every entry under `SessionStart`,
`UserPromptSubmit`, `Stop`, `Notification` and `PostToolUse` that points at
this repo, and the `## Session tools` block from any `CLAUDE.md` that has it. (`make hooks`
is unrelated: it only points this repo's own git at `.githooks` for the
pre-commit leak scan, not Claude Code's hooks.)

### tmux setup

Pane detection needs nothing extra: Claude Code sets its process title to its
version string and its pane title to a glyph plus the session name. Whether a
session is working or idle comes from the hooks block `install.sh` prints (the pane-registry
hook records each turn starting and stopping); the title glyph is only the
fallback. Two optional snippets make it nicer:

```sh
# In your Claude Code statusLine script: stamp the window with the model name.
# session_peers also counts a pane as Claude when this is set.
model=$(printf '%s' "$input" | jq -r '.model.display_name // empty')
[ -n "$TMUX" ] && [ -n "$model" ] && tmux set -w @claude_model "$model" 2>/dev/null
```

```tmux
# ~/.tmux.conf: show each Claude session's name and busy state as the window name.
set -g automatic-rename on
set -g automatic-rename-format "#{pane_title}"
```

Name sessions with `/rename` (or `claude -n NAME`); that name is what
`session_peers` shows and what `--peek` and `bcast --to` match on.
