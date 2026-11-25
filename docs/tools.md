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
  worker's pane, and that keystroke is the authorization. `handoff_session.py`
  parks the spec with `session_bus.py send --staged`, and `bus_nudge.py` leaves a
  pane alone while a staged spec for it has zero offers; the human's first typed
  prompt runs the hook, delivers the spec and releases the pane. Known limits: the spec is delivered only if the bus hook runs in
  that pane (for Codex: `--no-daemon` and hooks trusted in `/hooks`); otherwise the
  spec stays held, other mail to that session waits behind it, and all of it
  expires after 24h (`bus_nudge` prints a HOLDING line saying so). A spec parked
  with a plain `send` (a manual staged-go) is not protected, and a running
  `bus_nudge` LaunchAgent keeps the old code until it is restarted.
- **Mail from outside a session.** A script (a CI watcher, a cron job) can
  call `session_bus.append()` with a tmux session name. On this machine the
  inbox sockets are per process (observed), and the docs give no way for a
  process outside any session to find a session by name. (Claude Code's
  Channels, a research preview, pushes external events into sessions
  launched with it; the bus predates it and addresses sessions by tmux
  name.)

Bus mail lands on the recipient's next prompt, through a hook, fenced and
tagged with the sender. `bus_nudge.py` wakes an idle recipient by typing one
fixed line (never the message) into its empty input box. Codex panes started with
`codex --no-daemon` work the same way; otherwise the nudge asks the pane to pull its inbox
([docs/codex.md](codex.md)).

## What is in the box

