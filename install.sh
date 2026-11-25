#!/bin/sh
# install.sh -- link session-notes into a Claude Code setup.
#
# What it does:
#   - symlinks the /reflect, /refresh and /rnr skills into ~/.claude/skills/<name>
#   - optionally symlinks the four commands (handoff_session, session_peers,
#     bcast, wt) into a directory on your PATH
#   - in a terminal, it asks [y/N] before each of those, and before each edit below
#   - offers, one [y/N] question each, to add its hooks (and SESSION_NOTES, which the
#     skills use to find the tools) to ~/.claude/settings.json, and the lines that tell
#     Claude the commands exist to ~/.claude/CLAUDE.md
#     (it shows what it would add and keeps a .bak-session-notes copy first);
#     not in a terminal, or with --dry-run, it prints both for you to paste
#   - with --all: every skill in skills/, every tool, and the optional hooks
#   - offers the standing rules (rules/core.md, plus an optional private addendum): in
#     CLAUDE.md as @import lines; for Codex as a marked block in ~/.codex/AGENTS.md that
#     every install rewrites (bin/rules_block.py)
#   - with --codex, writes one small launcher (~/.local/bin/session-notes) that every Codex
#     hook runs through, so hooks.json never names a checkout; refuses to run from a linked
#     git worktree (install from the main checkout)
#   - with --codex, also sets Codex's default model and reasoning effort when unset, and
#     offers build and review profiles (codex -p build, codex -p review)
#   - offers a blank copy of the environment file (templates/environment.md) that describes your
#     setup to both agents' auto-approval reviewers; it never applies it, `bin/environment_render.py
#     apply` does, after showing a diff
#   - on macOS, offers two background jobs: the nightly memory checks and the bus_nudge loop
#
# What it never does: change settings.json or a CLAUDE.md without a yes, touch
# your memory files, or overwrite anything that is not already a symlink it created.
#
#   sh install.sh                    ask about the links, then about the two edits
#   sh install.sh --bin-dir ~/.local/bin
#   sh install.sh --all --bin-dir ~/.local/bin
#   sh install.sh --yes              answer yes to every question
#   sh install.sh --dry-run          show what would happen
#   sh install.sh --git-hooks <repo>  install the session-notes pre-commit into a git repo
#                                    (chains any hook already there; see docs/tools.md, wt)
#   sh install.sh --codex --bin-dir ~/.local/bin
#                                    set up OpenAI Codex instead (docs/codex.md): link the
#                                    Codex skills into ~/.codex/skills, link the commands
#                                    into --bin-dir, write the hook
#                                    launcher, refresh hooks.json and config.toml edits,
#                                    the rules block, the model defaults and profiles;
#                                    never touches ~/.claude
set -eu

REPO=$(cd "$(dirname "$0")" && pwd)
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME/.claude}}"
SKILLS_DIR="$CLAUDE_DIR/skills"
BIN_DIR=""
DRY=0
ALL=0
YES=0
CODEX=0
GITHOOKS=""
GITHOOKS_SET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --dry-run|-n) DRY=1; shift ;;
    --all) ALL=1; shift ;;
    --yes|-y) YES=1; shift ;;
    --git-hooks) [ $# -ge 2 ] || { echo "install.sh: --git-hooks needs a repo path" >&2; exit 2; }
                 [ -n "$2" ] || { echo "install.sh: --git-hooks needs a non-empty repo path" >&2; exit 2; }
                 GITHOOKS="$2"; GITHOOKS_SET=1; shift 2 ;;
    --codex) CODEX=1; shift ;;
    -h|--help) sed -n '2,44p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [ "$GITHOOKS_SET" = 1 ]; then
  if [ "$DRY" = 1 ]; then exec python3 "$REPO/bin/wt.py" install-hooks --dry-run "$GITHOOKS"; fi
  exec python3 "$REPO/bin/wt.py" install-hooks "$GITHOOKS"
fi

# A linked git worktree is deleted when its change merges: never install Codex hooks from one.
# The check lives in codex_hooks.py (one source of truth); it prints the message and exits 1.
if [ "$CODEX" = 1 ]; then
  python3 "$REPO/bin/codex_hooks.py" check-repo --repo "$REPO" || exit 1
fi

