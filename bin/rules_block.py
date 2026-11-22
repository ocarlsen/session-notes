#!/usr/bin/env python3
"""rules_block.py -- put the standing rules (rules/core.md) in front of both agents.

Codex has no @import, so the rules are written into a MARKED block in ~/.codex/AGENTS.md
and the block is rewritten on every install: a user who installed an older snippet gets the
newer sections too. Claude Code gets @import lines in ~/.claude/CLAUDE.md instead.

The block holds, in order: AGENTS.snippet.md, rules/core.md, and the user's private addendum
(config key rules.local, default ~/.config/session-notes/rules.local.md) if that file exists.

    rules_block.py render [--agent codex|claude]      print the block (or the Claude lines)
    rules_block.py apply --file PATH [--dry-run]      rewrite the block in PATH
    rules_block.py claude-lines                       the @import lines for a CLAUDE.md
    rules_block.py claude-apply --file PATH [--dry-run]
    rules_block.py codex-defaults --config PATH [--dry-run]   set model + effort if unset
    rules_block.py profiles --dir CODEX_DIR [--dry-run]       create build/review profiles
    rules_block.py --selftest

`apply` cases: (a) the markers are present: replace everything between them, keep every other
byte; (b) no markers but an old install of the snippet: drop each section whose heading is one
of the snippet's headings and put the block where the first one was; (c) neither: append the
block. The file is created if missing, a copy named PATH.bak-session-notes-YYYYmmdd-HHMMSS is kept
before any change (an existing backup is never overwritten), and a second run changes nothing.
Codex caps the combined size of the global and project AGENTS.md files at 32 KiB, so a result
over 32768 bytes is refused.
"""
import argparse
import difflib
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

BEGIN = "<!-- session-notes:begin (install.sh rewrites this block; edit outside it) -->"
END = "<!-- session-notes:end -->"
ADDENDUM_HEADING = "## Local addendum (private rules)"
CLAUDE_HEADING = "## Standing rules (session-notes)"
BAK = ".bak-session-notes"   # prefix; the backup name adds a timestamp
MAX_BYTES = 32768   # Codex project_doc_max_bytes default
WARN_BYTES = 28000
BLOCK_WARN_BYTES = 12000
# Lines of the snippet in versions older than the current one: a legacy section is removed only
# while its lines are known wording, so text the user wrote after it survives.
OLD_WORDING = (
    "`session_bus.py send` fails, tell the user what you wanted to send instead of finding",
    "another way to reach that session.",
)
CORE = os.path.join("rules", "core.md")
REVIEW = os.path.join("rules", "review-contract.md")

# Codex defaults and profiles, defined once. Profiles are layered with `codex -p NAME`.
DEFAULT_MODEL = "gpt-6.2-astra"
DEFAULT_EFFORT = "medium"
PROFILES = {
    "build": ("gpt-6.2-sol", "medium", "writes the code (codex -p build)"),
    "review": ("gpt-6.2-astra", "high", "refutes a change with fresh context (codex -p review)"),
}


class RulesError(Exception):
    pass


def split_lines(text):
    """Lines with their endings, split on \\n only (str.splitlines also splits on U+2028,
    form feed and others, which TOML and Markdown do not treat as line breaks)."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def tilde(path):
    home = os.path.expanduser("~")
    if path == home or path.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home):]
    return path


def read_text(path):
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise RulesError("cannot read %s: %s" % (path, exc))


def addendum_path(local=None):
    if local is not None:
        return os.path.expanduser(local) if local else ""
    import notes_config
    try:
        return notes_config.load()["rules"]["local"]
    except notes_config.ConfigError as exc:
        raise RulesError("config: %s" % exc)


def ensure_nl(text):
    return text if text.endswith("\n") else text + "\n"


SEP = "\n---\n\n"


def render(repo=REPO, local=None):
    snippet = read_text(os.path.join(repo, "AGENTS.snippet.md"))
    docs = []
    for rel in (CORE, REVIEW):
        path = os.path.join(repo, rel)
        if not os.path.exists(path):
            raise RulesError("missing %s" % path)
        docs.append(ensure_nl(read_text(path)))
    parts = [BEGIN + "\n", ensure_nl(snippet), SEP.join([""] + docs)]
    add = addendum_path(local)
    if add and os.path.exists(add):
        text = read_text(add)
        if any(ln.rstrip("\r\n") in (BEGIN, END) for ln in split_lines(text)):
            raise RulesError("%s contains a session-notes marker line; remove it" % tilde(add))
        parts += ["\n", ADDENDUM_HEADING + "\n\n", ensure_nl(text)]
    parts.append(END + "\n")
    return "".join(parts)


def snippet_headings(repo=REPO):
    text = read_text(os.path.join(repo, "AGENTS.snippet.md"))
    return {ln.rstrip("\r\n") for ln in split_lines(text) if ln.startswith("## ")}


def known_lines(repo=REPO):
    text = read_text(os.path.join(repo, "AGENTS.snippet.md"))
    return {ln.strip() for ln in split_lines(text) if ln.strip()} | set(OLD_WORDING)


def compute(old, block, headings, known=()):
    """The new file text. old is None for a missing file."""
    if old is None:
        return block
    lines = split_lines(old)
    bare = [ln.rstrip("\r\n") for ln in lines]
    nb, ne = bare.count(BEGIN), bare.count(END)
    if nb or ne:
        if nb != 1 or ne != 1:
            raise RulesError("expected exactly one begin and one end marker, found %d begin and "
                             "%d end; fix the file by hand" % (nb, ne))
        b, e = bare.index(BEGIN), bare.index(END)
        if e < b:
            raise RulesError("the end marker comes before the begin marker; fix the file by hand")
        return "".join(lines[:b]) + block + "".join(lines[e + 1:])
    # A legacy section is its heading plus the lines after it that are known snippet wording
    # (blank lines too). It ends at the next `# ` or `## ` heading, or at the first line that is
    # not known wording: that line and everything after it belongs to the user.
    def legacy_heading(i):
        """A snippet heading is ours only if known wording follows (or nothing does): a heading
        followed directly by the user's own text keeps its heading and body."""
        if bare[i] not in headings:
            return False
        for j in range(i + 1, len(lines)):
            t = bare[j].strip()
            if t:
                return _is_heading(bare[j]) or t in known
        return True

    out, skipping, at = [], False, None
    for i, (ln, s) in enumerate(zip(lines, bare)):
        if _is_heading(s):
            skipping = legacy_heading(i)
            if skipping:
                if at is None:
                    at = len(out)
                continue
        elif skipping:
            if not s.strip() or s.strip() in known:
                continue
            skipping = False
        out.append(ln)
    if at is not None:
        before, after = out[:at], out[at:]
        if before and before[-1].strip():
            before.append("\n")
        tail = ["\n"] if after and after[0].strip() else []
        return "".join(before) + block + "".join(tail) + "".join(after)
    if not old:
        return block
    text = ensure_nl(old)
    if text.strip():
        text += "\n"
    return text + block


