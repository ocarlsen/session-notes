#!/usr/bin/env python3
"""leak_scan.py -- refuse to commit anything that looks personal or private.

This repo was extracted from one person's working setup, so the failure it
guards against is specific: a name, an address, a path or an internal ticket
id riding along inside a comment. Two layers:

  1. PUBLIC patterns, in this file, that need no secret to describe: email
     addresses, phone numbers, home-directory paths, private-range and CGNAT
     IPs, API-key shapes, internal ticket ids, and the em dash (a house style
     rule, enforced here so it has one gate).
  2. PRIVATE terms, read from a file OUTSIDE the repo
     (~/.config/session-notes/leak-terms.txt, or $LEAK_TERMS). Names and
     project words cannot be listed in a public scanner without publishing
     them, so they live on the author's machine only. A plain line is a
     case-insensitive substring. A line starting with "w:" is a whole word,
     case-sensitive (for short names that occur inside ordinary words). A
     line starting with "!" is an allowed phrase: a term hit inside it is not
     a finding.

Without the private file the scan FAILS, unless LEAK_SCAN_PUBLIC_ONLY=1 is
set (CI sets it; it cannot know the private terms by design).

    python3 tests/leak_scan.py                # every tracked + untracked file
    python3 tests/leak_scan.py --staged       # what the next commit would hold
    python3 tests/leak_scan.py --commit-msg F # a commit message file
    python3 tests/leak_scan.py --selftest
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EM_DASH = chr(0x2014)

PUBLIC = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")),
    ("home path", re.compile(r"/Users/(?!you\b|example\b)[A-Za-z0-9._-]+|/home/(?!you\b|example\b)[A-Za-z0-9._-]+")),
    ("encoded home", re.compile(r"-Users-(?!you\b|example\b)[A-Za-z0-9]+")),
    ("cgnat ip", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
    ("private ip", re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("api key", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|\bsk-[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b"
                           r"|\bxox[abprs]-[A-Za-z0-9-]{8,}|\bgh[pousr]_[A-Za-z0-9]{20,}"
                           r"|\bAIza[0-9A-Za-z_-]{30,}")),
    ("ticket id", re.compile(r"\b(?:P?DR|D|T)-\d+\b|(?<![\w:.-])T\d{2,4}\b(?![:\d])")),
    ("em dash", re.compile(EM_DASH)),
]

# Addresses that are fine to publish.
EMAIL_OK = re.compile(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com|anthropic\.com)$")

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2"}


def terms_path():
    return os.path.expanduser(os.environ.get(
        "LEAK_TERMS", "~/.config/session-notes/leak-terms.txt"))


def load_terms(path=None):
    """(terms, allowed_phrases, word_patterns). Terms and phrases are
    lowercased; word patterns are compiled case-sensitive regexes. None if
    the file is missing."""
    path = path or terms_path()
    if not os.path.exists(path):
        return None
    terms, allow, words = [], [], []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n").strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("!"):
                allow.append(line[1:].strip().lower())
            elif line.startswith("w:"):
                w = line[2:].strip()
                if w:
                    words.append(re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(w)))
            else:
                terms.append(line.lower())
    return terms, allow, words


def _inside_allowed(low, start, end, allow):
    for phrase in allow:
        i = low.find(phrase)
        while i != -1:
            if i <= start and end <= i + len(phrase):
                return True
            i = low.find(phrase, i + 1)
    return False


def scan_text(text, private=None):
    """[(lineno, kind, snippet)] for one file's text."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in PUBLIC:
            for m in rx.finditer(line):
                if kind == "email" and EMAIL_OK.search(m.group(0)):
                    continue
                hits.append((n, kind, m.group(0)))
        if private:
            terms, allow = private[0], private[1]
            for rx in (private[2] if len(private) > 2 else []):
                for m in rx.finditer(line):
                    hits.append((n, "private word", m.group(0)))
            low = line.lower()
            for t in terms:
                i = low.find(t)
                while i != -1:
                    if not _inside_allowed(low, i, i + len(t), allow):
                        hits.append((n, "private term", line[i:i + len(t)]))
                        break
                    i = low.find(t, i + 1)
    return hits


def repo_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            # A linked worktree's ".git" is a file naming the main checkout's path: git
            # metadata, never committed, so it is skipped like the .git directory.
            if f == ".git" or os.path.splitext(f)[1].lower() in BINARY_EXT:
                continue
            out.append(os.path.relpath(os.path.join(dirpath, f), ROOT))
    return sorted(out)