# A path under $HOME prints as ~/...: shorter, and the hook commands, which run in a shell,
# still resolve if the home directory's absolute path changes.
tilde() { case "$1" in "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }
SHOWN=$(tilde "$REPO")

run() { if [ "$DRY" = 1 ]; then echo "would: $*"; else "$@"; fi; }

link() {  # link SRC DEST: create or refresh a symlink, never clobber a real file
  src=$1 dest=$2
  if [ -L "$dest" ]; then
    case "$(readlink "$dest")" in
      "$REPO"/*) run ln -sfn "$src" "$dest" ;;
      *) echo "skip: $dest is a symlink to somewhere else (remove it to install)" >&2; return 0 ;;
    esac
  elif [ -e "$dest" ]; then
    echo "skip: $dest exists and is not a symlink (move it aside to install)" >&2
    return 0
  else
    run ln -s "$src" "$dest"
  fi
  [ "$DRY" = 1 ] || echo "linked $(tilde "$dest")"
}

# The default surface. The skills call every other tool by its path under
# $SESSION_NOTES/bin, so nothing they need has to be on PATH.
SKILLS="reflect refresh rnr"
TOOLS="handoff_session session_peers bcast wt"
TOOLS_ALL="$TOOLS session_bus bus_nudge reflect_lock append_memory_index memory_index_lint \
         boot_budget memory_hygiene_canary nightly_check launch_agents notes_config"
if [ "$ALL" = 1 ]; then
  # skills/codex/ holds the Codex variants (install.sh --codex), not a Claude skill.
  SKILLS=$(for d in "$REPO"/skills/*/; do n=$(basename "$d"); [ "$n" = codex ] || echo "$n"; done)
  TOOLS="$TOOLS_ALL"
fi
# Codex has no Claude install behind it, so --codex links the full command set.
if [ "$CODEX" = 1 ]; then TOOLS="$TOOLS_ALL"; fi

# In a terminal (or with --yes) every step asks first; otherwise links are made as before and
# the two edits are printed for a by-hand paste.
ASK=0
if [ "$DRY" = 0 ] && { [ "$YES" = 1 ] || { [ -t 0 ] && [ -t 1 ]; }; }; then ASK=1; fi

ask() {  # ask QUESTION: 0 on a yes. --yes answers yes; no answer means no.
  if [ "$YES" = 1 ]; then echo "$1 [y/N] y"; return 0; fi
  printf '%s [y/N] ' "$1"
  read -r answer || answer=""
  case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

count() { set -- $1; echo $#; }

# link_commands: symlink each command in $TOOLS into $BIN_DIR (one step, one question), then say
# if $BIN_DIR is not on PATH. The one place both installs (Claude and --codex) link commands.
link_commands() {
  [ -n "$BIN_DIR" ] || return 0
  if [ "$ASK" = 1 ]; then echo; echo "Commands to link into $(tilde "$BIN_DIR"): $TOOLS"; fi
  if [ "$ASK" = 0 ] || ask "Link these $(count "$TOOLS") commands?"; then
    run mkdir -p "$BIN_DIR"
    for tool in $TOOLS; do
      [ -f "$REPO/bin/$tool.py" ] && link "$REPO/bin/$tool.py" "$BIN_DIR/$tool"
    done
    case ":$PATH:" in
      *":$BIN_DIR:"*) ;;
      *) echo "Note: $(tilde "$BIN_DIR") is not on your PATH; add it (for example export PATH=\"$(tilde "$BIN_DIR"):\$PATH\" in your shell profile) so the commands resolve." ;;
    esac
  else
    echo "Skipped the commands."
  fi
}

# shq WORD: WORD quoted so it pastes back into a shell, spaces and all.
shq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# offer QUESTION ARGS...: run `rules_block.py ARGS --dry-run`, show it, and (when asking)
# apply it on a yes. Not asking: print the command to run. "unchanged" needs no question.
# Warnings go to stderr and print as they come.
offer() {
  q=$1; shift
  if ! out=$(python3 "$REPO/bin/rules_block.py" "$@" --dry-run); then
    echo "$out"; echo "Skipped (see above)."; return 0
  fi
  echo "$out"
  if printf '%s\n' "$out" | grep -q '^unchanged:'; then return 0; fi
  if [ "$ASK" = 1 ]; then
    if ask "$q"; then python3 "$REPO/bin/rules_block.py" "$@" || echo "Failed (see above)."
    else echo "Skipped."; fi
  else
    cmd="python3 $(shq "$REPO/bin/rules_block.py")"
    for a in "$@"; do cmd="$cmd $(shq "$a")"; done
    echo "To apply: $cmd"
  fi
}

# Background jobs as macOS LaunchAgents (bin/launch_agents.py), one [y/N] each:
# the memory checks every night, and the bus_nudge loop kept running.
offer_jobs() {
  [ "$(uname -s)" = Darwin ] || return 0
  for job in nightly-check bus-nudge; do
    echo
    if [ "$DRY" = 1 ]; then
      python3 "$REPO/bin/launch_agents.py" install "$job" --dry-run | sed -n 1,2p
    elif [ "$ASK" = 1 ]; then
      case $job in
        nightly-check) q="Run the memory checks every night (every installed agent)?" ;;
        bus-nudge) q="Keep the bus_nudge loop running (starts at login, restarts if it exits)?" ;;
      esac
      if ask "$q"; then python3 "$REPO/bin/launch_agents.py" install "$job" --yes; fi
    else
      echo "Optional: python3 $SHOWN/bin/launch_agents.py install $job"
    fi
  done
}