def diff_text(path, old, new):
    return "".join(difflib.unified_diff(
        split_lines(old or ""), split_lines(new),
        fromfile=path if old is not None else "/dev/null", tofile=path + " (new)"))


def backup_path(path):
    """PATH.bak-session-notes-YYYYmmdd-HHMMSS (local time); on a same-second clash the next
    number after every existing one (-1, -2, ...), so a pruned name is never reused out of
    order. Never a name that exists."""
    base = "%s%s-%s" % (path, BAK, time.strftime("%Y%m%d-%H%M%S"))
    d, name = os.path.split(base)
    top = -1
    for other in os.listdir(d or "."):
        if other == name:
            top = max(top, 0)
        elif other.startswith(name + "-") and other[len(name) + 1:].isdigit():
            top = max(top, int(other[len(name) + 1:]))
    return base if top < 0 else "%s-%d" % (base, top + 1)


KEEP_BACKUPS = 3


def prune_backups(path):
    """Keep the newest KEEP_BACKUPS of this tool's own timestamped backups of PATH. Only names
    of exactly that pattern are touched; install.sh's PATH.bak-session-notes never is."""
    d, base = os.path.split(path)
    pat = re.compile(r"^%s%s-(\d{8}-\d{6})(?:-(\d+))?$" % (re.escape(base), re.escape(BAK)))
    found = []
    for name in os.listdir(d or "."):
        m = pat.match(name)
        if m:
            found.append(((m.group(1), int(m.group(2) or 0)), name))
    for _, name in sorted(found)[:-KEEP_BACKUPS]:
        os.unlink(os.path.join(d or ".", name))


def write_with_backup(path, new, old):
    """Write new to path; returns the backup's path, or None for a file that did not exist."""
    target = os.path.realpath(path)
    bak = None
    if old is not None:
        bak = backup_path(path)
        shutil.copy2(target, bak)
        prune_backups(path)
    d = os.path.dirname(target) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".rules_block.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
        if os.path.exists(target):
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return bak


def finish(path, old, new, dry, label):
    """Shared tail: unchanged / diff / write, for every file this tool edits."""
    if old == new:
        print("unchanged: %s" % tilde(path))
        return 0
    if dry:
        sys.stdout.write(diff_text(tilde(path), old, new))
        return 0
    bak = write_with_backup(path, new, old)
    print("%s %s%s" % (label, tilde(path), " (copy kept as %s)" % tilde(bak) if bak else ""))
    return 0


def load_existing(path):
    return read_text(path) if os.path.exists(path) else None


def cmd_apply(path, dry, repo=REPO, local=None):
    old = load_existing(path)
    block = render(repo, local)
    new = compute(old, block, snippet_headings(repo), known_lines(repo))
    size = len(new.encode("utf-8"))
    if size > MAX_BYTES:
        raise RulesError("%s would be %d bytes; Codex caps the combined size of the global and "
                         "project AGENTS.md files at 32 KiB (%d bytes). Trim the file, or the "
                         "addendum, and run again" % (tilde(path), size, MAX_BYTES))
    if len(block.encode("utf-8")) > BLOCK_WARN_BYTES:
        print("warning: the session-notes block alone is %d bytes (over %d); it uses much of the "
              "Codex budget" % (len(block.encode("utf-8")), BLOCK_WARN_BYTES), file=sys.stderr)
    if size > WARN_BYTES:
        print("warning: %s is %d bytes, close to the %d Codex reads" % (
            tilde(path), size, MAX_BYTES), file=sys.stderr)
    return finish(path, old, new, dry, "wrote")


def claude_targets(repo=REPO, local=None):
    """[(line, target)]: the @import lines a CLAUDE.md needs and the real file each names."""
    out = [("@%s" % tilde(os.path.join(repo, rel)), os.path.join(repo, rel)) for rel in (CORE, REVIEW)]
    add = addendum_path(local)
    if add and os.path.exists(add):
        read_text(add)  # an unreadable addendum is loud here too
        out.append(("@%s" % tilde(add), add))
    return out


def claude_lines(repo=REPO, local=None):
    return [line for line, _ in claude_targets(repo, local)]


