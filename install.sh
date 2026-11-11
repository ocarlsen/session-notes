#!/bin/sh
# install.sh -- link session-notes into a Claude Code setup.
#
# What it does:
#   - symlinks the /reflect, /refresh and /rnr skills into ~/.claude/skills/<name>
#   - optionally symlinks the three commands (handoff_session, session_peers,
#     bcast) into a directory on your PATH
#   - in a terminal, it asks [y/N] before each of those, and before each edit below
#   - offers, one [y/N] question each, to add its hooks (and SESSION_NOTES, which the
#     skills use to find the tools) to ~/.claude/settings.json, and the lines that tell
#     Claude the commands exist to ~/.claude/CLAUDE.md
#     (it shows what it would add and keeps a .bak-session-notes copy first);
#     not in a terminal, or with --dry-run, it prints both for you to paste
#   - with --all: every skill in skills/, every tool, and the optional hooks
#
# What it never does: change settings.json or a CLAUDE.md without a yes, touch
# your memory files, or overwrite anything that is not already a symlink it created.
#
#   sh install.sh                    ask about the links, then about the two edits
#   sh install.sh --bin-dir ~/.local/bin
#   sh install.sh --all --bin-dir ~/.local/bin
#   sh install.sh --yes              answer yes to every question
#   sh install.sh --dry-run          show what would happen
set -eu

REPO=$(cd "$(dirname "$0")" && pwd)
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME/.claude}}"
SKILLS_DIR="$CLAUDE_DIR/skills"
BIN_DIR=""
DRY=0
ALL=0
YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --dry-run|-n) DRY=1; shift ;;
    --all) ALL=1; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

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
TOOLS="handoff_session session_peers bcast"
if [ "$ALL" = 1 ]; then
  SKILLS=$(for d in "$REPO"/skills/*/; do basename "$d"; done)
  TOOLS="$TOOLS session_bus bus_nudge reflect_lock append_memory_index memory_index_lint \
         boot_budget memory_hygiene_canary notes_config"
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
    --env "SESSION_NOTES=$REPO" "$@"
  echo
  python3 "$REPO/bin/setup_claude.py" claude-md --file "$CLAUDE_DIR/CLAUDE.md" --snippet "$REPO/CLAUDE.snippet.md" "$@"
  CHECK="python3 $SHOWN/bin/session_peers.py"
  [ -n "$BIN_DIR" ] && [ -L "$BIN_DIR/session_peers" ] && CHECK="session_peers"
  cat <<EOF

Check, inside tmux: $CHECK
EOF
  exit 0
fi

cat <<EOF

Next steps (by hand, on purpose):
1. In ~/.claude/settings.json, so the skills find the tools, add to "env":  "SESSION_NOTES": "$REPO"

2. Merge into the "hooks" object of ~/.claude/settings.json:
$HOOKS_JSON

3. Paste into a CLAUDE.md (~/.claude/CLAUDE.md, or one project's), so Claude knows the commands:
$(cat "$REPO/CLAUDE.snippet.md")

Optional: config.example.toml lists project scopes. Check, inside tmux: python3 $SHOWN/bin/session_peers.py
EOF