# The private environment description that drives both agents' auto-approval reviewers
# (bin/environment_render.py). Offers the blank template; never writes settings.json or
# config.toml itself: `environment_render.py apply` does that, after showing a diff.
offer_environment() {
  ENV_SRC=$(python3 "$REPO/bin/notes_config.py" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["environment"]["source"])' 2>/dev/null) \
    || ENV_SRC="$HOME/.config/session-notes/environment.md"
  [ -n "$ENV_SRC" ] || ENV_SRC="$HOME/.config/session-notes/environment.md"
  echo
  if [ -e "$ENV_SRC" ]; then
    echo "Environment file $(tilde "$ENV_SRC") exists. Preview what it renders to, then apply:"
  elif [ "$ASK" = 1 ]; then
    echo "Your environment (what the auto-approval reviewers should trust) is described in $(tilde "$ENV_SRC")."
    if ask "Copy the blank template there (mode 600)?"; then
      mkdir -p "$(dirname "$ENV_SRC")"
      cp "$REPO/templates/environment.md" "$ENV_SRC"; chmod 600 "$ENV_SRC"
      echo "wrote $(tilde "$ENV_SRC"). Fill it in, then:"
    else
      echo "Skipped the environment file. When you want it: cp templates/environment.md $(tilde "$ENV_SRC")"
      return 0
    fi
  else
    echo "To describe your environment: cp $SHOWN/templates/environment.md $(tilde "$ENV_SRC")"
    echo "Fill it in, then:"
  fi
  echo "  python3 $SHOWN/bin/environment_render.py diff     # what would change"
  echo "  python3 $SHOWN/bin/environment_render.py apply    # asks, backs up, then writes"
  if [ "$CODEX" = 1 ]; then
    echo "For Codex, apply --target codex writes sandbox_mode, approval_policy and approvals_reviewer"
    echo "(auto_review) plus the reviewer policy into $(tilde "$CODEX_DIR/config.toml"), and an allow rule for"
    echo "session_bus.py send into $(tilde "$CODEX_DIR/rules/default.rules") (docs/codex.md, Auto-review)."
  fi
}