def _fence_flags(lines):
    """flags[i] is True for a line inside (or opening or closing) a ``` or ~~~ fenced block."""
    flags, fence = [], None
    for ln in lines:
        body = ln.lstrip()
        if fence is None:
            if body.startswith("```") or body.startswith("~~~"):
                fence = body[0] * 3
                flags.append(True)
                continue
            flags.append(False)
        else:
            flags.append(True)
            if body.startswith(fence):
                fence = None
    return flags


def _is_heading(s):
    return s.startswith("# ") or s.startswith("## ")


def cmd_claude_apply(path, dry, repo=REPO, local=None):
    """Adds the missing lines. Outside our own `## Standing rules` section it never edits or
    removes a line; inside it, a stale line (our rules file, no longer on disk) is replaced."""
    old = load_existing(path)
    lines = split_lines(old or "")
    here = os.path.dirname(os.path.abspath(path))
    notes = []

    def target_of(line):
        b = line.strip()
        if b.startswith("@") and len(b) > 1:
            p = os.path.expanduser(b[1:])
            return p if os.path.isabs(p) else os.path.join(here, p)
        return None

    # Stale lines inside our section: ours (they name our rules files) and dangling.
    fenced = _fence_flags(lines)
    bare = [ln.rstrip("\r\n") for ln in lines]
    heads = [i for i, b in enumerate(bare) if b == CLAUDE_HEADING and not fenced[i]]
    if heads:
        end = next((i for i in range(heads[0] + 1, len(lines))
                    if not fenced[i] and _is_heading(bare[i])), len(lines))
        drop = []
        for i in range(heads[0] + 1, end):
            t = target_of(lines[i])
            if (t and not fenced[i] and t.endswith((CORE, REVIEW)) and not os.path.exists(t)):
                notes.append("removed stale line %s (that file is gone)" % lines[i].strip())
                drop.append(i)
        lines = [ln for i, ln in enumerate(lines) if i not in drop]
    fenced = _fence_flags(lines)
    imported = set()   # real files the existing @ lines point at, resolved the way Claude does
    for i, ln in enumerate(lines):
        t = target_of(ln)
        if t and not fenced[i]:
            imported.add(os.path.realpath(t))
    missing = [line for line, target in claude_targets(repo, local)
               if os.path.realpath(target) not in imported]
    if not missing and not notes:
        print("unchanged: %s" % tilde(path))
        return 0
    for n in notes:
        print(n)
    bare = [ln.rstrip("\r\n") for ln in lines]
    heads = [i for i, b in enumerate(bare) if b == CLAUDE_HEADING and not fenced[i]]
    if not missing:
        new = "".join(lines)
    elif heads:
        i = heads[0] + 1
        while i < len(lines) and not (not fenced[i] and bare[i].startswith("## ")):
            i += 1
        while i > 0 and not bare[i - 1].strip():
            i -= 1   # after the section's last non-blank line
        head = lines[:i]
        if head and not head[-1].endswith("\n"):
            head[-1] += "\n"
        new = "".join(head) + "".join(m + "\n" for m in missing) + "".join(lines[i:])
    else:
        text = ensure_nl("".join(lines)) if lines else ""
        if text.strip():
            text += "\n"
        new = text + CLAUDE_HEADING + "\n" + "".join(m + "\n" for m in missing)
    return finish(path, old, new, dry, "added the rules to")


_KEY_RE = re.compile(r"""^(?:([A-Za-z0-9_-]+)|"([^"\\]*)"|'([^']*)')\s*(?:=|\.)""")


def _quote_run(ln, i, q):
    # Length of the closing delimiter at i: three quotes, plus up to two more that belong to
    # the string's content (a string x" closes as x followed by four quote characters).
    n = 0
    while i + n < len(ln) and ln[i + n] == q:
        n += 1
    return n if n >= 3 else 3


def scan_toml(lines):
    """(index of the first table header, set of top-level key names before it, clean) where
    clean[i] says line i starts at depth 0 outside any string. A small line
    scanner: it tracks multi-line strings and bracket depth, ignoring brackets in strings and
    comments, so only a real header at depth 0 outside a string counts. Raises RulesError when it
    cannot be sure (unterminated string, brackets that never balance)."""
    state, depth, keys, clean = None, 0, set(), []
    for idx, ln in enumerate(lines):
        clean.append(state is None and depth == 0)
        if state is None and depth == 0:
            body = ln.lstrip()
            if body.startswith("["):
                return idx, keys, clean
            m = _KEY_RE.match(body)
            if m:
                keys.add(next(g for g in m.groups() if g is not None))
        i, n = 0, len(ln)
        while i < n:
            c = ln[i]
            if state == '"""':
                if c == "\\":
                    i += 2
                elif ln.startswith('"""', i):
                    state, i = None, i + _quote_run(ln, i, '"')
                else:
                    i += 1
            elif state == "'''":
                if ln.startswith("'''", i):
                    state, i = None, i + _quote_run(ln, i, "'")
                else:
                    i += 1
            elif c == "#":
                break
            elif ln.startswith('"""', i):
                state, i = '"""', i + 3
            elif ln.startswith("'''", i):
                state, i = "'''", i + 3
            elif c == '"':
                j = i + 1
                while j < n and ln[j] != '"':
                    j += 2 if ln[j] == "\\" else 1
                if j >= n:
                    raise RulesError("config.toml line %d: unterminated string; fix it by hand" % (idx + 1))
                i = j + 1
            elif c == "'":
                j = ln.find("'", i + 1)
                if j < 0:
                    raise RulesError("config.toml line %d: unterminated string; fix it by hand" % (idx + 1))
                i = j + 1
            else:
                if c in "[{":
                    depth += 1
                elif c in "]}":
                    depth -= 1
                    if depth < 0:
                        raise RulesError("config.toml line %d: unbalanced bracket; fix it by hand" % (idx + 1))
                i += 1
    if state or depth:
        raise RulesError("config.toml: a string or bracket is never closed, so I cannot tell where "
                         "the top-level keys end; fix it by hand")
    return len(lines), keys, clean