| Piece | Files | What it does |
| --- | --- | --- |
| Config | `bin/notes_config.py`, `config.example.toml` | One TOML file for paths, scopes, boot budgets and time zone. No hardcoded directories. |
| Scoped memory | `skills/reflect`, `skills/rnr`, `skills/refresh`, `skills/find-session`; `bin/reflect_lock.py`, `append_memory_index.py`, `memory_index_lint.py`, `boot_budget.py`, `memory_hygiene_canary.py` | Write, reload and check memory stores. A lock serializes concurrent reflects; the index writer refuses over-long lines; the budget and canary report drift. |
| Skill feedback | `bin/skill_feedback.py`; a ledger `<claude_home>/skill-feedback/<skill>.jsonl` per skill | Logs how the user corrects a skill's own output (edit / reject / approve, quoted verbatim), captured by `/reflect` (Step 2b). `propose` groups unresolved rows by habit and reports which have enough evidence (default: 3+ rows from 2+ sessions) to act on; it never edits a SKILL.md itself. `canary` prints the derived "N skills used, M rows logged" line. |
| Session coordination | `bin/session_peers.py`, `session_lineage.py`, `handoff_session.py`, `bcast.py`; `hooks/session_pane_registry.py` | List and peek at sibling sessions, launch a worker session and record who launched it, show the lineage tree, broadcast a line you type to every session. |
| Bus | `bin/session_bus.py`, `bus_nudge.py`; `hooks/session_bus_hook.py` | Staged specs and mail from scripts, addressed by tmux session name, delivered at least once; wake idle recipients. |
| Worktrees | `bin/wt.py`, `githooks/pre-commit`, `hooks/merge_consent.py` | A worktree per change, a PR that merges only on a review verdict, green CI and the user's yes, and a staged-content pre-commit. See the `wt` section below. |
| Review | `bin/review.py`, `bin/test_edit_check.py`, `skills/review` | Prepares a fresh-context reviewer's prompt for a PR and posts its verdict as the comment `wt merge` reads. See the `review` section below. |
| Environment | `bin/environment_render.py`, `templates/environment.md`, `templates/codex-auto-review-policy.md` | One private Markdown file describing your environment, rendered into Claude Code's `autoMode.environment` and Codex's `[auto_review] policy` (the pinned bundled policy with your entries inserted). `diff` shows the change, `apply` backs up and writes, `check` (run by `nightly_check.py`) reports drift, `upstream` reports when Codex's bundled policy changed. `apply --target codex` also allows `session_bus.py send` in `default.rules`. See `docs/codex.md`. |
| Standing rules | `rules/core.md`, `rules/review-contract.md`, `bin/rules_block.py` | The rules every session follows, for both agents. `rules_block.py apply` writes them, with `AGENTS.snippet.md` and an optional private addendum (`rules.local` in the config), into a marked block in `~/.codex/AGENTS.md` that every `install.sh --codex` rewrites (a file with an older install of the snippet is migrated, and anything outside the markers is kept; each change keeps a timestamped backup, and only the newest 3 are kept); `claude-apply` adds `@import` lines to `~/.claude/CLAUDE.md`. `codex-defaults` and `profiles` set Codex's model and effort where unset and create `build` and `review` profiles. |
| Live context | `bin/live.py` | One machine-wide rolling file of what just happened across scopes, loaded at session start. See the `live` section below. |
| Senses | `hooks/time_sense.py` | Stamps the first prompt, and any prompt after a long gap, with the current time; after a long idle it tells the model its context may be stale. |
| Checkout guard | `hooks/checkout_guard.py` | A PreToolUse guard that keeps edits out of a repo's main checkout, blocks raw `gh pr merge`, and stops destructive git on a dirty tree. See the `checkout_guard` section below. |
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
| `wt.py merge [name] [--dry-run]` | Refuses a dirty or unpushed worktree (4), runs the verdict gate (6), the CI gate (8) and the user-consent gate (7), then squash-merges (pinned to the gated head with `--match-head-commit`, so a later push cannot slip in), removes the worktree and its local branch (kept, with a note, if it gained work since the gate), and fast-forwards the main checkout if it is on the default branch and clean. |
| `wt.py list` | The repo's worktrees: dirty, commits ahead of origin, PR state (`?` offline). A worktree whose directory was deleted by hand shows as its own `missing` row; if it is really gone, `wt.py rm <name> --force` removes just that one record (without `--force` it is refused, exit 4). |
| `wt.py rm <name> [--force]` | Removes a worktree and branch; refuses dirty work, or commits reachable from HEAD that are on no remote branch (a detached HEAD included), without `--force`. |
| `wt.py gc [--yes]` | Lists worktrees whose PR is merged or closed, or whose remote branch is gone. Skips, and says why, any that is dirty or holds commits on no remote branch. Asks y/N per item on a terminal; `--yes` removes all listed; without a terminal and without `--yes` it only lists. A worktree whose directory is gone is only listed (never removed, even with `--yes`): it may sit on an unmounted volume. Run it by hand, never from a scheduled job. |
| `wt.py install-hooks [repo] [--dry-run]` | Installs or repairs the pre-commit (below); `--dry-run` writes nothing (`sh install.sh --dry-run --git-hooks <repo>` does the same). |

Exit codes: 1 usage or fatal, 2 not in a worktree, 3 token command failed, 4 dirty or unpushed,
5 branch already merged, 6 verdict gate refused, 7 no user consent, 8 CI gate refused.

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

**The CI gate.** After the verdict, `merge` reads the head sha's check runs and commit statuses.
Any failing or still-running check refuses (8) and is named; no checks at all proceeds. A check
that is also red on the base branch still blocks. A pending check is refused, not waited on:
rerun when CI finishes.

**The consent gate.** An APPROVE comment is something any session can post, so it does not
authorize a merge. The session ends its reply with a final sentence that IS the ask:
`merge #N?`, optionally after a short lead-in ending in `:` or followed by ` in owner/repo`, with
no other `#N` and no negation in it. The user answers a bare yes (yes, y, yep, yeah, ok, go, do
it, merge it) or types `merge #N`. The `hooks/merge_consent.py` UserPromptSubmit hook (Claude Code
and Codex) records that human prompt in `~/.claude/session-notes/state/merge-consent/grants.json`
(the passwd home of the user and a fixed path: `$HOME`, `SESSION_NOTES_CONFIG` and the configured
state dir do not move it): repo, PR number, time, the prompt verbatim, the session. A yes counts
only when the previous reply's final sentence is that ask and no human prompt came after the
reply (a "no, rebase first" in between voids it); the previous reply is read from the Claude
transcript or the Codex rollout. The first prompt of a session, bus nudges and bcast lines grant
nothing. The repo comes from the cwd's `origin`, else the one `owner/repo` or PR URL the reply
names, else the one github checkout under a configured scope root; a reply that names a repo
other than the cwd's grants nothing; with none, `merge` refuses the grant and says to ask from
inside the repo or name it (`merge #N in owner/repo?`).

