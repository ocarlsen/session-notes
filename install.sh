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
#   - with --codex, also sets Codex's default model and reasoning effort when unset, and
#     offers build and review profiles (codex -p build, codex -p review)
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
#   sh install.sh --codex            set up OpenAI Codex instead (docs/codex.md): link the
#                                    Codex skills into ~/.codex/skills, offer the hooks.json
#                                    and config.toml edits, the rules block, the model
#                                    defaults and profiles; never touches ~/.claude
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
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [ "$GITHOOKS_SET" = 1 ]; then
  if [ "$DRY" = 1 ]; then exec python3 "$REPO/bin/wt.py" install-hooks --dry-run "$GITHOOKS"; fi
  exec python3 "$REPO/bin/wt.py" install-hooks "$GITHOOKS"
fi

# A path under $HOME prints as ~/...: shorter, and the hook commands, which run in a shell,
# still resolve if the home directory's absolute path changes.
tilde() { case "$1" in "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }
SHOWN=$(tilde "$REPO")

run() { if [ "$DRY" = 1 ]; then echo "would: $*"; else "$@"; fi; }

link() {  # link SRC DEST: create or refresh a symlink, never clobber a real file
  src=$1 dest=$2
  if [ -L "$dest" ]; then
    run ln -sfn "$src" "$dest"
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
if [ "$ALL" = 1 ]; then
  # skills/codex/ holds the Codex variants (install.sh --codex), not a Claude skill.
  SKILLS=$(for d in "$REPO"/skills/*/; do n=$(basename "$d"); [ "$n" = codex ] || echo "$n"; done)
  TOOLS="$TOOLS session_bus bus_nudge reflect_lock append_memory_index memory_index_lint \
         boot_budget memory_hygiene_canary nightly_check launch_agents notes_config"
fi

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

if [ "$CODEX" = 1 ]; then
  # Codex keeps its own memory under <codex home>/session-notes; nothing here touches ~/.claude.
  CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
  echo "Codex skills to link into $(tilde "$CODEX_DIR/skills"): \$reflect \$refresh \$rnr"
  if [ "$ASK" = 0 ] || ask "Link these 3 skills?"; then
    run mkdir -p "$CODEX_DIR/skills"
    for name in reflect refresh rnr; do
      link "$REPO/skills/codex/$name" "$CODEX_DIR/skills/$name"
    done
  else
    echo "Skipped the skills."
  fi

  CODEX_HOOKS=$(cat <<JSON
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|compact",
        "hooks": [
          {
            "type": "command",
            "command": "SESSION_NOTES_AGENT=codex python3 $REPO/hooks/codex_session_start.py",
            "statusMessage": "Loading session notes",
            "timeout": 10,
            "additionalContextLimit": 4000
          }
        ]
      }
    ]
  }
}
JSON
)
  CODEX_TOML=$(cat <<TOML

# session-notes: the skills find the tools through these, and \$reflect may write its store.
[shell_environment_policy]
set = { SESSION_NOTES = "$REPO", SESSION_NOTES_AGENT = "codex" }

[sandbox_workspace_write]
writable_roots = ["$CODEX_DIR/session-notes"]
TOML
)
  HJ="$CODEX_DIR/hooks.json" CT="$CODEX_DIR/config.toml"
  echo
  if [ "$ASK" = 1 ] && [ ! -e "$HJ" ]; then
    echo "$(tilde "$HJ") does not exist yet. It would contain:"; echo "$CODEX_HOOKS"
    if ask "Write it?"; then
      printf '%s\n' "$CODEX_HOOKS" > "$HJ"; echo "wrote $(tilde "$HJ")"
    else
      echo "Skipped hooks.json."
    fi
  else
    echo "Merge into $(tilde "$HJ") by hand (an existing file is never edited):"; echo "$CODEX_HOOKS"
  fi
  echo
  if [ "$ASK" = 1 ] && ! grep -q -E '^\[(shell_environment_policy|sandbox_workspace_write)\]' "$CT" 2>/dev/null; then
    echo "Would append to $(tilde "$CT"):"; echo "$CODEX_TOML"
    if ask "Append it (keeping a .bak-session-notes copy)?"; then
      if [ -e "$CT" ]; then cp "$CT" "$CT.bak-session-notes"; fi
      printf '%s\n' "$CODEX_TOML" >> "$CT"; echo "appended to $(tilde "$CT")"
    else
      echo "Skipped config.toml."
    fi
  else
    echo "Add to $(tilde "$CT") by hand, merging into any table of the same name:"; echo "$CODEX_TOML"
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
  cat <<TXT

Then trust the hook: /hooks in the Codex CLI, or Settings > Hooks in the desktop app.
Codex trusts a hook by its hash, so re-trust it after any edit. Details: docs/codex.md
TXT
  offer_jobs
  exit 0
fi

count() { set -- $1; echo $#; }

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

if [ -n "$BIN_DIR" ]; then
  [ "$ASK" = 1 ] && { echo; echo "Commands to link into $(tilde "$BIN_DIR"): $TOOLS"; }
  if [ "$ASK" = 0 ] || ask "Link these $(count "$TOOLS") commands?"; then
    run mkdir -p "$BIN_DIR"
    for tool in $TOOLS; do
      [ -f "$REPO/bin/$tool.py" ] && link "$REPO/bin/$tool.py" "$BIN_DIR/$tool"
    done
  else
    echo "Skipped the commands."
  fi
fi

PR="python3 $SHOWN/hooks/session_pane_registry.py"
BUS="python3 $SHOWN/hooks/session_bus_hook.py"
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
$(hook "$PR")
  ],
  "UserPromptSubmit": [
$(hook "$PR"),
$(hook "$BUS")$EXTRA_PROMPT
  ],
  "Stop": [
$(hook "$PR"),
$(hook "$BUS")
  ],
  "Notification": [
    {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "$PR"}]}
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
offer_jobs