def staged_files():
    r = subprocess.run(["git", "-C", ROOT, "diff", "--cached", "--name-only",
                        "--diff-filter=ACMR", "-z"], capture_output=True, text=True)
    return [f for f in r.stdout.split("\0") if f]


def staged_text(rel):
    r = subprocess.run(["git", "-C", ROOT, "show", ":" + rel], capture_output=True)
    return r.stdout.decode("utf-8", "replace")


def main(argv):
    if "--selftest" in argv:
        return selftest()
    private = load_terms()
    if private is None:
        if os.environ.get("LEAK_SCAN_PUBLIC_ONLY") == "1":
            print("leak_scan: private terms file absent; PUBLIC patterns only "
                  "(LEAK_SCAN_PUBLIC_ONLY=1)")
        else:
            print("leak_scan: FAIL, no private terms file at %s.\n"
                  "Create it (see this file's docstring), or set "
                  "LEAK_SCAN_PUBLIC_ONLY=1 in CI." % terms_path())
            return 2

    found = 0
    if "--commit-msg" in argv:
        path = argv[argv.index("--commit-msg") + 1]
        with open(path) as fh:
            text = "\n".join(l for l in fh.read().splitlines() if not l.startswith("#"))
        sources = [("commit message", text)]
    elif "--staged" in argv:
        sources = [(f, staged_text(f)) for f in staged_files()
                   if os.path.splitext(f)[1].lower() not in BINARY_EXT]
    else:
        sources = []
        for f in repo_files():
            with open(os.path.join(ROOT, f), "rb") as fh:
                sources.append((f, fh.read().decode("utf-8", "replace")))

    for name, text in sources:
        for n, kind, snip in scan_text(text, private):
            print("%s:%d: %s: %r" % (name, n, kind, snip))
            found += 1
    if found:
        print("leak_scan: FAIL, %d finding(s) in %d source(s)" % (found, len(sources)))
        return 1
    print("leak_scan: clean (%d source(s)%s)"
          % (len(sources), "" if private else ", public patterns only"))
    return 0


def selftest():
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    kinds = lambda t, p=None: [k for _, k, _ in scan_text(t, p)]
    check("email caught", kinds("mail bob" + "@corp.io now") == ["email"])
    check("example email ok", kinds("you@example.com") == [])
    check("noreply ok", kinds("x@users.noreply.github.com") == [])
    check("phone caught", "phone" in kinds("call (555) " + "123-4567"))
    check("home path caught", "home path" in kinds("/Users" + "/alice/x"))
    check("placeholder home ok", kinds("/Users/you/x") == [])
    check("encoded home caught", "encoded home" in kinds("projects/-Users" + "-alice-x"))
    check("cgnat caught", "cgnat ip" in kinds("http://100." + "101.2.3:8000"))
    check("private ip caught", "private ip" in kinds("192." + "168.1.10"))
    check("api key caught", "api key" in kinds("sk-" + "ant-api03-abcdefghijk"))
    check("ticket caught", "ticket id" in kinds("see " + "DR" + "-1234"))
    check("bare T-number caught", "ticket id" in kinds("closed " + "T" + "29 today"))
    check("iso time not a ticket", "ticket id" not in kinds("2026-01-01T12:00:00"))
    check("em dash caught", "em dash" in kinds("a " + EM_DASH + " b"))
    check("version not ip", kinds("v2.1.199") == [])
    p = (["zork"], ["zorkian heights"])
    check("private term caught", kinds("Hello Zork", p) == ["private term"])
    check("allowed phrase ok", kinds("the Zorkian Heights way", p) == [])
    check("term outside phrase caught",
          kinds("zork and zorkian heights", p) == ["private term"])
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("# c\nzork\nw:Qux\n!zorkian heights\n")
    loaded = load_terms(fh.name)
    os.unlink(fh.name)
    check("word term loaded", len(loaded[2]) == 1)
    check("word term caught", kinds("met Qux today", loaded) == ["private word"])
    check("word term inside word ok", kinds("a Quxote quxly sQux", loaded) == [])
    check("word term is case-sensitive", kinds("qux", loaded) == [])
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, ".git"), "w") as fh:
            fh.write("gitdir: /Users" + "/alice/repo/.git/worktrees/x\n")
        global ROOT
        saved, ROOT = ROOT, d
        try:
            check("worktree .git file skipped", ".git" not in repo_files())
        finally:
            ROOT = saved
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
