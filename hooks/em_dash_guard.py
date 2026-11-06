#!/usr/bin/env python3
"""em_dash_guard.py -- example PostToolUse(Write|Edit) hook: flag em dashes in prose just written.

An example of turning a style rule into a forcing function. A rule that lives only in
memory ("no em dashes in prose") is self-policed and keeps slipping, mostly in what the
model writes interactively: docs, pages, drafts. This hook scans the text the model just
WROTE and, if an em dash landed in a prose file, surfaces it to both the user
(systemMessage) and the model (additionalContext) so the model rewrites it before it ships.

What it does on a hit: prints one JSON object with a short systemMessage and an
additionalContext block listing the offending lines (up to 12) and asking the model to
rewrite them. Nothing else: no file is touched.

Scope by design:
  * FLAGGED surfaces: .html/.htm/.txt/.md prose (docs, pages, drafts).
  * NOT flagged: code (.py/.js/.sh/.json/.css/...), where an em dash in a comment is
    harmless, and internal memory files (CLAUDE.md, MEMORY.md, RESUME.md, anything under
    a .claude/ or memory/ directory), where dashes are historical record, not output.
  * Only the text of the CURRENT write is scanned (Write.content / Edit.new_string), so a
    file's pre-existing dashes the model didn't touch never re-nag.

Advisory, not blocking: it never undoes the write and never rewrites prose (a justified
em dash or a code string must survive). Always exits 0; any error is silent so a guard
hiccup can never break a write. Edit the sets below to fit your own rule.

    python3 em_dash_guard.py --selftest
"""
import json
import os
import sys

EM_DASH = chr(0x2014)  # em dash only; the en dash in number/date ranges is deliberately allowed

# Basenames whose em dashes are internal record, never output.
_SKIP_BASENAMES = {"CLAUDE.md", "MEMORY.md", "RESUME.md"}
# Path fragments that mark internal stores (dashes there are history, not output).
_SKIP_FRAGMENTS = ("/.claude/", "/memory/", "/.backups/", "/logs/")

# Only these extensions carry prose.
_PROSE_EXTS = (".html", ".htm", ".txt", ".md")


def _in_scope(path):
    if not path:
        return False
    low = path.lower()
    if not low.endswith(_PROSE_EXTS):
        return False
    base = os.path.basename(path)
    if base in _SKIP_BASENAMES:
        return False
    if any(frag in path for frag in _SKIP_FRAGMENTS):
        return False
    return True


def _written_text(tool_name, tool_input):
    """The text THIS call introduced, not the whole file."""
    if tool_name == "Write":
        return tool_input.get("content") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string") or ""
    return ""


def check_payload(payload):
    """The hook's output dict for one PostToolUse payload, or None."""
    if not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Write", "Edit"):
        return None
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    if not _in_scope(path):
        return None
    text = _written_text(tool_name, tool_input)
    if EM_DASH not in text:
        return None

    # Collect the offending lines so the rewrite is one-shot.
    offenders = [
        (i, ln.strip())
        for i, ln in enumerate(text.splitlines(), 1)
        if EM_DASH in ln
    ]
    n = sum(ln.count(EM_DASH) for _, ln in offenders)
    rel = path
    home = os.path.expanduser("~")
    if rel.startswith(home + os.sep):
        rel = "~" + rel[len(home):]

    snippet = "\n".join("  L%d: %s" % (i, s[:160]) for i, s in offenders[:12])
    if len(offenders) > 12:
        snippet += "\n  ... (%d more lines)" % (len(offenders) - 12)

    ctx = (
        "EM-DASH GUARD: %d em dash(es) in the text just written to %s.\n"
        "House rule: no em dashes in prose by default. Rewrite each one out now (split into "
        "two sentences, or use a comma, colon or parentheses) UNLESS it is honestly the only "
        "construction that works, in which case say why. Do not leave it for the user to "
        "catch.\n%s" % (n, rel, snippet)
    )
    return {
        "systemMessage": "em-dash guard: %d em dash(es) in %s; asking the model to rewrite them."
        % (n, rel),
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": ctx,
        },
    }


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    out = check_payload(payload)
    if out is not None:
        sys.stdout.write(json.dumps(out))


def selftest():
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        dash = "a " + EM_DASH + " b"
        page = os.path.join(home, "site", "index.html")
        hit = check_payload({"tool_name": "Write",
                             "tool_input": {"file_path": page, "content": "ok\n" + dash}})
        check("write to prose flagged", hit is not None)
        ctx = hit["hookSpecificOutput"]["additionalContext"] if hit else ""
        check("line number reported", "L2:" in ctx)
        check("home shortened", "~/site/index.html" in ctx)
        check("no em dash in own output", hit is not None
              and EM_DASH not in hit["systemMessage"] and EM_DASH not in ctx.split("\n")[1])
        check("edit new_string flagged", check_payload(
            {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x.md",
                                                 "old_string": "x", "new_string": dash}}))
        check("clean text silent", check_payload(
            {"tool_name": "Write", "tool_input": {"file_path": page, "content": "a - b"}}) is None)
        check("en dash allowed", check_payload(
            {"tool_name": "Write", "tool_input": {"file_path": page,
                                                  "content": "1" + chr(0x2013) + "5"}}) is None)
        check("code skipped", check_payload(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x.py", "content": dash}}) is None)
        check("memory file skipped", check_payload(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/p/MEMORY.md",
                                                  "content": dash}}) is None)
        check("memory dir skipped", check_payload(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/p/memory/note.md",
                                                  "content": dash}}) is None)
        check("other tool ignored", check_payload(
            {"tool_name": "Read", "tool_input": {"file_path": page}}) is None)
        check("junk payload ignored", check_payload(["x"]) is None)
        many = "\n".join([dash] * 15)
        big = check_payload({"tool_name": "Write",
                             "tool_input": {"file_path": page, "content": many}})
        check("long list truncated", big is not None and "(3 more lines)"
              in big["hookSpecificOutput"]["additionalContext"])
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        main()
    except Exception:
        pass
