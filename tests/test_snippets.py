#!/usr/bin/env python3
"""The Session tools section must read the same for Claude and Codex.

install.sh appends CLAUDE.snippet.md to ~/.claude/CLAUDE.md and AGENTS.snippet.md to
~/.codex/AGENTS.md. Both carry a "## Session tools (session-notes)" section; this test
fails if the two copies drift apart."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEAD = "## Session tools (session-notes)"


def section(path):
    """The lines from HEAD up to the next '## ' heading, trailing blanks dropped."""
    with open(os.path.join(ROOT, path)) as fh:
        lines = fh.read().splitlines()
    if HEAD not in lines:
        return None
    start = lines.index(HEAD)
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return out


def main():
    claude, codex = section("CLAUDE.snippet.md"), section("AGENTS.snippet.md")
    if claude is None or codex is None:
        print("FAIL a snippet has no %r section" % HEAD)
        return 1
    if claude != codex:
        print("FAIL Session tools differs between CLAUDE.snippet.md and AGENTS.snippet.md")
        return 1
    print("ok   Session tools section matches in both snippets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