`merge` then requires an unexpired (30 minutes), unused grant for that repo and PR, and a newest
APPROVE verdict whose GitHub `created_at` is EARLIER than the yes (a push after the yes needs a
new review, then a new ask). It spends the grant (recording the head sha), posts
`Merge authorized by the user: "<prompt>" (<time>)` on the PR, and merges; with no live grant it
exits 7 telling the session to ask the user. `checkout_guard` refuses the ordinary ways an agent
could write the grant directory (an edit, a redirect or `tee` naming the path, moving a parent
directory, a hard link) and refuses to run or import the hook or to call the merge API directly
(`gh api` PUT `pulls/N/merge`, a `mergePullRequest` mutation).

**What the gate does and does not protect.** The verdict gate stops other GitHub accounts from
approving. It does not stop you: one login can author the code and post the APPROVE, and
`$WT_GH` and `token_command` are trusted local configuration, so anyone who can change them can
bypass it. The consent gate stops a session that FOLLOWS its refusals. It does not stop one that
deliberately forges: any process running as the same user can write the grant file through an
interpreter (`python3 -c`, `node -e`, `rsync`, a variable-built path) and `checkout_guard` is a
command parser, not a sandbox. Known limits, left open on purpose:

- The guard judges the path a command names. It follows no variable or directory change reliably:
  `cd "$HOME/.claude/session-notes/state/merge-consent" && echo ... > grants.json`, and `mkdir -p`
  of that directory followed by a `cd` into it and a relative redirect, both write the file.
- A session can type into its own pane (`tmux send-keys`), set `CLAUDE_CODE_ENTRYPOINT` before a
  headless run, or start `claude --resume <id> "merge #N"` or `codex exec resume <id> "merge #N"`;
  each presents its text as a human prompt to the hook, which cannot tell it from the user's.
- A grant is usable by any session on the machine, not only the one the user answered.
- A yes is tied to the PR, the repo and the time, not to the code: the APPROVE-before-yes check
  catches a push after the yes only when a new APPROVE is posted, and `--match-head-commit` pins
  the merge to the head the gates saw.

## review: the reviewer's prompt, and the verdict comment

`bin/review.py` implements `rules/review-contract.md`. Run it inside a checkout of the PR's repo;
it reuses `wt.py`'s GitHub access (`[wt].token_command`, `$WT_GH`) and its verdict format.

| Command | What it does |
| --- | --- |
| `review.py prepare <PR>` | Fetches the PR's head sha, title, body and diff, runs the test-edit check on the diff, and writes `prompt.md` and `meta.json` under `<state_dir>/review/<owner>-<repo>-<PR>/`. The prompt has the test-edit output first, then the contract, the reviewer's instructions, the PR title and body as the spec, the repo path and head sha, and the diff (a file beside the prompt when over about 200 KB). From round 2 on it also runs the test-edit check on the range since the last reviewed head (the sha in the newest own verdict comment at another sha; no own verdict, no section), under a "Since the last reviewed head" sub-heading, and names that prior verdict comment's URL; if git cannot produce the range it warns and says so there. Prints the prompt path, the head sha and the review round; says so, without failing, when this is the last review the contract's 2 fix rounds allow or the limit is passed. |
| `review.py post <PR> APPROVE\|CHANGES --body-file F [--dry-run]` | Posts the reviewer's reply as `Review verdict: <V> <sha>` plus the body. Refuses when `prepare` was never run, when the PR head moved since `prepare` (exit 4), when the body is empty, when its last non-empty line is not exactly `VERDICT: APPROVE` or `VERDICT: CHANGES`, or when that line disagrees with the argument. A CHANGES that brings the account's CHANGES verdicts past 2 prints a stop-and-report line. `--dry-run` posts nothing. After a post it calls `on_verdict_posted`, which records a short note through `bin/live.py` when that file exists and does nothing otherwise. |

