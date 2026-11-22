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

Exit codes: 0 always for a successful scan, whether or not there are hits; 1 for a usage error
or a malformed config value (the message names the key and value); 2 for --selftest failure.
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


def _strip_prefix(p):
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def _block_path(header_lines):
    """File path for a block, from its header region ONLY (see
    _split_header_and_hunks for why that restriction matters)."""
    b_path = None
    a_path = None
    for line in header_lines:
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p != "/dev/null":
                b_path = _strip_prefix(p)
        elif line.startswith("--- "):
            p = line[4:].strip()
            if p != "/dev/null":
                a_path = _strip_prefix(p)
    return b_path or a_path


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


def _process_block(path, is_new, is_deleted, hunk_lines, extra_globs):
    hits = []
    is_test = _is_test_file(path, extra_globs)
    is_py = path.endswith(".py")

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
        for line in hunk["lines"]:
            if line.startswith("\\"):
                continue  # "\ No newline at end of file" is not a real line
            if line.startswith("+"):
                continue  # added lines don't advance the old-side counter
            if line.startswith("-"):
                text = line[1:]
                stripped = text.strip()
                cur_old_line = old_line
                old_line += 1
                if stripped == "":
                    continue  # blank removed lines are not a finding
                if stripped in added_stripped:
                    continue  # reindent/move within the same hunk
                if is_test:
                    hits.append(
                        {"file": path, "line": cur_old_line, "text": text, "reason": "test-file"}
                    )
                elif is_py:
                    if "check(" in stripped:
                        hits.append(
                            {"file": path, "line": cur_old_line, "text": text, "reason": "assertion"}
                        )
                    elif not stripped.startswith("#") and ASSERT_RE.search(stripped):
                        hits.append(
                            {"file": path, "line": cur_old_line, "text": text, "reason": "assertion"}
                        )
            else:
                old_line += 1  # context line
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
        path = _block_path(header)
        if not path:
            continue
        is_new = any(line.startswith("new file mode") for line in header)
        is_deleted = any(line.startswith("deleted file mode") for line in header)
        added, removed = _count_lines(hunk_lines)
        counts[path] = {"added": added, "removed": removed}
        hits.extend(_process_block(path, is_new, is_deleted, hunk_lines, extra_globs))
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
        if h["reason"] in ("assertion", "test-file-deleted"):
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

    print("selftest:", "PASS" if ok else "FAIL")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scan a unified diff for edits to existing tests, fixtures and guards. "
                    "Reports only: exits 0 whether or not it finds hits.")
    ap.add_argument("--repo-root", default=None,
                    help="the repo the diff is from (diff paths are repo-relative, so this only "
                         "documents intent; extra globs come from config [review].test_globs)")
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
        diff_text = sys.stdin.read()
    else:
        diff_text = Path(args.diff_file).read_text()

    hits, counts = _scan_blocks(diff_text, extra_globs=globs)
    if hits:
        print(format_output(hits, counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
