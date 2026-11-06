#!/bin/sh
# Run every --selftest in the repo with HOME pointed at a throwaway directory,
# so no test can read or write the real ~/.claude. CI runs exactly this.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SANDBOX=$(mktemp -d)
trap 'command rm -rf "$SANDBOX"' EXIT
REAL_HOME=$HOME
MARK="$SANDBOX/.start"
: > "$MARK"
export HOME="$SANDBOX"
export SESSION_NOTES_CONFIG="$SANDBOX/config.toml"
: > "$SESSION_NOTES_CONFIG"   # empty config = all defaults
unset TMUX TMUX_PANE
# A git hook runs this with GIT_DIR, GIT_INDEX_FILE and friends exported for the repo being
# committed to. A self-test's `git -C <tmpdir> ...` would then act on THAT repo (GIT_DIR
# overrides -C). Self-tests must never inherit the caller's repo, so drop every GIT_ var.
for v in $(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
fail=0
for f in "$ROOT"/bin/*.py "$ROOT"/hooks/*.py "$ROOT"/skills/*/*.py "$ROOT"/tools/*.py "$ROOT"/tests/leak_scan.py; do
  [ -f "$f" ] || continue
  grep -q -- "--selftest" "$f" || continue
  if out=$(python3 "$f" --selftest 2>&1); then
    echo "PASS ${f#$ROOT/}"
  else
    echo "FAIL ${f#$ROOT/}"; echo "$out" | tail -20 | sed 's/^/    /'; fail=1
  fi
done
if out=$(python3 "$ROOT/tests/test_hooks.py" 2>&1); then
  echo "PASS tests/test_hooks.py"
else
  echo "FAIL tests/test_hooks.py"; echo "$out" | grep -v '^ok' | sed 's/^/    /'; fail=1
fi

# No test may write into the real home. session-notes' default state lives
# under ~/.claude/session-notes and its config under ~/.config/session-notes,
# so anything created or changed there during this run is a leak out of the
# sandbox. (The private leak-terms file is the user's own and is excluded.)
for d in "$REAL_HOME/.claude/session-notes" "$REAL_HOME/.config/session-notes"; do
  [ -e "$d" ] || continue
  stray=$(find "$d" -newer "$MARK" ! -name leak-terms.txt ! -path "$d" 2>/dev/null)
  if [ -n "$stray" ]; then
    echo "FAIL real HOME touched by the test run:"; echo "$stray" | sed 's/^/    /'; fail=1
  fi
done
[ $fail = 0 ] && echo "PASS real HOME untouched"
exit $fail