Exit codes: 1 fatal, a refusal or a selftest failure, 2 usage error (argparse), 3 token command failed, 4 the head moved since `prepare`. A PR diff that is not UTF-8 is a clear exit 1. `test_edit_check.py` decodes a non-UTF-8 diff (file or stdin) with replacement and a warning.
The `/review` skill (and `$review` in Codex) is the procedure: checks and rebase, `prepare`, a
reviewer sub-agent at high effort, `post`, then ask the user `merge #N?` (name `owner/repo`
when not inside the checkout) and, on their yes, `wt merge`.

`bin/test_edit_check.py` reads a unified diff on stdin (or `--diff-file`) and reports every
removed or changed line in a test, fixture or guard file, every deleted test file, and every
removed assertion in any Python file. Test-shaped files are the built-in path rules (`tests/`,
`test_*.py`, `*_test.py`, `*.spec.ts`, `fixtures/`, `evals/`, canary and leak-term names) plus
the fnmatch globs in `[review].test_globs`. It also reports a test file that was renamed (even to a
non-test path), a changed binary fixture, and removed lines inside a multi-line `check(` call (a
heuristic that counts parentheses within the hunk, so it can miss a call whose strings contain parentheses). Paths git quotes (non-ASCII names) are decoded. It only reports: exit 0 with or without hits,
1 for a malformed config value (the message names the key), 2 for a usage error.

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

## checkout_guard: keep work out of the main checkout

`hooks/checkout_guard.py` is a PreToolUse hook (installed by default for Claude Code, matcher
`Edit|Write|MultiEdit|NotebookEdit|Bash`, and for Codex by `install.sh --codex`). It reads the tool payload and either exits 0 (allow,
silent) or exits 2 with one paragraph on stderr (refuse; the model sees the reason).

