#!/usr/bin/env python3
"""test_edit_check.py: scan a unified diff for edits to EXISTING tests, fixtures and guards.

A change that weakens the check that would catch it is the first thing a reviewer should look
at. This tool surfaces, from the diff alone and without trusting a builder's own account of it,
every removed or changed line in a test-shaped file, every deleted test file, and every removed
assertion in any Python file.

It REPORTS, never blocks. A test, fixture or guard edit is routinely legitimate (a format
change, a verified fix that invalidates a recorded answer) and a diff cannot tell that apart
from a quiet loosening. Each hit is a line for a human or a reviewer to look at before trusting
a "tests still pass" claim.

    git diff origin/main... | test_edit_check.py
    test_edit_check.py --diff-file change.diff

Test-shaped means the built-in path rules (a tests/ or test/ directory, test_*.py, *_test.py,
*.spec.ts/js, a fixtures/ directory, an evals/ directory, canary and leak-term file names) plus
any fnmatch globs in session-notes config `[review].test_globs`.

Pure core (scan, file_counts, format_output) plus a thin CLI; bin/review.py imports the core.

Exit codes: 0 for a successful scan, whether or not there are hits; 1 for a malformed config
value (the message names the key and value); 2 for a usage error (argparse) or a --selftest
failure.
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import notes_config  # noqa: E402

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_GIT_RE = re.compile(r"^diff --git ")
ASSERT_RE = re.compile(r"\bassert\b")

_CANARY_KEYWORDS = ("canary", "leak_scan", "public_guard", "leak-terms", "leak_terms")
_MAX_REPORT_LINES = 40


def _is_test_path(path):
    """True if the bare path shape alone marks this as a test/fixture/guard file."""
    parts = [p for p in path.split("/") if p]
    base = parts[-1] if parts else path
    if "tests" in parts[:-1] or "test" in parts[:-1]:
        return True
    if fnmatch.fnmatch(base, "test_*.py") or fnmatch.fnmatch(base, "*_test.py"):
        return True
    if fnmatch.fnmatch(base, "*.spec.ts") or fnmatch.fnmatch(base, "*.spec.js"):
        return True
    if "fixtures" in parts[:-1]:
        return True
    if path.startswith("evals/") or "/evals/" in path:
        return True
    for kw in _CANARY_KEYWORDS:
        if kw in base:
            return True
    return False


def config_globs(cfg=None):
    """Extra test globs from config `[review].test_globs`. A malformed value raises
    notes_config.ConfigError naming the key (notes_config validates it on load)."""
    cfg = cfg or notes_config.load()
    return list(cfg["review"]["test_globs"])


def _is_test_file(path, extra_globs):
    if _is_test_path(path):
        return True
    for g in extra_globs:
        if fnmatch.fnmatch(path, g):
            return True
    return False


def _split_blocks(diff_text):
    """Split a unified diff into per-file blocks on 'diff --git ' lines.

    A diff with no such header (e.g. a bare single-file hunk) is treated as
    one block so the function still does something sane rather than nothing.
    """
    blocks = []
    cur = []
    for line in diff_text.splitlines():
        if DIFF_GIT_RE.match(line):
            if cur:
                blocks.append(cur)
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        blocks.append(cur)
    if not blocks and diff_text.strip():
        blocks = [diff_text.splitlines()]
    return blocks


def _split_header_and_hunks(block_lines):
    """Split a file block into its header region (before the first '@@') and
    its hunk region (from the first '@@' onward).

    This matters because '--- '/'+++ ' only mean "file path" in the header.
    Inside a hunk, a removed line that happens to start with '---' (a SQL
    '-- comment' line, or a content line that itself reads '-- a/x') is
    content, not a second file-header line, and must never overwrite the
    path this block is already attributed to.
    """
    header = []
    hunk_lines = []
    in_header = True
    for line in block_lines:
        if in_header and HUNK_RE.match(line):
            in_header = False
        if in_header:
            header.append(line)
        else:
            hunk_lines.append(line)
    return header, hunk_lines


_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


def _unquote_c(tok):
    """Decode a git C-quoted path (`"a/t\\303\\251st.py"`); any other string is returned as is.

    Octal escapes are UTF-8 bytes; \\\\ \\" \\t \\n and the other single-letter escapes are handled.
    """
    if not (len(tok) >= 2 and tok.startswith('"') and tok.endswith('"')):
        return tok
    raw = tok[1:-1]
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\" or i + 1 >= len(raw):
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in "0123":
            digits = raw[i + 1:i + 4]
            if len(digits) == 3 and all(c in "01234567" for c in digits):
                out.append(int(digits, 8))
                i += 4
                continue
        if nxt in _C_ESCAPES:
            out.append(_C_ESCAPES[nxt])
        else:
            out += ("\\" + nxt).encode("utf-8")
        i += 2
    return out.decode("utf-8", errors="replace")


def _strip_prefix(p):
    p = _unquote_c(p)
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def _quoted_head(rest):
    """(first token, remainder) when `rest` starts with a C-quoted token, else None."""
    if not rest.startswith('"'):
        return None
    i = 1
    while i < len(rest):
        if rest[i] == "\\":
            i += 2
            continue
        if rest[i] == '"':
            return rest[:i + 1], rest[i + 1:].strip()
        i += 1
    return None


def _git_header_path(header_lines):
    """Path from `diff --git a/X b/X` when both sides name the same file (no rename)."""
    for line in header_lines:
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):].strip()
            n = (len(rest) - 5) // 2
            if n > 0:
                a = rest[2:2 + n]
                if rest == "a/%s b/%s" % (a, a):
                    return a
            # a quoted form: `"a/x" "b/x"`, `a/x "b/x"` or `"a/x" b/x`
            head = _quoted_head(rest)
            if head:
                first, second = head
            else:
                k = rest.find(' "b/')
                first, second = (rest[:k], rest[k + 1:]) if k > 0 and rest.endswith('"') else (
                    None, None)
            if first is not None:
                a, b = _strip_prefix(first), _strip_prefix(second)
                if a and a == b:
                    return a
    return None


def _block_paths(header_lines):
    """(old_path, new_path) for a block, from its header region ONLY (see
    _split_header_and_hunks for why that restriction matters). Either may be None (a new
    file has no old path, a deleted one no new path). The `---`/`+++` lines win; when they
    are absent (a pure rename, a binary file, an empty deleted file) `rename from`/`rename to`
    and then the `diff --git` line supply the path."""
    old = new = None
    rename_from = rename_to = None
    for line in header_lines:
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p != "/dev/null":
                new = _strip_prefix(p)
        elif line.startswith("--- "):
            p = line[4:].strip()
            if p != "/dev/null":
                old = _strip_prefix(p)
        elif line.startswith("rename from "):
            rename_from = _unquote_c(line[len("rename from "):].strip())
        elif line.startswith("rename to "):
            rename_to = _unquote_c(line[len("rename to "):].strip())
    git_path = _git_header_path(header_lines)
    old = old or rename_from or git_path
    new = new or rename_to or git_path
    return old, new


def _count_lines(hunk_lines):
    """Raw added/removed content-line counts for a file's hunk region.

    Used only to decide, per file, whether a diff is a net SHRINK (more
    removed than added) for the PR-comment noise tiering in format_output.
    """
    added = removed = 0
    for line in hunk_lines:
        if HUNK_RE.match(line):
            continue
        if line.startswith("\\"):
            continue  # "\ No newline at end of file" is not a real line
        if line.startswith("-"):
            removed += 1
        elif line.startswith("+"):
            added += 1
    return added, removed


def _paren_delta(text):
    """Net open parens on a line, counted from its first `check(` when it has one."""
    i = text.find("check(")
    seg = text[i:] if i >= 0 else text
    return seg.count("(") - seg.count(")")


def _process_block(old_path, new_path, is_new, is_deleted, is_binary, hunk_lines, extra_globs):
    path = new_path or old_path
    hits = []
    renamed = bool(old_path and new_path and old_path != new_path)
    # A file moved OUT of a test path is still an edit to a test: either path being
    # test-shaped counts.
    is_test = _is_test_file(path, extra_globs) or (
        renamed and _is_test_file(old_path, extra_globs))
    is_py = path.endswith(".py") or bool(old_path and old_path.endswith(".py"))

    if is_new:
        return hits  # new files produce nothing by construction

    if is_deleted:
        if is_test:
            first_line = None
            for line in hunk_lines:
                m = HUNK_RE.match(line)
                if m:
                    first_line = int(m.group(1))
                    break
            hits.append(
                {
                    "file": path,
                    "line": first_line or 1,
                    "text": f"(file deleted) {path}",
                    "reason": "test-file-deleted",
                }
            )
        return hits  # one hit per deleted file, never one per removed line

    if renamed and _is_test_file(old_path, extra_globs):
        hits.append(
            {
                "file": old_path,
                "line": 1,
                "text": f"(renamed) {old_path} -> {new_path}",
                "reason": "test-file-renamed",
            }
        )
    if is_binary and is_test and not renamed:
        hits.append(
            {
                "file": path,
                "line": 1,
                "text": f"(binary file changed) {path}",
                "reason": "test-file-binary",
            }
        )

    # Group hunk lines into hunks, each tracking its own old-side line counter.
    hunks = []
    current = None
    for line in hunk_lines:
        m = HUNK_RE.match(line)
        if m:
            if current is not None:
                hunks.append(current)
            current = {"old_start": int(m.group(1)), "lines": []}
        elif current is not None:
            current["lines"].append(line)
    if current is not None:
        hunks.append(current)

    for hunk in hunks:
        old_line = hunk["old_start"]
        # Inside a hunk every line that starts with '-' or '+' is content
        # (the file-header '---'/'+++' lines live outside the hunk region).
        added_stripped = {l[1:].strip() for l in hunk["lines"] if l.startswith("+")}
        # Multi-line check( heuristic (Python files outside test paths): while a `check(`
        # call seen on an old-side line of THIS hunk still has unbalanced parens, any removed
        # line is part of that call and is flagged. Limits: parens inside strings are counted,
        # and a call that opens before the hunk's first context line is not seen.
        open_depth = 0
        for line in hunk["lines"]:
            if line.startswith("\\"):
                continue  # "\ No newline at end of file" is not a real line
            if line.startswith("+"):
                continue  # added lines don't advance the old-side counter
            removed = line.startswith("-")
            text = line[1:]
            stripped = text.strip()
            cur_old_line = old_line
            old_line += 1
            inside_call = open_depth > 0
            if inside_call:
                open_depth = max(open_depth + text.count("(") - text.count(")"), 0)
            elif "check(" in stripped and not stripped.startswith("#"):
                open_depth = max(_paren_delta(stripped), 0)
            if not removed:
                continue
            if stripped == "":
                continue  # blank removed lines are not a finding
            if stripped in added_stripped:
                continue  # reindent/move within the same hunk
            is_assert = "check(" in stripped or (
                not stripped.startswith("#") and ASSERT_RE.search(stripped))
            if is_test:
                # A changed assertion is its own detail line, never folded into the
                # "N lines changed" summary row.
                reason = "assertion" if is_assert else "test-file"
                hits.append({"file": path, "line": cur_old_line, "text": text, "reason": reason})
            elif is_py:
                if is_assert or inside_call:
                    hits.append(
                        {"file": path, "line": cur_old_line, "text": text, "reason": "assertion"}
                    )
    return hits


def _scan_blocks(diff_text, extra_globs=None):
    """Shared walk over every file block: returns (hits, file_counts).

    file_counts maps path -> {"added": n, "removed": n} for the noise-tiering
    decision in format_output. scan() and file_counts() below are thin
    public wrappers so most callers never need to know this exists.
    """
    extra_globs = list(extra_globs or [])
    hits = []
    counts = {}
    for block in _split_blocks(diff_text):
        header, hunk_lines = _split_header_and_hunks(block)
        old_path, new_path = _block_paths(header)
        path = new_path or old_path
        if not path:
            continue
        is_new = any(line.startswith("new file mode") for line in header)
        is_deleted = any(line.startswith("deleted file mode") for line in header)
        is_binary = any(line.startswith("Binary files ") or line.startswith("GIT binary patch")
                        for line in header)
        added, removed = _count_lines(hunk_lines)
        counts[path] = {"added": added, "removed": removed}
        hits.extend(_process_block(old_path, new_path, is_new, is_deleted, is_binary,
                                   hunk_lines, extra_globs))
    return hits, counts


def scan(diff_text, extra_globs=None):
    """Return a list of hits: {file, line, text, reason} for a unified diff.

    `extra_globs` are fnmatch globs (config `[review].test_globs`) that also mark a file as
    a test, fixture or guard.
    """
    hits, _ = _scan_blocks(diff_text, extra_globs=extra_globs)
    return hits


def file_counts(diff_text, extra_globs=None):
    """Per-file {"added": n, "removed": n} content-line counts for a diff."""
    _, counts = _scan_blocks(diff_text, extra_globs=extra_globs)
    return counts


def format_output(hits, counts=None):
    """Text for a reviewer. Report-only: this never claims to block anything.

    Noise tiering (routine fixture churn is common): an "assertion" or "test-file-deleted" hit always lists
    its line in full, since those are rare and specific. A "test-file" hit
    only lists in full when its file is a net SHRINK (more removed than
    added) in this diff; otherwise every "test-file" hit for that file
    collapses into one summary row, so a large fixture reshuffle that grew
    the test doesn't bury the comment in lines nobody will read.

    `counts=None` (no per-file counts available, e.g. a hand-rolled diff
    with no file_counts() pass alongside it) means there is no basis for
    the net-shrink-vs-growth call, so every hit is listed in full with no
    collapsing, rather than silently defaulting added=removed=0 (which
    reads as a net-growth file and collapses into a misleading "0 lines
    changed" row).
    """
    no_counts = counts is None
    counts = counts or {}
    entries = []
    collapsed_files = set()
    for h in hits:
        if h["reason"] in ("assertion", "test-file-deleted", "test-file-renamed",
                            "test-file-binary"):
            entries.append(f"{h['file']}:{h['line']} [{h['reason']}] {h['text'].strip()}")
            continue
        if no_counts:
            entries.append(f"{h['file']}:{h['line']} [{h['reason']}] {h['text'].strip()}")
            continue
        c = counts.get(h["file"], {"added": 0, "removed": 0})
        if c.get("removed", 0) > c.get("added", 0):
            entries.append(f"{h['file']}:{h['line']} [{h['reason']}] {h['text'].strip()}")
        else:
            if h["file"] in collapsed_files:
                continue
            collapsed_files.add(h["file"])
            total = c.get("added", 0) + c.get("removed", 0)
            entries.append(
                f"{h['file']}: {total} lines changed "
                f"({c.get('removed', 0)} removed / {c.get('added', 0)} added)"
            )

    lines = [
        "This change removes or edits lines in existing tests, fixtures or guards, or removes "
        "assertions. Look at each one first: was the check loosened to pass, and what still fails "
        "if the fix regresses?",
        "",
        "```",
    ]
    lines.extend(entries[:_MAX_REPORT_LINES])
    if len(entries) > _MAX_REPORT_LINES:
        lines.append(f"... {len(entries) - _MAX_REPORT_LINES} more")
    lines.append("```")
    return "\n".join(lines)


def selftest():
    ok = True
    must_fire_count = 0

    def check(cond, msg):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"  [{status}] {msg}")

    # --- a. tests/test_x.py: fixture string changed -> test-file hit at the right line ---
    must_fire_count += 1
    diff_a = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -10,2 +10,2 @@\n"
        '-    text = "a jar"\n'
        '+    text = "a plain jar on the shelf"\n'
    )
    hits_a = scan(diff_a)
    check(
        len(hits_a) == 1 and hits_a[0]["reason"] == "test-file" and hits_a[0]["line"] == 10,
        "a. fixture string change in tests/test_x.py -> one test-file hit at line 10",
    )

    # --- b. bin/tool.py: looser check() replaces a tighter one -> assertion hit ---
    must_fire_count += 1
    diff_b = (
        "diff --git a/bin/tool.py b/bin/tool.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/bin/tool.py\n"
        "+++ b/bin/tool.py\n"
        "@@ -20,2 +20,2 @@\n"
        '-    check("drops short", out == [])\n'
        '+    check("drops short", True)\n'
    )
    hits_b = scan(diff_b)
    check(
        len(hits_b) == 1 and hits_b[0]["reason"] == "assertion" and hits_b[0]["line"] == 20,
        "b. loosened check() in bin/tool.py (non-test path) -> one assertion hit",
    )

    # --- c. bin/tool.py: assert deleted, nothing added -> assertion hit ---
    must_fire_count += 1
    diff_c = (
        "diff --git a/bin/tool.py b/bin/tool.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/bin/tool.py\n"
        "+++ b/bin/tool.py\n"
        "@@ -30,1 +29,0 @@\n"
        "-    assert x == 3\n"
    )
    hits_c = scan(diff_c)
    check(
        len(hits_c) == 1 and hits_c[0]["reason"] == "assertion" and hits_c[0]["line"] == 30,
        "c. bare assert deleted in bin/tool.py -> one assertion hit",
    )

    # --- d. tests/leak_scan.py: a pattern line edited -> test-file hit ---
    must_fire_count += 1
    diff_d = (
        "diff --git a/tests/leak_scan.py b/tests/leak_scan.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/leak_scan.py\n"
        "+++ b/tests/leak_scan.py\n"
        "@@ -5,1 +5,1 @@\n"
        '-PATTERNS = ["foo"]\n'
        '+PATTERNS = ["foo", "bar"]\n'
    )
    hits_d = scan(diff_d)
    check(
        len(hits_d) == 1 and hits_d[0]["reason"] == "test-file",
        "d. edited pattern line in tests/leak_scan.py -> one test-file hit",
    )

    # --- e. tests/test_old.py deleted (several - lines) -> exactly ONE hit ---
    must_fire_count += 1
    diff_e = (
        "diff --git a/tests/test_old.py b/tests/test_old.py\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/tests/test_old.py\n"
        "+++ /dev/null\n"
        "@@ -1,5 +0,0 @@\n"
        "-import unittest\n"
        "-\n"
        "-\n"
        "-def test_x():\n"
        "-    assert True\n"
    )
    hits_e = scan(diff_e)
    check(
        len(hits_e) == 1 and hits_e[0]["reason"] == "test-file-deleted",
        "e. deleted tests/test_old.py -> exactly one test-file-deleted hit, not one per line",
    )

    # --- f. config test_globs extra glob fires on profile_data.py ---
    must_fire_count += 1
    import tempfile

    diff_f = (
        "diff --git a/profile_data.py b/profile_data.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/profile_data.py\n"
        "+++ b/profile_data.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-DATA = 1\n"
        "+DATA = 2\n"
    )
    hits_f = scan(diff_f, extra_globs=["profile_data.py"])
    check(
        len(hits_f) == 1 and hits_f[0]["reason"] == "test-file",
        "f. an extra test glob matches profile_data.py -> one test-file hit",
    )

    check(must_fire_count == 6, "positive control: six MUST-FIRE cases were actually run")

    # --- g. new file tests/test_new.py -> nothing ---
    diff_g = (
        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/tests/test_new.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def test_x():\n"
        "+    assert True\n"
        "+    pass\n"
    )
    check(scan(diff_g) == [], "g. new file tests/test_new.py -> no hits")

    # --- h. tests/test_x.py pure addition -> nothing ---
    diff_h = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -20,2 +20,3 @@\n"
        " def test_y():\n"
        "     pass\n"
        "+    extra_check()\n"
    )
    check(scan(diff_h) == [], "h. pure addition to an existing hunk in tests/test_x.py -> no hits")

    # --- i. bin/tool.py removing a non-assert line -> nothing ---
    diff_i = (
        "diff --git a/bin/tool.py b/bin/tool.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/bin/tool.py\n"
        "+++ b/bin/tool.py\n"
        "@@ -5,1 +5,1 @@\n"
        "-    x = 1\n"
        "+    x = 2\n"
    )
    check(scan(diff_i) == [], "i. non-assert line swap in bin/tool.py -> no hits")

    # --- j. tests/test_x.py reindent only -> nothing ---
    diff_j = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -8,1 +8,1 @@\n"
        "-    foo()\n"
        "+        foo()\n"
    )
    check(scan(diff_j) == [], "j. reindent-only change in tests/test_x.py (same hunk) -> no hits")

    # --- k. .md file removing a line containing 'assert' -> nothing ---
    diff_k = (
        "diff --git a/docs/notes.md b/docs/notes.md\n"
        "index 1111111..2222222 100644\n"
        "--- a/docs/notes.md\n"
        "+++ b/docs/notes.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-this has assert in prose\n"
        "+this line changed\n"
    )
    check(scan(diff_k) == [], "k. 'assert' in a removed .md line -> no hits (only .py counts)")

    # --- l. a removed '---' header line is never counted ---
    # Cases a-f all contain '--- a/<path>' / '+++ b/<path>' header lines and
    # already assert exact hit counts; a header miscounted as content would
    # inflate every one of them. Re-check explicitly against case a.
    check(
        not any(h["text"].strip() == "a/tests/test_x.py" for h in hits_a),
        "l. the '--- a/...' file header line is never itself reported as a hit",
    )

    # --- m. a malformed [review].test_globs fails loudly, naming the key and the value ---
    saved = os.environ.get("SESSION_NOTES_CONFIG")
    with tempfile.TemporaryDirectory() as td:
        cfgp = os.path.join(td, "c.toml")
        Path(cfgp).write_text('[review]\ntest_globs = "profile_data.py"\n')
        os.environ["SESSION_NOTES_CONFIG"] = cfgp
        try:
            r = subprocess.run([sys.executable, os.path.realpath(__file__)], input="",
                               capture_output=True, text=True)
            check(r.returncode == 1 and "review.test_globs" in r.stderr and "profile_data.py" in r.stderr,
                  "m1. a non-list test_globs exits 1 naming the key and the value")
            Path(cfgp).write_text('[review]\ntest_globs = ["profile_data.py"]\n')
            r = subprocess.run([sys.executable, os.path.realpath(__file__)], input=diff_f,
                               capture_output=True, text=True)
            check(r.returncode == 0 and "profile_data.py" in r.stdout,
                  "m2. a configured glob is applied by the CLI, and hits still exit 0")
        finally:
            if saved is None:
                os.environ.pop("SESSION_NOTES_CONFIG", None)
            else:
                os.environ["SESSION_NOTES_CONFIG"] = saved

    # === Round 2 fixes ===

    # --- n. a removed '-- foo'-shaped SQL comment line (diff-prefixed to
    #        '--- foo') must still count as content inside a hunk ---
    diff_n = (
        "diff --git a/tests/test_sql.py b/tests/test_sql.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/test_sql.py\n"
        "+++ b/tests/test_sql.py\n"
        "@@ -3,1 +3,1 @@\n"
        "--- comment removed\n"
        "+-- comment kept\n"
    )
    hits_n = scan(diff_n)
    check(
        len(hits_n) == 1 and hits_n[0]["text"] == "-- comment removed",
        "n. a removed SQL '-- comment' line (prefixed '---') still counts as hunk content",
    )

    # --- o. a removed '--- a/sneaky' line INSIDE a deleted file's hunk must
    #        not clobber the path taken from the real file header ---
    diff_o = (
        "diff --git a/tests/test_old2.py b/tests/test_old2.py\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/tests/test_old2.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "--- a/sneaky\n"
        "-assert True\n"
    )
    hits_o = scan(diff_o)
    check(
        len(hits_o) == 1 and hits_o[0]["file"] == "tests/test_old2.py",
        "o. a '--- a/sneaky'-shaped hunk line never overrides the real deleted-file path",
    )

    # --- p. a commented-out 'assert' is skipped; a commented-out 'check(' still fires ---
    diff_p_assert = (
        "diff --git a/bin/tool.py b/bin/tool.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/bin/tool.py\n"
        "+++ b/bin/tool.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-    # assert x == 3\n"
        "+    pass\n"
    )
    check(scan(diff_p_assert) == [], "p1. a removed commented-out 'assert' line -> no hit")

    diff_p_check = (
        "diff --git a/bin/tool.py b/bin/tool.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/bin/tool.py\n"
        "+++ b/bin/tool.py\n"
        "@@ -1,1 +1,1 @@\n"
        '-    # check("x", True)\n'
        "+    pass\n"
    )
    hits_p_check = scan(diff_p_check)
    check(
        len(hits_p_check) == 1 and hits_p_check[0]["reason"] == "assertion",
        "p2. a removed commented-out 'check(' line still fires (check() rule unchanged)",
    )

    # --- q. noise tiering: a net-growth test file collapses to one summary
    #        row; a net-shrink test file lists every removed line in full ---
    diff_q = (
        "diff --git a/tests/test_big.py b/tests/test_big.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/test_big.py\n"
        "+++ b/tests/test_big.py\n"
        "@@ -1,3 +1,5 @@\n"
        "-a\n"
        "-b\n"
        "-c\n"
        "+a2\n"
        "+b2\n"
        "+c2\n"
        "+d2\n"
        "+e2\n"
        "diff --git a/tests/test_shrink.py b/tests/test_shrink.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/test_shrink.py\n"
        "+++ b/tests/test_shrink.py\n"
        "@@ -1,3 +1,1 @@\n"
        "-x\n"
        "-y\n"
        "-z\n"
        "+x2\n"
    )
    hits_q = scan(diff_q)
    counts_q = file_counts(diff_q)
    comment_q = format_output(hits_q, counts_q)
    check(
        comment_q.count("tests/test_big.py:") == 1 and "3 removed / 5 added" in comment_q,
        "q1. net-growth test file collapses to exactly one summary row",
    )
    check(
        comment_q.count("tests/test_shrink.py:") == 3,
        "q2. net-shrink test file lists all three removed lines in full",
    )

    # --- s. format_output(hits, counts=None): no per-file counts available
    #        -> every hit listed in full, never collapsed to "0 lines changed" ---
    diff_s = (
        "diff --git a/tests/test_big.py b/tests/test_big.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/tests/test_big.py\n"
        "+++ b/tests/test_big.py\n"
        "@@ -1,3 +1,5 @@\n"
        "-a\n"
        "-b\n"
        "-c\n"
        "+a2\n"
        "+b2\n"
        "+c2\n"
        "+d2\n"
        "+e2\n"
    )
    hits_s = scan(diff_s)
    comment_s = format_output(hits_s)
    check(
        "lines changed" not in comment_s,
        "s1. format_output with counts=None never emits a collapsed 'N lines changed' row",
    )
    check(
        comment_s.count("tests/test_big.py:") == 3,
        "s2. format_output with counts=None lists all three removed lines in full",
    )

    # --- t. the report cap: more entries than the cap end in a "... N more" line ---
    many = "".join("-a%d\n" % i for i in range(_MAX_REPORT_LINES + 5)) + "+b\n"
    diff_t = ("diff --git a/tests/test_cap.py b/tests/test_cap.py\n--- a/tests/test_cap.py\n"
              "+++ b/tests/test_cap.py\n@@ -1,%d +1,1 @@\n" % (_MAX_REPORT_LINES + 5)) + many
    out_t = format_output(scan(diff_t))
    check("... 5 more" in out_t and out_t.count("test_cap.py:") == _MAX_REPORT_LINES,
          "t. the report is capped and says how many were left out")

    # --- u. a pure rename with no ---/+++ lines is seen, and a rename OUT of tests/ is a hit ---
    diff_u = (
        "diff --git a/tests/test_gate.py b/tests/gate.py.off\n"
        "similarity index 100%\n"
        "rename from tests/test_gate.py\n"
        "rename to tests/gate.py.off\n"
    )
    hits_u = scan(diff_u)
    check(len(hits_u) == 1 and hits_u[0]["reason"] == "test-file-renamed"
          and hits_u[0]["file"] == "tests/test_gate.py",
          "u1. a pure rename of a test file (no ---/+++ lines) is reported")
    diff_u2 = (
        "diff --git a/tests/test_gate.py b/lib/gate.py\n"
        "similarity index 80%\n"
        "rename from tests/test_gate.py\n"
        "rename to lib/gate.py\n"
        "--- a/tests/test_gate.py\n"
        "+++ b/lib/gate.py\n"
        "@@ -4,1 +4,1 @@\n"
        "-    assert ok\n"
        "+    pass\n"
    )
    hits_u2 = scan(diff_u2)
    check({h["reason"] for h in hits_u2} == {"test-file-renamed", "assertion"},
          "u2. a rename OUT of tests/ with edits reports the rename and the removed assert")
    diff_u3 = (
        "diff --git a/lib/a.py b/lib/b.py\nsimilarity index 100%\n"
        "rename from lib/a.py\nrename to lib/b.py\n"
    )
    check(scan(diff_u3) == [], "u3. a rename between non-test paths is not a hit")

    # --- v. binary fixtures and empty deleted files (no ---/+++ lines) ---
    diff_v = (
        "diff --git a/tests/fixtures/a.bin b/tests/fixtures/a.bin\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/tests/fixtures/a.bin and b/tests/fixtures/a.bin differ\n"
    )
    hits_v = scan(diff_v)
    check(len(hits_v) == 1 and hits_v[0]["reason"] == "test-file-binary"
          and hits_v[0]["file"] == "tests/fixtures/a.bin",
          "v1. a changed binary fixture is reported")
    diff_v2 = (
        "diff --git a/tests/test_empty.py b/tests/test_empty.py\n"
        "deleted file mode 100644\n"
        "index e69de29..0000000\n"
    )
    hits_v2 = scan(diff_v2)
    check(len(hits_v2) == 1 and hits_v2[0]["reason"] == "test-file-deleted"
          and hits_v2[0]["file"] == "tests/test_empty.py",
          "v2. a deleted empty test file is reported")
    diff_v3 = (
        "diff --git a/docs/img.png b/docs/img.png\nindex 1..2 100644\n"
        "Binary files a/docs/img.png and b/docs/img.png differ\n"
    )
    check(scan(diff_v3) == [], "v3. a changed binary file outside test paths is not a hit")

    # --- w. a changed assert in a test file is its own detail line, not a summary row ---
    diff_w = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
        "@@ -5,1 +5,1 @@\n"
        "-    assert out == [1, 2]\n"
        "+    assert out\n"
    )
    out_w = format_output(scan(diff_w), file_counts(diff_w))
    check("tests/test_x.py:5 [assertion]" in out_w and "lines changed" not in out_w,
          "w. a one-for-one assert swap in a test file shows as a detail line")

    # --- x. multi-line check( in a non-test file: continuation lines are flagged ---
    diff_x = (
        "diff --git a/bin/tool.py b/bin/tool.py\n"
        "--- a/bin/tool.py\n+++ b/bin/tool.py\n"
        "@@ -40,5 +40,5 @@\n"
        " def t():\n"
        '     check("drops short",\n'
        "-          out == [] and not err,\n"
        "+          True,\n"
        '           "detail")\n'
        "-    y = 2\n"
        "+    y = 3\n"
    )
    hits_x = scan(diff_x)
    check([h["line"] for h in hits_x] == [42],
          "x1. a loosened continuation line of a multi-line check( is flagged, and a removed "
          "line after the call closes is not")
    diff_x2 = (
        "diff --git a/bin/tool.py b/bin/tool.py\n"
        "--- a/bin/tool.py\n+++ b/bin/tool.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def f(a,\n"
        "-      b):\n"
        "+      c):\n"
        "     return 1\n"
    )
    check(scan(diff_x2) == [], "x2. a removed line in a multi-line non-check call is not flagged")

    # --- y. git C-quoted paths (non-ASCII names) are decoded, not skipped ---
    diff_y1 = (
        'diff --git "a/tests/fixtures/\\303\\251.bin" "b/tests/fixtures/\\303\\251.bin"\n'
        "index 1..2 100644\n"
        'Binary files "a/tests/fixtures/\\303\\251.bin" and "b/tests/fixtures/\\303\\251.bin" differ\n'
    )
    hits_y1 = scan(diff_y1)
    check(len(hits_y1) == 1 and hits_y1[0]["reason"] == "test-file-binary"
          and hits_y1[0]["file"] == "tests/fixtures/\u00e9.bin",
          "y1. a changed binary fixture with a quoted non-ASCII name is reported, decoded")
    diff_y2 = (
        'diff --git "a/tests/t\\303\\251st_q.py" "b/lib/q.py"\n'
        "similarity index 100%\n"
        'rename from "tests/t\\303\\251st_q.py"\n'
        "rename to lib/q.py\n"
    )
    hits_y2 = scan(diff_y2)
    check(len(hits_y2) == 1 and hits_y2[0]["file"] in ("lib/q.py", "tests/t\u00e9st_q.py"),
          "y2. a pure rename out of a quoted test path is reported")
    diff_y3 = (
        'diff --git "a/tests/t\\303\\251st.py" "b/tests/t\\303\\251st.py"\n'
        "index 1..2 100644\n"
        '--- "a/tests/t\\303\\251st.py"\n'
        '+++ "b/tests/t\\303\\251st.py"\n'
        "@@ -3,1 +3,1 @@\n"
        "-    assert a == 1\n"
        "+    assert a\n"
    )
    hits_y3 = scan(diff_y3)
    check(len(hits_y3) == 1 and hits_y3[0]["file"] == "tests/t\u00e9st.py"
          and hits_y3[0]["line"] == 3,
          "y3. a quoted ---/+++ path is decoded and the edit is reported at its line")
    check(_unquote_c('"a/x\\\\y\\"z\\tq\\nr"') == 'a/x\\y"z\tq\nr'
          and _unquote_c('"\\303\\251"') == "\u00e9" and _unquote_c("plain") == "plain",
          "y4. octal (UTF-8), backslash, quote, tab and newline escapes decode")
    diff_y5 = (
        'diff --git a/tests/plain.bin "b/tests/plain.bin"\n'
        "index 1..2 100644\n"
        "Binary files a/tests/plain.bin and b/tests/plain.bin differ\n"
    )
    check(len(scan(diff_y5)) == 1, "y5. a header with only the b side quoted still names the file")

    # --- z. a diff file with non-UTF-8 bytes is read with a warning, not a traceback ---
    import contextlib
    import io
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        pth = os.path.join(td, "bad.diff")
        with open(pth, "wb") as fh:
            fh.write(b"diff --git a/x b/x\n+caf\xe9\n")
        err_z = io.StringIO()
        with contextlib.redirect_stderr(err_z):
            txt_z = read_diff_file(pth)
        check("caf\ufffd" in txt_z and "not valid UTF-8" in err_z.getvalue(),
              "z1. --diff-file with a non-UTF-8 byte is decoded with replacement and warns")
        with open(pth, "wb") as fh:
            fh.write("caf\u00e9\n".encode("utf-8"))
        err_z = io.StringIO()
        with contextlib.redirect_stderr(err_z):
            txt_z = read_diff_file(pth)
        check(txt_z == "caf\u00e9\n" and err_z.getvalue() == "",
              "z2. a UTF-8 diff file reads silently")

    # --- z3. stdin with a non-UTF-8 byte is decoded with replacement and a warning ---
    saved_stdin = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(
        b"diff --git a/tests/test_s.py b/tests/test_s.py\n--- a/tests/test_s.py\n"
        b"+++ b/tests/test_s.py\n@@ -1,1 +1,1 @@\n-x = 'caf\xe9'\n+x = 2\n"), encoding="utf-8")
    err_s, out_s = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stderr(err_s), contextlib.redirect_stdout(out_s):
            rc_s = main(["--diff-file", "-"])
    finally:
        sys.stdin = saved_stdin
    check(rc_s == 0 and "stdin is not valid UTF-8" in err_s.getvalue()
          and "tests/test_s.py" in out_s.getvalue(),
          "z3. a non-UTF-8 byte on stdin warns and the hit is still reported")

    print("selftest:", "PASS" if ok else "FAIL")
    return ok


def _decode_diff(data, label):
    """Diff text from bytes. Bytes that are not UTF-8 are replaced, with a warning on stderr."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        print("test_edit_check: warning: %s is not valid UTF-8; undecodable bytes were replaced"
              % label, file=sys.stderr)
        return data.decode("utf-8", errors="replace")


def read_diff_file(path):
    """The diff file's text, decoded by _decode_diff."""
    return _decode_diff(Path(path).read_bytes(), path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scan a unified diff for edits to existing tests, fixtures and guards. "
                    "Reports only: exits 0 whether or not it finds hits.")
    ap.add_argument(
        "--diff-file",
        default="-",
        help="path to a diff file, or '-' to read from stdin (default)",
    )
    ap.add_argument("--selftest", action="store_true", help="run the built-in hermetic selftest")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 2

    try:
        globs = config_globs()
    except notes_config.ConfigError as exc:
        print("test_edit_check: config: %s" % exc, file=sys.stderr)
        return 1

    if args.diff_file == "-":
        diff_text = _decode_diff(sys.stdin.buffer.read(), "stdin")
    else:
        diff_text = read_diff_file(args.diff_file)

    hits, counts = _scan_blocks(diff_text, extra_globs=globs)
    if hits:
        print(format_output(hits, counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