def codex_defaults(old):
    """config.toml text with top-level model + model_reasoning_effort added if unset.
    Top-level keys must come before the first [table] line."""
    lines = split_lines(old or "")
    first, keys, clean = scan_toml(lines)
    add = [ln for key, ln in (
        ("model", 'model = "%s"\n' % DEFAULT_MODEL),
        ("model_reasoning_effort", 'model_reasoning_effort = "%s"\n' % DEFAULT_EFFORT))
        if key not in keys]
    if not add:
        return old
    # A comment block right above the first table belongs to that table: go above it.
    at = first
    # Only lines the scanner saw at depth 0 outside a string may be stepped over.
    while at > 0 and clean[at - 1] and (
            not lines[at - 1].strip() or lines[at - 1].lstrip().startswith("#")):
        at -= 1
    before, after = lines[:at], lines[at:]
    if before and not before[-1].endswith("\n"):
        before[-1] += "\n"
    new = before + ["# session-notes defaults (change freely)\n"] + add
    if after and after[0].strip():
        new.append("\n")
    return "".join(new + after)


def validate_defaults(text, added):
    """With tomllib (Python 3.11+): the result must parse and carry the values we set."""
    try:
        import tomllib
    except ImportError:
        return
    try:
        data = tomllib.loads(text)
    except Exception as exc:
        raise RulesError("the edited config.toml would not parse (%s); nothing written" % exc)
    for key, want in (("model", DEFAULT_MODEL), ("model_reasoning_effort", DEFAULT_EFFORT)):
        if key in added and data.get(key) != want:
            raise RulesError("the edited config.toml would not have top-level %s = %r; nothing "
                             "written" % (key, want))


def cmd_codex_defaults(path, dry):
    old = load_existing(path)
    new = codex_defaults(old)
    if new is not None and new != old:
        _, before, _ = scan_toml(split_lines(old or ""))
        validate_defaults(new, {k for k in ("model", "model_reasoning_effort") if k not in before})
    return finish(path, old, new if new is not None else old, dry, "set defaults in")


def profile_text(name):
    model, effort, why = PROFILES[name]
    return '# Codex profile: %s\nmodel = "%s"\nmodel_reasoning_effort = "%s"\n' % (why, model, effort)


def cmd_profiles(codex_dir, dry):
    todo = [n for n in PROFILES if not os.path.exists(os.path.join(codex_dir, n + ".config.toml"))]
    if not todo:
        print("unchanged: build and review profiles already exist in %s" % tilde(codex_dir))
        return 0
    for n in todo:
        path = os.path.join(codex_dir, n + ".config.toml")
        if dry:
            print("would create %s:" % tilde(path))
            sys.stdout.write("".join("  " + ln for ln in profile_text(n).splitlines(True)))
        else:
            os.makedirs(codex_dir, exist_ok=True)
            with open(path, "x", encoding="utf-8") as fh:  # never overwrites
                fh.write(profile_text(n))
            print("created %s" % tilde(path))
    return 0