**Arm A, edits.** `Edit`, `Write`, `MultiEdit`, `NotebookEdit` and Codex `apply_patch` are
refused when the target is a tracked file and its repo's MAIN checkout is on the default branch
(`origin/HEAD`'s branch if set, else `main`, or `master` when that is what is checked out). The
message says to run `wt new <name>` and edit there. Allowed: a new untracked file, anything in a
linked worktree (whatever its branch), a main checkout on another branch or a detached HEAD, and
paths outside a git repo. An `apply_patch` is refused if any file it touches qualifies. Shell
writes are judged the same way: a redirect (`>`, `>>`, `>|`, `>!`, `&>`), `tee`, `sed -i` / `perl -i`
(their file operands, not the script), the destination of `cp` / `install` / `ln` (a directory
destination means `<dir>/<each source name>`), `mv`, `rm`, `truncate`, `unlink` and `dd of=`,
also inside `bash -c`, `eval` and the wrappers `env` (`-C` too), `timeout`, `nice`, `nohup`,
`time`, `command`, `builtin`, `noglob`, `exec`, `sudo`. Glob operands are expanded the way the
shell does. After a `cd` to a literal, existing directory (also `pushd`, `cd -` and a `cd` in a
subshell, which ends with the subshell) relative paths are judged from there only, so `cd
<worktree> && edit` works; when the guard cannot follow a `cd` (variables, command
substitution, a missing directory, `popd`) it judges from the payload directory too and refuses
if either refuses. Writes outside a repo, to untracked files, in linked worktrees and to exempt
paths pass.

**Arm B, shell.** Refused: any raw `gh pr merge` invocation except `--help` (use `wt merge`, which
checks the review verdict); and, only when it would destroy uncommitted work, `git checkout -f`,
`git checkout [<ref>] -- <paths>`, `git checkout <path>`, `git checkout .`, `git restore`
(a plain `--staged` is fine), `git switch -f`, `git reset --hard`, and `git clean` with force and
`-d` (not `-X` alone, not a dry run). `git -C <dir>` and a leading `cd <dir> &&` pick the tree.
Only an invocation counts: the command is parsed (heredoc bodies dropped, env prefixes, shell
keywords such as `if`/`do`/`!` and wrappers such as `sudo` or `env` skipped), so
`echo "gh pr merge"` or a commit message that mentions it does not trip it. Redirections
(`2>&1`, `> /dev/null`, `&>f`, `2> err`, attached or detached) are removed before any argument
is read, so they are never mistaken for a path operand.

**One predicate for path forms.** For `git checkout` and `git restore` with paths, the hook runs
`git status --porcelain --untracked-files=no -- <operands>` once and lets git do the glob,
directory and `.` matching. A plain checkout or restore is allowed only if git succeeded and
printed nothing. Conflict resolution (`--ours`, `--theirs`, `--merge`, and `-m` for checkout) is
allowed only if git succeeded, printed something, and every line is an unmerged code (`UU`, `AA`,
`DD`, `AU`, `UA`, `DU`, `UD`), so a pathspec that also matches a merely modified file is refused.
Operands exclude commit-ish names and the values of `-b`, `-B` and `--orphan`, so
`git checkout -b docs` and `git checkout main --` are branch operations, not path checkouts.
Whole-tree verbs (`checkout -f`, `switch -f`, `reset --hard`, `clean`) ask whether the tree is
dirty. A repo with no `origin` remote is never refused by Arm A, since there is no PR flow and
`wt new` needs one.

**Fail safe on git errors.** Inside Arm B, a `git status` that fails in a repo (a pathspec outside
the repository, a corrupt index) counts as dirty and refuses; it is never read as clean. Outside
any repo there is nothing to lose and the command passes.

**The config file is protected.** The guard's own config file (the path `notes_config` resolves,
a symlink and its target alike) cannot be changed by the agent. Edit, Write, MultiEdit,
NotebookEdit and `apply_patch` on it are refused, and so are shell commands that write it:
a redirect target, `tee`, `sed -i` or `perl -i`, `cp`/`install`/`ln` onto it, `mv` from or onto it,
`rm`/`truncate`/`unlink` of it, `dd of=`. Reads (`cat`, `grep`, `cp` out of it) pass. This is
checked before the config is even loaded and before `exempt_repos` and `exempt_paths`, so a
config that fails to parse cannot make itself editable and no exemption can cover it. Ask the
user to edit it.

**Exemptions** are config only, in `[checkout_guard]`: `exempt_repos` (repo roots where the guard
does nothing, both arms) and `exempt_paths` (glob patterns Arm A allows). Every configured scope's
memory store is always exempt for edits. There is no environment variable, flag or command-line
comment that turns the guard off.

**Fail open.** An unreadable payload, an internal error or an import error exits 0 with one
`checkout_guard: <error>` line on stderr. A config file that fails to parse also fails open (the
line names the file and says the guard is off for that call), except that the file itself stays
protected.

**Known limits.** Arm A judges only the listed commands, invoked directly or through
`bash -c`, `eval` or the listed wrappers. It does not see a program that writes on its own
(`python3 script.py`, `python -c`, `make`), nor `xargs`, a heredoc fed to a shell, `$'...'`
quoting, `gsed`, `ruby -i`, `curl -o`, `rsync`, `patch` or `git apply`. Also not seen: brace
expansion (`mv README.md{,.bak}`), BSD `sed -I`, `awk -i inplace`, and a `cd` that never runs or
only runs in a subshell or condition (`false && cd x; rm README.md`, `(cd x); rm README.md`): the
guard follows the first literal `cd` it reads. Removing or moving a checkout's top-level
directory (an `rm` operand or an `mv` source, as in `rm -rf <clone>`) is allowed; as any other
write target (`cp -R <dir>/. .`) it is judged as before; naming or globbing files inside it is still judged. A
refusal for a relative path that was judged from the payload directory because a `cd` could not
be followed says so; give an absolute path or run the `cd` as its own tool call.

