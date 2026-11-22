# session-notes: the full toolkit

The README covers the default install: three skills and four commands (`handoff_session`, `session_peers`, `bcast` and `wt`). This page is everything else in the repo, and the reference for the default pieces too. `sh install.sh --all` links all of it.

## The mental model

```
                     every session loads (native CLAUDE.md imports)
  ~/.claude/CLAUDE.md ──@──> global store: MEMORY.md (index) + RESUME.md
                                           + one .md file per memory
  ~/code/app/CLAUDE.md ─@──> app's store:  RESUME.md (auto memory loads MEMORY.md) + files
         (loads only in sessions started under ~/code/app)

  /reflect   writes durable lessons to the right store (lock-serialized)
  /refresh   reloads the stores mid-session (imports are read once at boot)
  /rnr       refresh, then reflect

  handoff_session.py --name worker --spec "..."
       lead ──launches──> worker      (parent recorded at startup)
       session_peers.py --tree        lead
                                        worker
       session_peers.py --me          parent: lead (live | exited)

  bcast "pause and post status"  ──>  every agent pane, prefixed and logged
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
  `nightly_check.py` runs both for every installed agent (Claude, Codex or
  both, each against its own stores); on macOS,
  `launch_agents.py install nightly-check` runs it every night.
  A check that finds a file it cannot read (a macOS privacy block under
  launchd, malformed settings JSON) reports `BLIND` and withholds the verdict
  that depended on it, instead of guessing. In Codex mode the canary also warns
  when the files the SessionStart hook loads reach 90% of
  `agent.codex_boot_bytes`. Each run records itself in `paths.last_check`;
  `nightly_check.py status [--max-age-days 8]` prints one line from that record
  (nothing when clean and fresh; loud when crashed, stale or never run).

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
| Skill feedback | `bin/skill_feedback.py`; a ledger `<claude_home>/skill-feedback/<skill>.jsonl` per skill | Logs how the user corrects a skill's own output (edit / reject / approve, quoted verbatim), captured by `/reflect` (Step 2b). `propose` groups unresolved rows by habit and reports which have enough evidence (default: 3+ rows from 2+ sessions) to act on; it never edits a SKILL.md itself. `canary` prints the derived "N skills used, M rows logged" line. |
| Session coordination | `bin/session_peers.py`, `session_lineage.py`, `handoff_session.py`, `bcast.py`; `hooks/session_pane_registry.py` | List and peek at sibling sessions, launch a worker session and record who launched it, show the lineage tree, broadcast a line you type to every session. |
| Bus | `bin/session_bus.py`, `bus_nudge.py`; `hooks/session_bus_hook.py` | Staged specs and mail from scripts, addressed by tmux session name, delivered at least once; wake idle recipients. |
| Worktrees | `bin/wt.py`, `githooks/pre-commit` | A worktree per change, a PR that merges only on a review verdict, and a staged-content pre-commit. See the `wt` section below. |
| Standing rules | `rules/core.md`, `rules/review-contract.md`, `bin/rules_block.py` | The rules every session follows, for both agents. `rules_block.py apply` writes them, with `AGENTS.snippet.md` and an optional private addendum (`rules.local` in the config), into a marked block in `~/.codex/AGENTS.md` that every `install.sh --codex` rewrites (a file with an older install of the snippet is migrated, and anything outside the markers is kept); `claude-apply` adds `@import` lines to `~/.claude/CLAUDE.md`. `codex-defaults` and `profiles` set Codex's model and effort where unset and create `build` and `review` profiles. |
| Senses | `hooks/time_sense.py` | Stamps the first prompt, and any prompt after a long gap, with the current time; after a long idle it tells the model its context may be stale. |
| Example guard | `hooks/em_dash_guard.py` | A harmless PostToolUse hook showing the pattern: it flags em dashes in text the model just wrote and asks for a rewrite. |

Every script has a `--selftest`; `make test` runs them all with `HOME`
pointed at a temp dir, plus smoke tests that run each hook the way Claude
Code does.

## wt: a worktree per change, and a PR you cannot merge unreviewed

`bin/wt.py` works in any git repo (run it anywhere inside one, a worktree included). One change
gets its own branch and worktree off `origin/<default>`, so parallel sessions never share a
working tree.

| Command | What it does |
| --- | --- |
| `wt.py new <name>` | Fetch, then `git worktree add <path> -b <name> origin/<default>`; prints the path. On a repo's first use it installs the pre-commit hook (below). |
| `wt.py pr [--title T] [--body B] [--dry-run]` | From a worktree on a non-default branch: refuses a dirty tree (exit 4) and a branch that already has a MERGED PR (exit 5), then pushes and opens the PR. `--dry-run` prints the push and create commands and runs nothing. wt never auto-commits. |
| `wt.py merge [name] [--dry-run]` | Refuses a dirty or unpushed worktree (4), runs the verdict gate (6), then squash-merges (pinned to the gated head with `--match-head-commit`, so a later push cannot slip in), removes the worktree and its local branch (kept, with a note, if it gained work since the gate), and fast-forwards the main checkout if it is on the default branch and clean. |
| `wt.py list` | The repo's worktrees: dirty, commits ahead of origin, PR state (`?` offline). A worktree whose directory was deleted by hand shows as its own `missing` row; if it is really gone, `wt.py rm <name> --force` removes just that one record (without `--force` it is refused, exit 4). |
| `wt.py rm <name> [--force]` | Removes a worktree and branch; refuses dirty work, or commits reachable from HEAD that are on no remote branch (a detached HEAD included), without `--force`. |
| `wt.py gc [--yes]` | Lists worktrees whose PR is merged or closed, or whose remote branch is gone. Skips, and says why, any that is dirty or holds commits on no remote branch. Asks y/N per item on a terminal; `--yes` removes all listed; without a terminal and without `--yes` it only lists. A worktree whose directory is gone is only listed (never removed, even with `--yes`): it may sit on an unmounted volume. Run it by hand, never from a scheduled job. |
| `wt.py install-hooks [repo] [--dry-run]` | Installs or repairs the pre-commit (below); `--dry-run` writes nothing (`sh install.sh --dry-run --git-hooks <repo>` does the same). |

Exit codes: 1 usage or fatal, 2 not in a worktree, 3 token command failed, 4 dirty or unpushed,
5 branch already merged, 6 verdict gate refused.

**Where a worktree goes**, first rule that applies: (1) the deepest `[[scopes]]` entry that
contains the repo and sets `worktree_root` (relative to the scope root, or absolute):
`<worktree_root>/<name>`; (2) `[wt].root`: `<root>/<repo name>/<name>`; (3) the repo tracks a
`CLAUDE.md` at its root: a hidden sibling, `<parent>/.<repo name>-worktrees/<name>`, because a
worktree nested inside the repo would also load the main checkout's `CLAUDE.md`; (4)
`<repo>/.worktrees/<name>`, with `/.worktrees/` added once to `.git/info/exclude`.

**Config** (`[wt]`, defaults in `config.example.toml`): `token_command` (shell command whose
output is a GitHub token; exported as `GH_TOKEN` to every `gh` call; failing or empty is exit 3,
never a silent fallback), `root`, `public_repos`, `size_cap_kb`, `leak_terms`. Every `gh` call
runs the executable in `$WT_GH` (default `gh`).

**The verdict gate.** A review is a PR comment whose first line is exactly

```
Review verdict: APPROVE <40-hex head sha>
```

or `Review verdict: CHANGES <sha>`. `merge` reads the PR's comments and the token's own login and
considers only that login's comments, so a stranger commenting on a public repo cannot approve.
The newest verdict comment must be APPROVE and its sha must equal the PR's current head, so a
push after the approval voids it. It refuses, saying which condition failed: no verdict, newest
is CHANGES, approved sha differs from the head, or only other authors' verdicts exist.
A verdict comment that was edited after posting (its `updated_at` differs from `created_at`)
is not trusted: the gate refuses, and you post a fresh verdict comment and delete the edited one.
`parse_verdict` and `verdict_line` in `wt.py` are the one definition of the format.

**What the gate does and does not protect.** It stops other GitHub accounts from approving. It
does not stop you: one login can author the code and post the APPROVE, and `$WT_GH` and
`token_command` are trusted local configuration, so anyone who can change them can bypass it.

**Ignored files go too.** `rm`, `gc` and `merge` remove the worktree directory, including
gitignored files in it (a local `.env`, build output). Copy out anything you want to keep first.

**The pre-commit** (`githooks/pre-commit`) checks only what is staged: API-key shapes (the
pattern from `tests/leak_scan.py`) and PEM private-key headers, in every staged added, copied, modified, renamed or type-changed file; any file over `size_cap_kb`; and,
only for repos listed in `public_repos`, the private terms in `leak_terms` (a missing terms file
fails the commit). A failed `git diff` or a staged blob that cannot be read fails the commit too. It prints each finding with its file and exits 1.
`wt.py install-hooks` (or `sh install.sh --git-hooks <repo>`) writes a small shim to
`<git-dir>/hooks/pre-commit`. An existing hook is never replaced: it is moved to
`pre-commit.pre-session-notes` and the shim runs it after the checks pass (the install refuses
if that name is already taken). The shim records the path of the checkout's `githooks/pre-commit`; if that file
is gone it prints the missing path and the repair command and refuses the commit (it never skips
the checks silently). Re-running `install-hooks` is idempotent when the recorded path is current,
and rewrites the shim in place ("repaired") when the path changed or vanished, keeping the chain.
A `pre-commit` that is a dangling symlink is refused with a message and left alone. If the repo sets `core.hooksPath`,
nothing is written and the command says so; wt never sets or changes `core.hooksPath`.

## Skill feedback: skills that improve from being corrected

A skill's output gets edited, rejected, or approved by the user every time it runs; that reaction
is the only signal that says whether the skill is doing the right thing, and it exists only in the
conversation, not in any hook. `/reflect` (Step 2b) reads the transcript for those reactions and
logs each one, verbatim, through `bin/skill_feedback.py add` into
`<claude_home>/skill-feedback/<skill>.jsonl`, one JSON row per line, tagged with a short `habit`
slug naming the pattern it is evidence of. The ledger is USER DATA, so it lives under the Claude
home like the rest of a user's state, never inside a skill's own folder: a skill folder may be a
symlink into a checkout (`install.sh` makes `~/.claude/skills/<name>` exactly that), and a ledger
written through one would land inside whoever's git repo that checkout happens to be.

`bin/skill_feedback.py propose <skill>` groups unresolved rows by habit and reports which ones
have enough evidence (by default: 3+ edit/reject rows from 2+ distinct sessions) to act on, with
exact row citations, the before/after text, any `approve` rows for the same habit as "keep"
examples, and a ready commit trailer. `propose` never writes to SKILL.md: the proposal is HELD for
the model to draft and the user to approve.

The threshold waits for a habit to repeat, but one case can't wait: the user edited or
rejected text the SKILL.md itself still quotes as an example. The newer reaction wins, so
`propose` lists those rows first, under "contradicts the skill", with no threshold (a match
is the row's `before`, 20+ characters, found in SKILL.md with whitespace collapsed). Fixing
it may mean keeping the quote as a "don't" example; either way, mark the row `applied` with
the fixing commit so it stops being flagged. `--skill-dir` points the check at a skill
outside `<claude_home>/skills/`.

`bin/skill_feedback.py canary` prints `skill feedback: N skills used, M rows logged`: N is the
count of distinct skills the session invoked, by Skill tool call or by a user-typed `/<skill>`
(a typed name counts when it names an installed skill or a plugin `ns:skill`, so built-ins like
`/clear` do not), excluding `reflect`/`rnr`, the step running the capture. M is this session's row count summed across every ledger under
`skill-feedback/`. `/reflect` Step 2b runs it and pastes the line verbatim, so the count is never
eyeballed. If a session id matches transcripts in more than one project folder, it uses the
newest and names it on stderr. `add` refuses `reflect`/`rnr` for the same reason N excludes them.

**Before shipping a proposed edit, run a blind A/B.** Generate the same few paragraphs twice, once
under the current SKILL.md (`git show HEAD:<path>/SKILL.md`) and once under the proposed version,
strip anything that labels which is which, and have the user (or a second model) pick without
knowing. Ship the edit only if the proposed version wins; a habit that only looks better to the
model that proposed it isn't a fix.

Try it against the synthetic example (a tiny made-up `commit-message` skill; its SKILL.md lives
under the example claude_home's `skills/`, its ledger under that same claude_home's
`skill-feedback/`, both inside `examples/`, neither something `install.sh` ever links):

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
recipients of bus mail woken instead, keep `bin/bus_nudge.py` running: on macOS
`python3 bin/launch_agents.py install bus-nudge` starts it at login and restarts it
if it exits; or run it in its own tmux session. Only one loop runs at a time. A session launched with `handoff_session.py` records its parent at
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