def selftest():
    import contextlib
    import io
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    def refuses(fn):
        try:
            fn()
        except RulesError:
            return True
        return False

    def quiet(fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = fn()
        return rc, buf.getvalue()

    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "rules"))
        with open(os.path.join(repo, "AGENTS.snippet.md"), "w") as fh:
            fh.write("## session-notes memory\n\nmem line\n\n## Session tools (session-notes)\n"
                     "- tools line\n\n## Codex and other sessions\n- codex line\n")
        with open(os.path.join(repo, "rules", "core.md"), "w") as fh:
            fh.write("# Standing rules\n\n## Rule one\n- do it\n")
        with open(os.path.join(repo, "rules", "review-contract.md"), "w") as fh:
            fh.write("# Review contract\n\n## Verdict\n- say APPROVE\n")
        local = os.path.join(tmp, "none.md")
        agents = os.path.join(tmp, "AGENTS.md")
        import glob

        def baks(p):
            return sorted(glob.glob(p + BAK + "-*"))

        def apply(path=agents, dry=False, loc=local):
            return quiet(lambda: cmd_apply(path, dry, repo, loc))

        # (c) missing file is created
        rc, out = apply()
        text = open(agents).read()
        check("missing file created with one block", rc == 0 and text.count(BEGIN) == 1
              and text.count(END) == 1 and "## Rule one" in text and not baks(agents))
        # idempotent: second run is a no-op and writes no .bak
        rc, out = apply()
        check("second apply unchanged, no .bak", "unchanged" in out and not baks(agents)
              and open(agents).read() == text)

        # (c) append to a file with no markers and no legacy sections
        mine = "# My notes\n\n## Uploads\nkeep me\n"
        open(agents, "w").write(mine)
        apply()
        text = open(agents).read()
        check("append keeps content, one blank line", text.startswith(mine + "\n" + BEGIN)
              and len(baks(agents)) == 1 and open(baks(agents)[0]).read() == mine)
        # (a) markers: only the block changes, outside bytes are untouched
        pre, post = "top line\n\n", "\n## Uploads\nuser text, no newline at end"
        open(agents, "w").write(pre + BEGIN + "\nOLD STUFF\n" + END + "\n" + post)
        apply()
        text = open(agents).read()
        check("markers replaced, outside byte-for-byte", text.startswith(pre + BEGIN)
              and text.endswith(END + "\n" + post) and "OLD STUFF" not in text
              and "## Rule one" in text)

        # (b) the real legacy case: old first section + the user's own Uploads section
        legacy = ("## session-notes memory\n\nmem line\n\n"
                  "## Uploads and files\nMy own hand-written rules.\nSecond line.\n")
        open(agents, "w").write(legacy)
        apply()
        text = open(agents).read()
        check("legacy: old section gone (only the block's copy remains)", text.count("mem line") == 1)
        check("legacy: block at the old position", text.startswith(BEGIN))
        check("legacy: later snippet sections present", "## Session tools (session-notes)" in text
              and "## Codex and other sessions" in text)
        check("legacy: user's Uploads section untouched",
              text.endswith(END + "\n\n## Uploads and files\nMy own hand-written rules.\nSecond line.\n"))
        before = open(agents).read()
        rc, out = apply()
        check("legacy: second apply unchanged", "unchanged" in out and open(agents).read() == before)

        # a legacy section in the middle: block goes where the first removed one was
        open(agents, "w").write("# Head\n\n## Codex and other sessions\n- codex line\n\n## Mine\nx\n"
                                "## Session tools (session-notes)\n- tools line\n")
        apply()
        text = open(agents).read()
        check("legacy middle: block at first removed section, both removed",
              text.startswith("# Head\n\n" + BEGIN) and text.count("tools line") == 1
              and text.endswith(END + "\n\n## Mine\nx\n"))

        # B1: the user's own text after the old section survives (H1 section, plain text)
        french = ("# Global\nintro\n\n## session-notes memory\n\nmem line\n\n# Work rules\n"
                  "- never push to main\n### details\n- more\n\nAlways answer in French.\n")
        open(agents, "w").write(french)
        apply()
        text = open(agents).read()
        check("legacy: H1 section and plain text after the old section survive",
              "# Work rules\n- never push to main\n### details\n- more\n\nAlways answer in French.\n"
              in text and "mem line" not in text.split(BEGIN)[0] and text.startswith("# Global\nintro\n\n" + BEGIN)
              and text.count("Always answer in French.") == 1)
        open(agents, "w").write("## session-notes memory\n\nmem line\nAlways answer in French.\n- tools line\n")
        apply()
        text = open(agents).read()
        check("legacy: first unknown line ends the removal, rest kept verbatim",
              text.endswith(END + "\n\nAlways answer in French.\n- tools line\n"))

        # B1b: backups are timestamped and never overwritten
        bk = os.path.join(tmp, "BK.md")
        open(bk, "w").write("one\n")
        apply(path=bk)
        open(bk, "w").write("two\n")
        apply(path=bk)
        got = [open(b).read() for b in baks(bk)]
        check("backups: two changed writes keep two distinct backups", sorted(got) == ["one\n", "two\n"]
              and len(set(baks(bk))) == 2)

        # N8/N9/N4
        evil = os.path.join(tmp, "evil.md")
        open(evil, "w").write("- x\n" + END + "\n- y\n")
        check("addendum with a marker line is refused", refuses(lambda: render(repo, evil)))
        check("empty local path means no addendum", ADDENDUM_HEADING not in render(repo, ""))
        import notes_config
        cfgp = os.path.join(tmp, "rules.toml")
        open(cfgp, "w").write('[rules]\nlocal = ""\n')
        check("config rules.local = empty is accepted as none",
              notes_config.load(cfgp)["rules"]["local"] == "")
        rtext = render(repo, local)
        check("review contract inlined after core with a separator",
              "\n---\n\n# Review contract" in rtext
              and rtext.index("# Standing rules") < rtext.index("# Review contract"))
        big2 = os.path.join(tmp, "big2.md")
        open(big2, "w").write("x" * (MAX_BYTES + 10) + "\n")
        try:
            cmd_apply(big2, False, repo, local)
            msg = ""
        except RulesError as exc:
            msg = str(exc)
        check("oversize message names the combined 32 KiB cap", "combined size" in msg and "32 KiB" in msg)

        # duplicate markers refuse
        open(agents, "w").write(BEGIN + "\na\n" + END + "\n" + BEGIN + "\nb\n" + END + "\n")
        dup = open(agents).read()
        check("duplicate markers refused", refuses(lambda: cmd_apply(agents, False, repo, local))
              and open(agents).read() == dup)
        open(agents, "w").write(BEGIN + "\nonly begin\n")
        check("lone marker refused", refuses(lambda: cmd_apply(agents, False, repo, local)))

        # size refusal and warning
        big = os.path.join(tmp, "big.md")
        open(big, "w").write("x" * (MAX_BYTES + 10) + "\n")
        check("oversize refused, file untouched", refuses(lambda: cmd_apply(big, False, repo, local))
              and os.path.getsize(big) == MAX_BYTES + 11)
        mid = os.path.join(tmp, "mid.md")
        open(mid, "w").write("y" * 28000 + "\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
            cmd_apply(mid, False, repo, local)
        check("warning above 28000 but still written", "warning" in buf.getvalue()
              and BEGIN in open(mid).read())

        # dry-run writes nothing
        fresh = os.path.join(tmp, "fresh.md")
        rc, out = apply(path=fresh, dry=True)
        check("dry-run on missing file: diff, nothing written", BEGIN in out
              and not os.path.exists(fresh))
        open(agents, "w").write(legacy)
        rc, out = apply(dry=True)
        check("dry-run on existing file: diff, file and .bak untouched",
              "+" + BEGIN in out and open(agents).read() == legacy)

        # addendum present / absent
        add = os.path.join(tmp, "rules.local.md")
        open(add, "w").write("- my private rule\n")
        text = render(repo, add)
        check("addendum included under its heading", ADDENDUM_HEADING in text
              and "my private rule" in text and text.index("## Rule one") < text.index("my private"))
        check("addendum absent is silent", "my private" not in render(repo, local)
              and ADDENDUM_HEADING not in render(repo, local))
        os.mkdir(os.path.join(tmp, "isdir.md"))
        check("unreadable addendum is loud", refuses(lambda: render(repo, os.path.join(tmp, "isdir.md"))))

        # claude lines + claude-apply
        lines = claude_lines(repo, local)
        check("claude-lines: core and review contract without addendum", len(lines) == 2
              and lines[0].endswith("rules/core.md") and lines[0].startswith("@")
              and lines[1].endswith("rules/review-contract.md"))
        check("claude-lines: addendum added when present", len(claude_lines(repo, add)) == 3)
        cm = os.path.join(tmp, "CLAUDE.md")
        orig = "# mine\nline\n@other/file.md\n"
        open(cm, "w").write(orig)
        quiet(lambda: cmd_claude_apply(cm, False, repo, local))
        text = open(cm).read()
        check("claude-apply appends section, keeps lines",
              text.startswith(orig) and CLAUDE_HEADING in text and "rules/core.md" in text
              and len(baks(cm)) == 1 and open(baks(cm)[0]).read() == orig)
        rc, out = quiet(lambda: cmd_claude_apply(cm, False, repo, local))
        check("claude-apply idempotent", "unchanged" in out and open(cm).read() == text
              and len(baks(cm)) == 1)
        rc, out = quiet(lambda: cmd_claude_apply(os.path.join(tmp, "new_claude.md"), True, repo, local))
        check("claude-apply dry-run writes nothing", not os.path.exists(os.path.join(tmp, "new_claude.md")))

        # addendum appears later: only its line is added, inside the existing section
        open(cm, "w").write("# mine\n\n" + CLAUDE_HEADING + "\n@" + tilde(os.path.join(
            repo, "rules", "core.md")) + "\n\n## Other\nz\n")
        rc, out = quiet(lambda: cmd_claude_apply(cm, False, repo, add))
        text = open(cm).read()
        check("claude-apply adds only the missing addendum line inside the section",
              text.count("rules/core.md") == 1 and text.count(CLAUDE_HEADING) == 1
              and text.index("@" + tilde(add)) < text.index("## Other")
              and text.index("@" + tilde(add)) > text.index(CLAUDE_HEADING)
              and text.endswith("## Other\nz\n"))
        rc, out = quiet(lambda: cmd_claude_apply(cm, False, repo, add))
        check("claude-apply addendum case idempotent", "unchanged" in out)
        check("render has --- between snippet and core",
              "\n\n---\n\n# Standing rules" in render(repo, local))

        # B3: add-only. A user's own, different core.md lines are never touched or replaced.
        real_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp
        try:
            for rel in (".claude/rules", "dot/claude-rules"):
                os.makedirs(os.path.join(tmp, rel), exist_ok=True)
                open(os.path.join(tmp, rel, "core.md"), "w").write("mine\n")
            mine = "# mine\n@~/.claude/rules/core.md\n@~/dot/claude-rules/core.md\nkeep\n"
            open(cm, "w").write(mine)
            rc, out = quiet(lambda: cmd_claude_apply(cm, False, repo, local))
            text = open(cm).read()
            good = "@" + tilde(os.path.join(repo, "rules", "core.md"))
            check("claude-apply leaves the user's own core.md lines untouched and adds ours",
                  text.startswith(mine) and good in text and "replaced" not in out
                  and text.count("rules/core.md") == 3 and CLAUDE_HEADING in text)
            rc, out = quiet(lambda: cmd_claude_apply(cm, False, repo, local))
            check("claude-apply add-only run is idempotent", "unchanged" in out and open(cm).read() == text)
        finally:
            if real_home is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = real_home
        # a relative @ line resolves against the CLAUDE.md's own folder, never the cwd
        rel_cm = os.path.join(repo, "CLAUDE.md")
        open(rel_cm, "w").write("@rules/core.md\n")
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            rc, out = quiet(lambda: cmd_claude_apply(rel_cm, False, repo, local))
        finally:
            os.chdir(cwd)
        text = open(rel_cm).read()
        check("claude-apply: relative @rules/core.md beside it counts as present, whatever the cwd",
              text.startswith("@rules/core.md\n") and text.count("rules/core.md") == 1
              and "rules/review-contract.md" in text)
        elsewhere = os.path.join(tmp, "sub")
        os.makedirs(elsewhere)
        open(os.path.join(elsewhere, "CLAUDE.md"), "w").write("@rules/core.md\n")
        rc, out = quiet(lambda: cmd_claude_apply(os.path.join(elsewhere, "CLAUDE.md"), False, repo, local))
        check("claude-apply: the same relative line in another folder does not count",
              open(os.path.join(elsewhere, "CLAUDE.md")).read().count("rules/core.md") == 2)

        # codex-defaults
        cfg = os.path.join(tmp, "config.toml")
        open(cfg, "w").write('approval_policy = "on-request"\n\n# the tui\n[tui]\nx = 1\n')
        quiet(lambda: cmd_codex_defaults(cfg, True))
        check("defaults dry-run writes nothing", not baks(cfg))
        quiet(lambda: cmd_codex_defaults(cfg, False))
        text = open(cfg).read()
        top = text.split("[tui]")[0]
        check("defaults land before the first table", 'model = "%s"' % DEFAULT_MODEL in top
              and 'model_reasoning_effort = "%s"' % DEFAULT_EFFORT in top
              and text.count("model = ") == 1 and "# the tui\n[tui]" in text
              and 'approval_policy = "on-request"' in top)
        check("defaults keep a .bak", len(baks(cfg)) == 1)
        rc, out = quiet(lambda: cmd_codex_defaults(cfg, False))
        check("defaults idempotent", "unchanged" in out and open(cfg).read() == text)
        open(cfg, "w").write('model = "mine"\n[tui]\nmodel_reasoning_effort = "low"\n')
        quiet(lambda: cmd_codex_defaults(cfg, False))
        text = open(cfg).read()
        check("defaults: set key kept, table key does not count",
              text.count('model = "mine"') == 1 and DEFAULT_MODEL not in text
              and text.split("[tui]")[0].count("model_reasoning_effort") == 1)
        none = os.path.join(tmp, "sub", "config.toml")
        quiet(lambda: cmd_codex_defaults(none, False))
        check("defaults create a missing config", 'model = "%s"' % DEFAULT_MODEL in open(none).read())

        # B2: the scanner
        def defaults_of(text):
            f = os.path.join(tmp, "t.toml")
            open(f, "w").write(text)
            quiet(lambda: cmd_codex_defaults(f, False))
            return open(f).read()

        arr = defaults_of('rows = [\n  ["a", "b"],\n  ["c"]\n]\n[tui]\nx = 1\n')
        check("scanner: [a,b] rows in a top-level array are not tables",
              arr.index("model =") > arr.index('["c"]') and arr.index("model =") < arr.index("[tui]"))
        ml = defaults_of('s = """\n[important]\nmore\n"""\n[tui]\nx = 1\n')
        check("scanner: a [line] inside a triple-quoted string is not a table",
              ml.index("model =") > ml.index('more\n"""') and ml.index("model =") < ml.rindex("[tui]"))
        lit = defaults_of("s = '''\n[important]\n'''\n[tui]\nx = 1\n")
        check("scanner: same for a literal ''' string", lit.index("model =") > lit.index("[important]"))
        q = defaults_of('"model" = "mine"\n\'model_reasoning_effort\' = "low"\n[tui]\n')
        check("scanner: quoted keys count as set", "gpt-6.2" not in q and q.count("model") == 2)
        cm2 = defaults_of("# [x]\nfoo = 1\n[tui]\n")
        check("scanner: a comment line starting with # [x] is not a table",
              cm2.index("model =") < cm2.index("[tui]") and "# [x]" in cm2)
        for name, bad in (("unterminated string", 'a = "oops\n[tui]\n'),
                          ("unbalanced array", 'a = [\n1,\n')):
            f = os.path.join(tmp, "bad.toml")
            open(f, "w").write(bad)
            check("scanner refuses: " + name, refuses(lambda: cmd_codex_defaults(f, False))
                  and open(f).read() == bad and not baks(f))

        # R1: the walk-up never steps into a string
        def check_di(text, label):
            out1 = defaults_of(text)
            f = os.path.join(tmp, "t.toml")
            body = out1.split('"""')
            check(label + ": keys land outside the string",
                  len(body) == 3 and "model" not in body[1] and "# heading" in body[1]
                  and 'model = "%s"' % DEFAULT_MODEL in out1
                  and out1.index("model =") > out1.rindex('"""'))
            rc, out = quiet(lambda: cmd_codex_defaults(f, False))
            check(label + ": second run unchanged", "unchanged" in out and open(f).read() == out1)

        di_tail = 'di = """\ntext\n# heading"""\n'
        saved = sys.modules.get("tomllib", "absent")
        for mode in ("validation on (when available)", "validation skipped"):
            if mode == "validation skipped":
                sys.modules["tomllib"] = None   # makes `import tomllib` raise ImportError
            try:
                check_di(di_tail + "[tui]\nx=1\n", "R1 with table, " + mode)
                check_di(di_tail, "R1 no table, " + mode)
            finally:
                if saved == "absent":
                    sys.modules.pop("tomllib", None)
                else:
                    sys.modules["tomllib"] = saved

        # R2: up to two quotes before a closing delimiter, and no splitting on U+2028
        q4 = defaults_of('b = """x""""\n[tui]\nx = 1\n')
        check("scanner: a string ending in a quote before the closing triple quote",
              q4.index("model =") < q4.index("[tui]") and q4.index("model =") > q4.index('x""""'))
        q5 = defaults_of("c = '''y'''''\n[tui]\nx = 1\n")
        check("scanner: same for ''' with two quotes of content",
              q5.index("model =") < q5.index("[tui]") and q5.index("model =") > q5.index("y'''''"))
        ls = defaults_of('s = "a' + chr(0x2028) + 'b"\n[tui]\nx = 1\n')
        check("scanner: U+2028 inside a string is not a line break",
              ("a" + chr(0x2028) + "b") in ls and ls.index("model =") < ls.index("[tui]")
              and ls.index("model =") > ls.index("b\""))
        ls2 = defaults_of('s = """a' + chr(0x2028) + '[fake]\n"""\n[tui]\n')
        check("scanner: U+2028 then [fake] inside a multi-line string stays in the string",
              ls2.index("model =") > ls2.index('[fake]') + 12 and ls2.index("model =") < ls2.index("[tui]"))

        # F1: a dangling line of ours inside our section is replaced; outside it, never touched
        gone = os.path.join(tmp, "gone-worktree", "rules", "core.md")
        stale = "@" + gone
        open(cm, "w").write("# mine\n" + stale + "\n\n" + CLAUDE_HEADING + "\n" + stale + "\n\n## Other\nz\n")
        rc, out = quiet(lambda: cmd_claude_apply(cm, False, repo, local))
        text = open(cm).read()
        check("claude-apply removes a dangling rules line inside our section and says so",
              "removed stale line" in out and text.count(stale) == 1
              and text.startswith("# mine\n" + stale + "\n")
              and text.index("rules/core.md", text.index(CLAUDE_HEADING)) < text.index("## Other")
              and text.endswith("## Other\nz\n") and "review-contract.md" in text)
        rc, out = quiet(lambda: cmd_claude_apply(cm, False, repo, local))
        check("claude-apply stale cleanup is idempotent", "unchanged" in out)
        # an existing user file in our section is never removed
        mine2 = os.path.join(tmp, "elsewhere2", "rules")
        os.makedirs(mine2)
        open(os.path.join(mine2, "core.md"), "w").write("x\n")
        open(cm, "w").write(CLAUDE_HEADING + "\n@" + os.path.join(mine2, "core.md") + "\n")
        quiet(lambda: cmd_claude_apply(cm, False, repo, local))
        check("claude-apply keeps an existing-file line inside our section",
              ("@" + os.path.join(mine2, "core.md")) in open(cm).read())

        # F2: an @ line inside a fenced block is not an import
        fenced_core = "@" + os.path.join(repo, "rules", "core.md")
        for fence in ("```", "~~~"):
            open(cm, "w").write("# doc\n" + fence + "\n" + fenced_core + "\n" + fence + "\n")
            rc, out = quiet(lambda: cmd_claude_apply(cm, False, repo, local))
            text = open(cm).read()
            check("claude-apply: an @ line inside a %s fence does not count" % fence,
                  text.startswith("# doc\n" + fence + "\n" + fenced_core + "\n" + fence + "\n")
                  and text.count("rules/core.md") == 2 and CLAUDE_HEADING in text)

        # R4: only the newest 3 of the tool's own backups are kept
        pr = os.path.join(tmp, "PRUNE.md")
        lookalike = pr + BAK + "-old"
        install_bak = pr + BAK
        open(lookalike, "w").write("keep\n")
        open(install_bak, "w").write("keep\n")
        for k in range(6):
            open(pr, "w").write("v%d\n" % k)
            apply(path=pr)
        mine_baks = baks(pr)
        mine_baks = [b for b in mine_baks if b != lookalike]
        check("prune: exactly 3 timestamped backups remain, the newest ones",
              len(mine_baks) == 3 and sorted(open(b).read() for b in mine_baks) == ["v3\n", "v4\n", "v5\n"])
        check("prune: install.sh's backup and a lookalike name are never touched",
              open(lookalike).read() == "keep\n" and open(install_bak).read() == "keep\n")

        # R5: a snippet heading followed directly by the user's own text stays, body and all
        mine3 = "## Codex and other sessions\n- my own notes on codex\n"
        open(agents, "w").write(mine3)
        apply()
        text = open(agents).read()
        check("legacy R5: heading with the user's own body is kept in place",
              text.startswith(mine3) and text.count("- my own notes on codex") == 1
              and text.count(BEGIN) == 1)
        open(agents, "w").write("## Codex and other sessions\n\n## Uploads\nmine\n")
        apply()
        text = open(agents).read()
        check("legacy R5: an empty snippet section is removed",
              text.startswith(BEGIN) and text.endswith(END + "\n\n## Uploads\nmine\n")
              and text.count("## Codex and other sessions") == 1)

        # profiles
        cd = os.path.join(tmp, "codexdir")
        quiet(lambda: cmd_profiles(cd, True))
        check("profiles dry-run writes nothing", not os.path.exists(cd))
        os.makedirs(cd)
        open(os.path.join(cd, "build.config.toml"), "w").write("mine\n")
        quiet(lambda: cmd_profiles(cd, False))
        check("profiles: existing never overwritten, absent created",
              open(os.path.join(cd, "build.config.toml")).read() == "mine\n"
              and 'model = "%s"' % PROFILES["review"][0] in open(
                  os.path.join(cd, "review.config.toml")).read())
        rc, out = quiet(lambda: cmd_profiles(cd, False))
        check("profiles idempotent", "unchanged" in out)

        # no em dash in what we render
        check("no em dash in output", chr(0x2014) not in render(repo, add))
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="standing rules for Claude Code and Codex")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("render")
    p.add_argument("--agent", choices=("codex", "claude"), default="codex")
    for name in ("apply", "claude-apply"):
        p = sub.add_parser(name)
        p.add_argument("--file", required=True)
        p.add_argument("--dry-run", "-n", action="store_true")
        p.add_argument("--yes", "-y", action="store_true", help="accepted for install.sh; never prompts")
    sub.add_parser("claude-lines")
    p = sub.add_parser("codex-defaults")
    p.add_argument("--config", required=True)
    p.add_argument("--dry-run", "-n", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")
    p = sub.add_parser("profiles")
    p.add_argument("--dir", required=True)
    p.add_argument("--dry-run", "-n", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    try:
        if args.cmd == "render":
            if args.agent == "claude":
                print("\n".join(claude_lines()))
            else:
                sys.stdout.write(render())
            return 0
        if args.cmd == "apply":
            return cmd_apply(args.file, args.dry_run)
        if args.cmd == "claude-apply":
            return cmd_claude_apply(args.file, args.dry_run)
        if args.cmd == "claude-lines":
            print("\n".join(claude_lines()))
            return 0
        if args.cmd == "codex-defaults":
            return cmd_codex_defaults(args.config, args.dry_run)
        if args.cmd == "profiles":
            return cmd_profiles(args.dir, args.dry_run)
    except RulesError as exc:
        print("rules_block.py: %s" % exc, file=sys.stderr)
        return 2
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