**Codex.** `install.sh --codex` registers the hook on PreToolUse for the tool names `Bash` and
`apply_patch`, the names and payload shapes recorded from Codex 0.171
(`tests/fixtures/codex_pretooluse_*.json`; see `docs/codex.md`). Codex honours exit 2 and
refuses the tool call. The other names in `SHELL_TOOLS` are kept for other Codex builds.

## live: what just happened, across scopes

Memory stores hold what a scope decided and are read at boot; they do not tell a session
what another session did ten minutes ago. `bin/live.py` keeps one short rolling file for that:
`~/.local/state/session-notes/live-context.md` (key `live.path`). Claude Code and Codex share it. It lives outside both agent homes because both
agents write it and Codex must never touch `~/.claude`; `install.sh --codex` adds its directory to
Codex's `writable_roots` (`notes_config.py --live-path` prints the resolved path).

```
# Live context (newest first; written by live.py, do not hand-edit)

- 2026-11-21 14:03 PST [recipes-app] merged PR #12: add search (https://github.com/...)
- 2026-11-21 13:40 PST [finance] decided to keep the old ledger format
```

One line per event, stamped in `[time].timezone`, tagged with the scope name (a folder outside
every scope is tagged with its git root's name, else `unscoped`). Only `live.max_lines` (30)
entries are kept, and one entry's text is at most `live.max_chars` (240) characters: a longer one
is refused, never truncated. Writes take an flock and replace the file whole, and a symlinked file
is updated in place. A hand-added non-entry line makes `add` refuse rather than rewrite the file.

Writers:
- `wt.py merge` adds `merged PR #N: title (url)` after a successful merge (a failure there only
  warns; the merge result is unchanged).
- `/reflect` and `$reflect` record each decision other sessions should know, with
  `live.py note -` (text on stdin).
- Any tool can `import live; live.add(scope_or_cwd, "text")`. Planned call points: the review
  verdict, ledger add/close, reminders add.

Readers: the Claude `SessionStart` hook (`live.py hook`, added to the hooks `install.sh` offers),
the Codex start hook (inside `agent.codex_boot_bytes`, after the memory files, which keep
priority), and `/refresh` and `$refresh` (`live.py show`). Startup output is silent when the
file is empty.

Isolation. A scope in the config sets `live`:

| `live` | Reads | Writes |
| --- | --- | --- |
| `"shared"` (default) | the shared file | the shared file |
| `"own"` | only its `live_file` | only its `live_file` |
| `"none"` | nothing | nothing (`live.py note` exits 3) |

A `<file>.lock` sits beside each live file (the flock target), so an own `live_file` inside a repo
should be gitignored along with its lock, or placed outside the tree. A linked git worktree
counts as its main checkout's scope, wherever it lives (even under another scope's folder). If git
cannot answer cleanly (missing, hung, an error other than "not a git repository", or output it
cannot parse), the folder is treated as isolated, even inside a shared scope: nothing is read and
`note` refuses. Limit: a worktree of a submodule (its git dir is not named `.git`) is matched by
its own path, not its superproject's. `live_file` may not be the shared file,
and two own scopes may not share one. `live_file` is required with `"own"` and refused otherwise; a relative path is relative to the scope
root. See `config.example.toml` for a `finance` scope with its own file and a `work-client` scope
that is fully isolated.

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
it shows up in `worker`'s context when someone types there. `--cli codex` launches
`codex --no-daemon` instead of Claude Code (default: codex when `SESSION_NOTES_AGENT=codex`,
else claude), with the same naming, lineage, staged and auto-start modes and `--dry-run`:

| handoff flag | Claude pane | Codex pane |
|---|---|---|
| `--model M` | `--model M` | `-m M` |
| `--bypass` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` (no approvals and no sandbox) |
| `--claude-bin` / `--codex-bin` | the Claude binary | `--claude-bin` is refused; use `--codex-bin` |
| `--codex-arg=ARG` (repeatable) | refused | passed to `codex` verbatim, before the prompt |
| session name | `claude -n <name>` | none; the tmux session name is the address |

A staged Codex pane needs the session-notes hooks trusted (`/hooks`); the parked spec arrives
on the first prompt typed in the pane. To have idle
recipients of bus mail woken instead (plain bus mail only: a staged spec is never woken,
only a typed prompt releases it), keep `bin/bus_nudge.py` running: on macOS
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
`handoff_session`, `wt`, `reflect_lock`, `append_memory_index`, `memory_index_lint`,
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

## selftest-live: real Claude Code and Codex sessions in a sandbox

`make check` runs unit self-tests; none of them starts a real CLI. `make selftest-live` does:
it clones this checkout's committed HEAD into a throwaway directory, installs it the way a new
user would (`install.sh`, then `install.sh --codex`), launches real sessions, and prints a
pass/fail table (feature x CLI): boot context and clock line, bus mail, peers, bcast, handoff,
refresh, reflect, the em-dash and checkout guards, the `wt` verdict gate, that every registered
hook path exists, live context (one CLI's note reaches a fresh session of the other), the review
skill (a verdict comment on a stubbed PR that `wt merge` accepts), the rendered environment (`claude
auto-mode config`, Codex `/status`), and that nothing the run wrote reached the real `~/.claude`, `~/.codex` and
session-notes dirs (no install target gained, lost or changed content referencing the sandbox;
files changed by other live sessions are listed, not failed). It catches what
unit tests cannot, such as a `hooks.json` naming scripts in a deleted worktree.

It costs API credits (Claude `--model haiku`, Codex `gpt-6.2-sol` at low effort, tiny prompts)
and is not part of `make check`. Setup: store one Anthropic and one OpenAI API key in the macOS
Keychain (services `session-notes-selftest` and `session-notes-selftest-openai`, overridable with
`SELFTEST_CLAUDE_KEYCHAIN_SERVICE` and `SELFTEST_OPENAI_KEYCHAIN_SERVICE`). A CLI with no key
has its rows reported SKIP (a key that Codex rejects is a FAIL). Claude gets the key through an
`apiKeyHelper` in the sandbox settings and the sandbox `CLAUDE_CONFIG_DIR` keeps the seat login out of
reach; the runner does not assert that a session never used it. The pane models can technically read the test keys (the helper command in the sandbox settings, the Codex sandbox `auth.json`), so use dedicated keys with a spend cap. Flags: `--only claude|codex`,
`--feature boot,bus,...`, `--keep` (keep the sandbox; a run with a FAIL keeps it anyway, with the
evidence files under `logs/`). `python3 tests/live/selftest_live.py --selftest` checks the runner's
own logic (hook-path check, table, real-dir guard, the `wt` gate, each with a red control) without
launching anything.

Limits worth knowing: Codex runs with `--dangerously-bypass-hook-trust`, so hook trust is not
covered; tmux is reached only through a sandbox `TMUX_TMPDIR` (asserted before any pane-listing
tool runs); `launchctl` and `gh` are stubs; `em_dash_guard` is advisory, so its row checks that the
flag reached the session, not that the file was refused. The `checkout` row checks both CLIs across two turns and three refusals: an edit-tool write to a tracked file, a shell write (`printf >>`) to a second tracked file, and a raw `gh pr merge`, each refused with the guard's own text, the files unchanged and no `pr merge` call reaching the `gh` stub (the stub wins on a login shell's `PATH` too); for Codex a new file in the same repo is still allowed (negative control). The Codex `handoff` row also runs one `bus_nudge.py --once --only` pass over the staged pane and checks it prints HOLDING and types nothing; the Claude handoff row is auto-start only and carries no hold check.

### Watch mode: `make selftest-watch`

The same run, built to be watched and filmed (`python3 tests/live/selftest_live.py --watch`; extra
options through `ARGS`, e.g. `make selftest-watch ARGS="--pace 2 --step"`). One tmux session
`watch` in the sandbox server, window fixed at 180x50 (`window-size manual`, so attaching or
recording never resizes it): a 3-row status pane on top, then the Claude session under test on the
left (89x46) and the Codex one on the right (90x46), each a read-only nested attach
(`tmux attach -r -f ignore-size`) of the real session. The side follows the step's focus session.
Attach from another terminal with

    tmux -L sn-watch attach

(a symlink named `sn-watch` in your default tmux socket directory, `$TMUX_TMPDIR` or `/tmp`
followed by `tmux-<uid>/`, points at the sandbox socket; a sandbox `attach` alias lands on the
`watch` window. The run refuses to start if `sn-watch` already exists there, and removes the link
in the same teardown as everything else, on every exit path). Needs a terminal of at least 180x50.

Steps that run as headless one-shots in CI mode (boot, reflect, em dash, checkout) run as
interactive panes here, with the same checks, and carry "(pane)" in the final table.

Flags: `--pace X` scales the holds (default 1: about 2 s on each chapter title before the step and
4 s on its result after; 0 = none); `--step` holds each step until Enter is pressed in the status
pane; `p` pauses and resumes; `q` aborts with full teardown; `--leak-terms FILE` adds the terms of a
leak-terms file to the screen guard, read with the same syntax as `tests/leak_scan.py` (`w:` whole
word entries, `!` allowed phrases, `#` comments); `--no-hold-final` tears down when the run ends instead of waiting for
`q` with the final table in the status pane. The status pane only forwards keys (through a FIFO
under the sandbox); it never types into another pane.