if [ "$CODEX" = 1 ]; then
  # Codex keeps its own memory under <codex home>/session-notes; nothing here touches ~/.claude.
  CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
  echo "Codex skills to link into $(tilde "$CODEX_DIR/skills"): \$reflect \$refresh \$rnr \$review"
  if [ "$ASK" = 0 ] || ask "Link these 4 skills?"; then
    run mkdir -p "$CODEX_DIR/skills"
    for name in reflect refresh rnr review; do
      link "$REPO/skills/codex/$name" "$CODEX_DIR/skills/$name"
    done
  else
    echo "Skipped the skills."
  fi

  # The commands the standing rules and AGENTS.md name (wt, handoff_session, session_peers, bcast,
  # session_bus, bus_nudge ...). Without --bin-dir nothing is linked, as on the Claude path.
  echo
  if [ -z "$BIN_DIR" ]; then
    echo "Commands not linked: pass --bin-dir DIR (a directory on your PATH, for example ~/.local/bin)"
    echo "to link $TOOLS there."
  fi
  link_commands

  # Runtime state (bus mailbox, pane registry, locks) defaults to ~/.claude/session-notes/state.
  # With no Claude home on this machine, pin it under $XDG_STATE_HOME (~/.local/state) in the
  # session-notes config instead, so Codex never makes a ~/.claude. Decided here, once, never at
  # runtime; an explicit state_dir is left alone and a Claude Code machine pins nothing.
  echo
  if PIN=$(python3 "$REPO/bin/notes_config.py" --pin-state-dir --dry-run 2>&1); then
    echo "State directory: $PIN"
    case $PIN in
      "would pin"*)
        if [ "$DRY" = 1 ]; then :
        elif [ "$ASK" = 1 ]; then
          if ask "Pin it (keeping a .bak-session-notes copy of an existing config)?"; then
            python3 "$REPO/bin/notes_config.py" --pin-state-dir || echo "Failed (see above)."
          else echo "Skipped; state will be kept under the default ~/.claude/session-notes/state."; fi
        else echo "To apply: python3 $(shq "$REPO/bin/notes_config.py") --pin-state-dir"; fi ;;
    esac
  else
    echo "State directory: not pinned ($PIN)"
  fi

  # The live-context file sits outside both agent homes; Codex may write it only if its
  # directory is a writable root too.
  LIVE_DIR=$(SESSION_NOTES_AGENT=codex python3 "$REPO/bin/notes_config.py" --live-path 2>/dev/null)
  LIVE_DIR=$(dirname "${LIVE_DIR:-$HOME/.local/state/session-notes/live-context.md}")
  CODEX_TOML=$(cat <<TOML

# session-notes: the skills find the tools through these, and \$reflect may write its store.
[shell_environment_policy]
set = { SESSION_NOTES = "$REPO", SESSION_NOTES_AGENT = "codex" }

[sandbox_workspace_write]
writable_roots = ["$CODEX_DIR/session-notes", "$LIVE_DIR"]
TOML
)
  HJ="$CODEX_DIR/hooks.json" CT="$CODEX_DIR/config.toml"
  echo
  CH="$REPO/bin/codex_hooks.py"
  # A refusal (corrupt or unexpected hooks.json) skips this step only; the rest still runs.
  ch() { python3 "$CH" "$@"; }
  hooks_skipped() { echo "Hooks step skipped: $(tilde "$HJ") was left untouched (see above)."; }
  # Every hook command runs through one generated launcher, so hooks.json never names a
  # checkout: reinstalling from another checkout rewrites only the launcher (no re-trust).
  LAUNCHER="$HOME/.local/bin/session-notes"
  LAUNCHER_OK=1
  echo "Launcher for the hooks, $(tilde "$LAUNCHER") (a small script that runs this checkout's hooks):"
  if [ "$DRY" = 1 ]; then
    ch launcher --repo "$REPO" --path "$LAUNCHER" --dry-run || LAUNCHER_OK=0
  elif [ "$ASK" = 1 ]; then
    if PLAN=$(ch launcher --repo "$REPO" --path "$LAUNCHER" --dry-run); then
      case $(printf '%s\n' "$PLAN" | tail -n 1) in
        *"(unchanged)") echo "The launcher is current." ;;
        *)
          printf '%s\n' "$PLAN"
          if ask "Write it?"; then
            ch launcher --repo "$REPO" --path "$LAUNCHER" || LAUNCHER_OK=0
          else
            echo "Skipped the launcher."; LAUNCHER_OK=0
          fi ;;
      esac
    else
      LAUNCHER_OK=0
    fi
  else
    echo "Write it by hand with:"
    echo "  python3 '$CH' launcher --repo '$REPO' --path '$LAUNCHER'"
  fi
  echo
  echo "Hooks for $(tilde "$HJ"): boot load, time sense, bus mail, pane registry, em-dash guard."
  echo "An existing file is refreshed: only this repo's hook entries change, yours are kept."
  if [ "$LAUNCHER_OK" = 0 ]; then
    echo "Hooks step skipped: no launcher for them to run through."
  elif [ "$DRY" = 1 ]; then
    ch merge --repo "$REPO" --file "$HJ" --launcher "$LAUNCHER" --dry-run || hooks_skipped
  elif [ "$ASK" = 1 ]; then
    if ch merge --repo "$REPO" --file "$HJ" --launcher "$LAUNCHER" --dry-run; then
      if ask "Write it (keeping a .bak-session-notes copy)?"; then
        ch merge --repo "$REPO" --file "$HJ" --launcher "$LAUNCHER" | tail -n 1
      else
        echo "Skipped hooks.json."
      fi
    else
      hooks_skipped
    fi
  else
    echo "Merge into $(tilde "$HJ") by hand, or run:"
    echo "  python3 '$CH' merge --repo '$REPO' --file '$HJ' --launcher '$LAUNCHER'"
    ch show --launcher "$LAUNCHER"
  fi
  echo
  if [ "$ASK" = 1 ] && ! grep -q -E '^\[(shell_environment_policy|sandbox_workspace_write)\]' "$CT" 2>/dev/null; then
    echo "Would append to $(tilde "$CT"):"; echo "$CODEX_TOML"
    if ask "Append it (keeping a .bak-session-notes copy)?"; then
      if [ -e "$CT" ]; then cp "$CT" "$CT.bak-session-notes"; fi
      printf '%s\n' "$CODEX_TOML" >> "$CT"; echo "appended to $(tilde "$CT")"
      # Only now: a writable root must exist before the sandbox can write under it.
      mkdir -p "$LIVE_DIR"
    else
      echo "Skipped config.toml."
    fi
  else
    echo "Add to $(tilde "$CT") by hand, merging into any table of the same name:"; echo "$CODEX_TOML"
    if grep -q '^writable_roots' "$CT" 2>/dev/null && ! grep '^writable_roots' "$CT" | grep -qF "$LIVE_DIR"; then
      echo
      echo "Your writable_roots does not include the live-context directory; add this entry to it:"
      echo "  \"$LIVE_DIR\""
    fi
    echo "Then create that directory (a writable root must exist): mkdir -p $(tilde "$LIVE_DIR")"
  fi
  # The desktop app ignores writable_roots, so each write to the store is an approval prompt.
  # Saves go through one fixed command line; this rule approves exactly that line, nothing else.
  RULES="$CODEX_DIR/rules/default.rules"
  CODEX_RULES=$(cat <<RULE
prefix_rule(pattern=["/bin/zsh", "-lc", "python3 $REPO/bin/codex_save.py commit"], decision="allow")
prefix_rule(pattern=["python3", "$REPO/bin/codex_save.py", "commit"], decision="allow")
RULE
)
  echo
  if grep -qF "$REPO/bin/codex_save.py\", \"commit\"" "$RULES" 2>/dev/null; then
    echo "$(tilde "$RULES") already approves codex_save.py commit."
  elif [ "$ASK" = 1 ]; then
    echo "Would add to $(tilde "$RULES") (approves only the exact save command, so the app stops asking):"
    echo "$CODEX_RULES"
    if ask "Add it?"; then
      run mkdir -p "$CODEX_DIR/rules"
      if [ -e "$RULES" ]; then cp "$RULES" "$RULES.bak-session-notes"; fi
      printf '%s\n' "$CODEX_RULES" >> "$RULES"; echo "added to $(tilde "$RULES")"
    else
      echo "Skipped the rule; the app will ask once per save."
    fi
  else
    echo "Add to $(tilde "$RULES") by hand:"; echo "$CODEX_RULES"
  fi
  # The standing rules and the memory lines live in one marked block, rewritten on every
  # install, so an older install gets the newer sections too. Without the memory lines, a
  # "remember X" turn makes Codex hand-write the store before any skill loads.
  AG="$CODEX_DIR/AGENTS.md"
  echo
  echo "Standing rules and session-notes lines for $(tilde "$AG") (read by Codex at every session start):"
  offer "Write the block (keeping a timestamped backup)?" apply --file "$AG"
  # Models are set once, in rules_block.py. Top-level keys go before the first [table].
  echo
  echo "Default model and effort for $(tilde "$CT"), only where unset:"
  offer "Set them (keeping a timestamped backup)?" codex-defaults --config "$CT"
  echo
  echo "Profiles for sub-agent roles in $(tilde "$CODEX_DIR") (codex -p build, codex -p review; never overwritten):"
  offer "Create the missing ones?" profiles --dir "$CODEX_DIR"
  cat <<'TXT'

