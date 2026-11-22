#!/usr/bin/env python3
"""em_dash_guard.py -- example PostToolUse(Write|Edit|apply_patch) hook: flag em dashes in prose just written.

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
  * Only the text of the CURRENT write is scanned (Write.content / Edit.new_string / the `+` lines of an apply_patch), so a
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


def parse_patch(text):
    """Codex apply_patch text -> [(path, added_text)]: only the `+` lines, per target file.

    Handles `*** Add File:`, `*** Update File:` (and `*** Move to:`, which retargets the
    file being updated), and ignores `*** Delete File:` and context/removed lines."""
    out = []
    path, lines = None, []

    def flush():
        if path is not None and lines:
            out.append((path, "\n".join(lines)))

    for ln in (text or "").splitlines():
        if ln.startswith("*** "):
            head, _, rest = ln[4:].partition(":")
            rest = rest.strip()
            if head in ("Add File", "Update File"):
                flush()
                path, lines = rest, []
            elif head == "Move to":
                path = rest
            elif head == "Delete File":
                flush()
                path, lines = None, []
            else:  # Begin Patch / End Patch / End of File
                continue
        elif path is not None and ln.startswith("+"):
            lines.append(ln[1:])
    flush()
    return out


def _writes(payload):
    """[(path, text)] this tool call introduced, or []."""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool_name in ("Write", "Edit"):
        return [(tool_input.get("file_path") or "", _written_text(tool_name, tool_input))]
    if tool_name == "apply_patch":
        cmd = tool_input.get("command")
        if isinstance(cmd, list):  # some shells pass argv; the patch is the last item
            cmd = cmd[-1] if cmd else ""
        base = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
        base = base or os.getcwd()
        # Patch paths are relative to the session cwd; the skip rules need the full path.
        return [(p if os.path.isabs(p) else os.path.join(base, p), t)
                for p, t in parse_patch(cmd if isinstance(cmd, str) else "")]
    return []


def _report(path, text):
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
    return n, rel, ctx


def check_payload(payload):
    """The hook's output dict for one PostToolUse payload, or None."""
    if not isinstance(payload, dict):
        return None
    hits = [(p, t) for p, t in _writes(payload) if _in_scope(p) and EM_DASH in t]
    if not hits:
        return None
    reports = [_report(p, t) for p, t in hits]
    total = sum(r[0] for r in reports)
    where = ", ".join(r[1] for r in reports)
    return {
        "systemMessage": "em-dash guard: %d em dash(es) in %s; asking the model to rewrite them."
        % (total, where),
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(r[2] for r in reports),
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
        patch = ("*** Begin Patch\n*** Add File: " + page + "\n+ok\n+" + dash + "\n"
                 "*** Update File: /tmp/code.py\n@@\n-old\n+" + dash + "\n"
                 "*** Update File: /tmp/notes.md\n@@\n ctx " + EM_DASH + " line\n-gone " + EM_DASH
                 + "\n+clean\n*** Delete File: /tmp/d.md\n*** End Patch")
        pp = check_payload({"tool_name": "apply_patch", "tool_input": {"command": patch}})
        pctx = pp["hookSpecificOutput"]["additionalContext"] if pp else ""
        check("apply_patch added line flagged", pp is not None and "L2:" in pctx)
        check("apply_patch only in-scope path", "code.py" not in pctx and "notes.md" not in pctx)
        check("apply_patch context/removed lines ignored", check_payload(
            {"tool_name": "apply_patch", "tool_input": {"command": patch.split("*** Update File: /tmp/code.py")[0].replace("+" + dash, "+x") + "*** Update File: /tmp/notes.md\n@@\n ctx " + EM_DASH + "\n-gone " + EM_DASH + "\n+clean\n*** End Patch"}}) is None)
        mv = "*** Begin Patch\n*** Update File: /tmp/a.txt\n*** Move to: /tmp/b.md\n@@\n+" + dash + "\n*** End Patch"
        mh = check_payload({"tool_name": "apply_patch", "tool_input": {"command": mv}})
        check("apply_patch move targets new path", mh is not None
              and "/tmp/b.md" in mh["hookSpecificOutput"]["additionalContext"])
        check("apply_patch bad command ignored", check_payload(
            {"tool_name": "apply_patch", "tool_input": {"command": None}}) is None)
        check("apply_patch memory path skipped", check_payload(
            {"tool_name": "apply_patch", "tool_input": {"command":
             "*** Begin Patch\n*** Add File: /tmp/memory/n.md\n+" + dash + "\n*** End Patch"}}) is None)
        rel = check_payload({"tool_name": "apply_patch", "cwd": "/work/proj", "tool_input": {"command":
            "*** Begin Patch\n*** Add File: memory/n.md\n+" + dash + "\n*** Add File: .claude/x.md\n+"
            + dash + "\n*** Add File: logs/a.txt\n+" + dash + "\n*** End Patch"}})
        check("apply_patch relative memory/, .claude/, logs/ paths skipped", rel is None)
        rel = check_payload({"tool_name": "apply_patch", "cwd": "/work/proj", "tool_input": {"command":
            "*** Begin Patch\n*** Add File: docs/a.md\n+" + dash + "\n*** End Patch"}})
        check("apply_patch relative prose path resolved and flagged", rel is not None
              and "/work/proj/docs/a.md" in rel["hookSpecificOutput"]["additionalContext"])
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