Nothing private on screen, because the run is public. The panes get only sandbox directories on
`PATH` (tmux, git, python3 and the CLIs are linked into the sandbox `bin/`), the user name `user`,
a plain `$ ` prompt, and no tmux status line or hostname titles. The Claude `apiKeyHelper` names no
keychain path: a sandbox `home/Library/Keychains/login.keychain-db` symlink to the real login
keychain lets `security find-generic-password -s <service> -w` work under the temp HOME (without
the link it exits 44). A screen guard captures every visible pane (rows joined across the pane edge, and also read with
the row breaks removed, so a path that wraps is still seen) about once a second while the runner
waits, and at each step transition. It looks for the real home path, the user name (both taken from
the passwd entry of the real user as well as `$HOME` and `$USER`), the short and full hostname, key
shaped strings (`sk-` plus 20 characters) and, with `--leak-terms`, each term. It does not scan
while the runner is inside a blocking call (bcast, handoff, the `wt` row, review PR setup,
`environment_render apply`: up to two minutes), only before and after it. `--watch` refuses to
start when a class has nothing to look for. A hit blanks the watch panes and aborts the run
(teardown runs; a trip in the final hold exits nonzero); the message names the pane and the class
of hit, never the matched text. A hit can be on screen for up to a second before it is seen.

#### Status file

The runner writes `status.json` in the sandbox root (the path is printed at start), rewritten
atomically at each transition, so a storyboard can time chapter cards off it:

    {"step": 3, "total": 16, "id": "bcast", "cli": "claude",
     "title": "Broadcast: one line reaches every pane",
     "claude": "working", "codex": "idle",
     "phase": "running", "result": "", "paused": false,
     "pass": 2, "fail": 0, "skip": 1, "t": 1795404391}

`phase` is `title` (the chapter title is up, before the step runs), `running`, `result` (the step
finished; `result` is PASS, FAIL or SKIP), `done` (the final table, in `table`, is up until `q`) or
`aborted` (screen guard, `q`, Ctrl-C or any other abort; `note` says why; once written the file is
final). `id` is the feature id and `cli` is `claude`,
`codex` or `both`; `claude` and `codex` are `idle`, `working`, `pass`, `fail` or `skip`; `step` is 0
during setup and `total` counts every step including the last one (`realdirs`). Chapter titles
have no dashes, at most 8 words and no internal jargon; the table in `CHAPTERS` in
`tests/live/selftest_live.py` is the list.