Trust new or changed hooks: /hooks in the Codex CLI, or Settings > Hooks in the desktop app.
Codex trusts the exact hook definition, so any change to an entry (even its timeout) may need
trusting again; an untrusted hook silently does not run.
Bus mail rides the next prompt when the hook runs inside the pane (`codex --no-daemon`, or
`codex features disable daemon_auto_start`, which sets it under [features] in config.toml);
otherwise bus_nudge falls back to the pull line.
Details: docs/codex.md
TXT
  offer_environment
  offer_jobs
  exit 0
fi

if [ "$ASK" = 1 ]; then
  echo "Skills to link into $(tilde "$SKILLS_DIR"): $(echo $SKILLS | sed 's/[^ ][^ ]*/\/&/g')"
fi
if [ "$ASK" = 0 ] || ask "Link these $(count "$SKILLS") skills?"; then
  run mkdir -p "$SKILLS_DIR"
  for name in $SKILLS; do
    link "$REPO/skills/$name" "$SKILLS_DIR/$name"
  done
else
  echo "Skipped the skills."
fi

link_commands

PR="python3 $SHOWN/hooks/session_pane_registry.py"
BUS="python3 $SHOWN/hooks/session_bus_hook.py"
CONSENT="python3 $SHOWN/hooks/merge_consent.py"
LIVE="python3 $SHOWN/bin/live.py hook"
hook() { printf '    {"hooks": [{"type": "command", "command": "%s"}]}' "$1"; }
if [ "$ALL" = 1 ]; then
  EXTRA_PROMPT=",
$(hook "python3 $SHOWN/hooks/time_sense.py")"
  EXTRA_EVENTS=',
  "PostToolUse": [
    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "python3 '"$SHOWN"'/hooks/em_dash_guard.py"}]}
  ]'
else
  EXTRA_PROMPT="" EXTRA_EVENTS=""
fi

HOOKS_JSON=$(cat <<EOF
{
  "SessionStart": [
$(hook "$PR"),
$(hook "$LIVE")
  ],
  "UserPromptSubmit": [
$(hook "$PR"),
$(hook "$BUS"),
$(hook "$CONSENT")$EXTRA_PROMPT
  ],
  "Stop": [
$(hook "$PR"),
$(hook "$BUS")
  ],
  "Notification": [
    {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "$PR"}]}
  ],
  "PreToolUse": [
    {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [{"type": "command", "command": "python3 $SHOWN/hooks/checkout_guard.py"}]},
    {"matcher": "Edit|Write|MultiEdit|Bash", "hooks": [{"type": "command", "command": "python3 $SHOWN/hooks/resume_size_guard.py"}]}
  ]$EXTRA_EVENTS
}
EOF
)

if [ "$ASK" = 1 ]; then
  set -- ; [ "$YES" = 1 ] && set -- --yes
  echo
  python3 "$REPO/bin/setup_claude.py" hooks --settings "$CLAUDE_DIR/settings.json" --json "$HOOKS_JSON" \
    --env "SESSION_NOTES=$REPO" --env "CLAUDE_CODE_SUBAGENT_EFFORT=medium" "$@"
  echo
  python3 "$REPO/bin/setup_claude.py" claude-md --file "$CLAUDE_DIR/CLAUDE.md" --snippet "$REPO/CLAUDE.snippet.md" "$@"
  echo
  echo "Standing rules for $(tilde "$CLAUDE_DIR/CLAUDE.md") (@import lines):"
  offer "Add them (keeping a timestamped backup)?" claude-apply --file "$CLAUDE_DIR/CLAUDE.md"
  CHECK="python3 $SHOWN/bin/session_peers.py"
  [ -n "$BIN_DIR" ] && [ -L "$BIN_DIR/session_peers" ] && CHECK="session_peers"
  cat <<EOF

Check, inside tmux: $CHECK
EOF
  offer_environment
  offer_jobs
  exit 0
fi

cat <<EOF

Next steps (by hand, on purpose):
1. In ~/.claude/settings.json, so the skills find the tools, add to "env":  "SESSION_NOTES": "$REPO"
   and, unless it already has a value, "CLAUDE_CODE_SUBAGENT_EFFORT": "medium" (the default effort for sub-agents)

2. Merge into the "hooks" object of ~/.claude/settings.json:
$HOOKS_JSON

3. Paste into a CLAUDE.md (~/.claude/CLAUDE.md, or one project's), so Claude knows the commands:
$(cat "$REPO/CLAUDE.snippet.md")

4. Add the standing rules to the same CLAUDE.md (or run: python3 $SHOWN/bin/rules_block.py claude-apply --file ~/.claude/CLAUDE.md):
$(python3 "$REPO/bin/rules_block.py" claude-lines 2>&1)

Optional: config.example.toml lists project scopes. Check, inside tmux: python3 $SHOWN/bin/session_peers.py
EOF
offer_environment
offer_jobs
