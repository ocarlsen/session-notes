#!/usr/bin/env python3
"""skill_feedback.py -- a ledger of how the user corrects a skill's own output,
so a repeated correction becomes a proposed SKILL.md edit instead of repeating forever.

WHY NOT A HOOK. A PostToolUse hook sees what the model just WROTE, never how the user
REACTED to it. The signal this ledger needs ("he replaced my draft with this exact
text", "he rejected it outright", "he approved a non-obvious choice") only exists in the
user's next message, so it can only be captured by something that reads the
conversation: the /reflect skill, once per session (see skills/reflect/SKILL.md, Step
2b). This module is the mechanical half: append rows, and decide which habits have
ENOUGH evidence to act on. Judgment (what the row quotes, which habit it is evidence
of, what the SKILL.md edit should say) stays with the model; this script never invents
or paraphrases text on its own.

LEDGER. One JSONL file per skill, under the Claude home, never inside a skill's own
folder: `<paths.claude_home>/skill-feedback/<skill>.jsonl` (resolved via
notes_config.py; `--ledger PATH` overrides it, mainly for tests). The ledger is USER
DATA, so it lives beside a user's other state, not inside a skill's directory: a skill
folder (`<claude_home>/skills/<name>`) is routinely a symlink INTO a checkout
(install.sh does exactly this), and writing private rows through a symlink would land
them inside whoever's git repo that checkout happens to be. The directory is created on
the first `add`. One row per line, literal fields only -- the user's words are quoted
verbatim, never paraphrased by this script or its caller:

    {"date": "YYYY-MM-DD", "session": "<session id>", "habit": "<kebab-case slug>",
     "before": "<text the skill produced>",
     "after_or_quote": "<user's literal replacement text or rejection words>",
     "verdict": "edit" | "reject" | "approve", "applied": null}

`habit` is a short kebab-case label naming the PATTERN a row is evidence of (e.g.
`no-trailing-summary`), assigned at capture time. It is the grouping key `propose` sorts
on, so qualifying is mechanical code, never judgment made twice. `applied` stays null
until a proposal citing the row is committed, then the commit sha (a string). Rows are
append-only and cited by 1-based line number (`<skill>.jsonl:3`); line order never
changes, only `applied` is ever set in place, by the `applied` command.

SKILL EXISTENCE. `add` (only) refuses loudly (nonzero exit, message naming the path) if
`<paths.claude_home>/skills/<skill>/SKILL.md` is missing, unless `--skill-dir PATH`
names where the skill actually lives: a typo'd skill name must not silently start a
ledger for a skill that doesn't exist. `propose`/`applied`/`canary` never check this --
the ledger is independent of the skill's directory, so they keep working even after a
skill is renamed or removed.

COMMANDS: add, propose, applied, canary. Run `skill_feedback.py --help`, or a
subcommand's `-h`, for flags; docs/tools.md has the full walkthrough and a worked
example. `--selftest` exercises every command in temp directories (`--ledger`,
`--skill-dir`) and never touches the real Claude home.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys
import tempfile

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import notes_config  # noqa: E402

# canary reuses find_session's transcript-path lookup (transcripts_for/paths) and its
# JSONL line reader (iter_lines) rather than writing a second one.
REPO_ROOT = os.path.dirname(BIN)
FIND_SESSION_DIR = os.path.join(REPO_ROOT, "skills", "find-session")
sys.path.insert(0, FIND_SESSION_DIR)
import find_session  # noqa: E402

VERDICTS = ("edit", "reject", "approve")
HABIT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
LEDGER_DIR_NAME = "skill-feedback"
# Skills that run the capture step itself, not a skill being reviewed by it: canary
# never counts them toward N, since a session always "uses" whichever one invoked it.
SELF_SKILLS = {"reflect", "rnr"}


class SkillFeedbackError(Exception):
    """A refusal that should print one message and exit nonzero, never a traceback."""


def validate_skill_name(skill):
    if not skill or not SKILL_NAME_RE.match(skill):
        raise SkillFeedbackError(
            "invalid skill name %r (letters, digits, - and _ only): a typo'd name must "
            "not silently create a new ledger somewhere unexpected" % skill)


def verify_skill_exists(skill, skill_dir=None, cfg=None):
    """Refuses loudly if the skill directory, or its SKILL.md, is missing. Only `add`
    calls this: the ledger lives elsewhere now, so `propose`/`applied`/`canary` never
    need the skill's directory to exist. `cfg` (and notes_config.load()) is never
    touched when `skill_dir` is given."""
    validate_skill_name(skill)
    if skill_dir:
        d = os.path.abspath(os.path.expanduser(skill_dir))
    else:
        cfg = cfg or notes_config.load()
        d = os.path.join(cfg["paths"]["claude_home"], "skills", skill)
    if not os.path.isdir(d):
        raise SkillFeedbackError("no such skill directory: %s" % d)
    if not os.path.isfile(os.path.join(d, "SKILL.md")):
        raise SkillFeedbackError("not a skill directory (no SKILL.md found): %s" % d)
    return d


def ledger_dir_for(cfg=None):
    cfg = cfg or notes_config.load()
    return os.path.join(cfg["paths"]["claude_home"], LEDGER_DIR_NAME)


def ledger_path_for(skill, ledger=None, cfg=None):
    """The ledger file for `skill`: `ledger` if given (an explicit override, mainly for
    tests), else <paths.claude_home>/skill-feedback/<skill>.jsonl. The directory is
    created lazily, on the first write (_append_row); a read-only command tolerates it
    being absent (read_rows() reports no rows for a path that doesn't exist yet)."""
    validate_skill_name(skill)
    if ledger:
        return os.path.abspath(os.path.expanduser(ledger))
    cfg = cfg or notes_config.load()
    return os.path.join(ledger_dir_for(cfg), skill + ".jsonl")


# ------------------------------------------------------------------------ reading
def _validate_row(row, path, lineno):
    if not isinstance(row, dict):
        raise SkillFeedbackError("%s:%d: bad row (not a JSON object)" % (path, lineno))
    if row.get("verdict") not in VERDICTS:
        raise SkillFeedbackError(
            "%s:%d: bad row (verdict must be one of %s)" % (path, lineno, ", ".join(VERDICTS)))
    for key in ("session", "habit"):
        val = row.get(key)
        if not isinstance(val, str) or not val:
            raise SkillFeedbackError(
                "%s:%d: bad row (%s must be a non-empty string)" % (path, lineno, key))
    for key in ("before", "after_or_quote"):
        if not isinstance(row.get(key), str):
            raise SkillFeedbackError("%s:%d: bad row (%s must be a string)" % (path, lineno, key))


def read_rows(path):
    """[(1-based line number, parsed dict), ...]. Missing file = no rows. A line that
    isn't valid JSON, or doesn't have the right shape, is a loud refusal naming the
    exact line, never a silent skip."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SkillFeedbackError("%s:%d: malformed JSON line (%s)" % (path, i, exc))
            _validate_row(row, path, i)
            rows.append((i, row))
    return rows


def _append_row(path, row):
    """Append `row` as a new JSONL line. Returns its real 1-based line number, counted
    from the newline bytes already on disk, never len(read_rows()) (a blank line
    consumes a line number without becoming a row, so that count can undershoot). If
    the ledger's last byte isn't a newline, writes one first so the new row can never
    fuse onto the previous line. Creates the ledger's directory if this is its first
    row."""
    real_path = os.path.realpath(path)
    os.makedirs(os.path.dirname(real_path), exist_ok=True)
    try:
        with open(real_path, "rb") as fh:
            content = fh.read()
    except FileNotFoundError:
        content = b""
    newline_count = content.count(b"\n")
    if content and not content.endswith(b"\n"):
        fix, existing_lines = b"\n", newline_count + 1
    else:
        fix, existing_lines = b"", newline_count
    with open(real_path, "ab") as fh:
        if fix:
            fh.write(fix)
        fh.write(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n")
    return existing_lines + 1


def _resolve_text(text, file_arg):
    if file_arg:
        if file_arg == "-":
            return sys.stdin.read()
        try:
            with open(os.path.expanduser(file_arg), encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise SkillFeedbackError("cannot read %s: %s" % (file_arg, exc))
    return text if text is not None else ""


def _oneline(text):
    """Collapsed to one line for the propose report; the ledger itself keeps the
    original text untouched."""
    return " ".join((text or "").split())


# ------------------------------------------------------------------------- add
def cmd_add(args):
    if args.skill in SELF_SKILLS:
        raise SkillFeedbackError(
            "%s runs the capture step itself; it is not a skill reviewed by it, so it "
            "gets no feedback rows" % args.skill)
    verify_skill_exists(args.skill, args.skill_dir)
    path = ledger_path_for(args.skill, args.ledger)

    if not HABIT_RE.match(args.habit):
        raise SkillFeedbackError(
            "--habit %r must be kebab-case, e.g. no-trailing-summary" % args.habit)
    if args.before is not None and args.before_file is not None:
        raise SkillFeedbackError("pass only one of --before / --before-file")
    if args.after is not None and args.after_file is not None:
        raise SkillFeedbackError("pass only one of --after / --after-file")

    before = _resolve_text(args.before, args.before_file)
    after = _resolve_text(args.after, args.after_file)
    if not before.strip():
        raise SkillFeedbackError(
            "--before/--before-file is required: the text the skill actually produced")
    if not after.strip():
        raise SkillFeedbackError(
            "--after/--after-file is required: the user's literal replacement or rejection words")

    session = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        raise SkillFeedbackError(
            "no session: pass --session or set CLAUDE_CODE_SESSION_ID "
            "(a row with no session can never count toward propose's session threshold)")

    if args.date:
        try:
            datetime.date.fromisoformat(args.date)
        except ValueError:
            raise SkillFeedbackError("--date must be YYYY-MM-DD, got %r" % args.date)
        date = args.date
    else:
        date = datetime.date.today().isoformat()

    rows = read_rows(path)
    for lineno, row in rows:
        # The verdict is part of the key: an approve of the same before/after an
        # earlier edit covered is a DIFFERENT fact (the user reversed course, or a
        # later session re-approved it), never the same row logged twice.
        if (row.get("session") == session and row.get("habit") == args.habit
                and row.get("verdict") == args.verdict
                and row.get("before", "").strip() == before.strip()
                and row.get("after_or_quote", "").strip() == after.strip()):
            print("skipped duplicate %s:%d" % (path, lineno))
            return 0

    new_row = {
        "date": date,
        "session": session,
        "habit": args.habit,
        "before": before,
        "after_or_quote": after,
        "verdict": args.verdict,
        "applied": None,
    }
    lineno = _append_row(path, new_row)
    print("logged %s:%d" % (path, lineno))
    return 0


# --------------------------------------------------------------------- propose
def cmd_propose(args):
    path = ledger_path_for(args.skill, args.ledger)
    rows = read_rows(path)
    if not rows:
        print("no feedback rows: %s" % path)
        return 0

    groups = {}
    for lineno, row in rows:
        if row.get("applied") is not None:
            continue
        groups.setdefault(row.get("habit"), []).append((lineno, row))

    qualifying, tentative = [], []
    for habit, items in sorted(groups.items()):
        edit_reject = [(ln, r) for ln, r in items if r.get("verdict") in ("edit", "reject")]
        sessions = sorted({r.get("session") for _, r in edit_reject})
        approve_items = [(ln, r) for ln, r in items if r.get("verdict") == "approve"]
        if len(edit_reject) >= args.min_rows and len(sessions) >= args.min_sessions:
            qualifying.append({"habit": habit, "rows": edit_reject,
                                "approve": approve_items, "sessions": sessions})
        else:
            tentative.append({"habit": habit, "rows": len(edit_reject), "sessions": len(sessions)})

    basename = os.path.basename(path)

    def trailer(habit, row_nums):
        return "Skill-feedback: %s %s rows %s" % (
            args.skill, habit, ",".join(str(n) for n in row_nums))

    print("## skill feedback: %s (%s)" % (args.skill, path))
    if not qualifying:
        print("\nno qualifying habits yet (need >= %d rows from >= %d sessions)"
              % (args.min_rows, args.min_sessions))
    for q in qualifying:
        row_nums = [ln for ln, _ in q["rows"]]
        print("\n### habit: %s" % q["habit"])
        print("rows: %s" % ", ".join("%s:%d" % (basename, ln) for ln in row_nums))
        print("sessions: %s" % ", ".join(q["sessions"]))
        for ln, r in q["rows"]:
            print("- %s:%d [%s]" % (basename, ln, r.get("verdict")))
            print("    before: %s" % _oneline(r.get("before")))
            print("    after:  %s" % _oneline(r.get("after_or_quote")))
        if q["approve"]:
            print("keep (approve) examples:")
            for ln, r in q["approve"]:
                print("- %s:%d" % (basename, ln))
                print("    before: %s" % _oneline(r.get("before")))
                print("    after:  %s" % _oneline(r.get("after_or_quote")))
        print(trailer(q["habit"], row_nums))
    if tentative:
        print("\n### tentative (below threshold, kept in the ledger as examples)")
        for t in tentative:
            print("- %s (%d row(s), %d session(s))" % (t["habit"], t["rows"], t["sessions"]))
    print()
    print("HELD: propose never edits SKILL.md. Draft it, run a blind A/B, and get the")
    print("user's approval before shipping -- see docs/tools.md.")
    return 0


# --------------------------------------------------------------------- applied
def cmd_applied(args):
    """Read-modify-write with no file lock: never run this while a /reflect might be
    concurrently appending new rows to the same ledger."""
    path = ledger_path_for(args.skill, args.ledger)
    if not os.path.exists(path):
        raise SkillFeedbackError("no ledger to update: %s" % path)
    if not COMMIT_RE.match(args.commit):
        raise SkillFeedbackError(
            "--commit must look like a git sha (7 to 40 hex chars), got %r" % args.commit)

    try:
        row_nums = sorted({int(x) for x in args.rows.split(",") if x.strip()})
    except ValueError:
        raise SkillFeedbackError("--rows must be comma-separated line numbers, got %r" % args.rows)
    if not row_nums:
        raise SkillFeedbackError("--rows must name at least one line number")

    real_path = os.path.realpath(path)
    with open(real_path, encoding="utf-8") as fh:
        raw_lines = fh.readlines()
    total = len(raw_lines)

    parsed = {}
    for n in row_nums:
        if n < 1 or n > total or not raw_lines[n - 1].strip():
            raise SkillFeedbackError("%s: no row at line %d" % (path, n))
        try:
            row = json.loads(raw_lines[n - 1])
        except json.JSONDecodeError as exc:
            raise SkillFeedbackError("%s:%d: malformed JSON line (%s)" % (path, n, exc))
        _validate_row(row, path, n)
        if row.get("applied") is not None:
            raise SkillFeedbackError(
                "%s:%d: already applied (commit %s)" % (path, n, row.get("applied")))
        parsed[n] = row

    # Only now mutate -- every row above is validated first, so a refusal never leaves
    # some rows applied and others not.
    for n, row in parsed.items():
        row["applied"] = args.commit
        raw_lines[n - 1] = json.dumps(row, ensure_ascii=False) + "\n"

    directory = os.path.dirname(real_path) or "."
    tmp_path = real_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.writelines(raw_lines)
        shutil.copymode(real_path, tmp_path)
        os.replace(tmp_path, real_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    print("applied %s: rows %s -> %s"
          % (path, ",".join(str(n) for n in row_nums), args.commit))
    return 0


# ---------------------------------------------------------------------- canary
COMMAND_NAME_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")


def _is_typed_skill(name, skills_dir):
    """A user-typed `/<name>` is a skill (not a built-in like /clear or /model) when it
    names an installed skill folder, or carries a plugin namespace (`ns:skill`)."""
    if ":" in name:
        return True
    return bool(skills_dir) and SKILL_NAME_RE.match(name) is not None and \
        os.path.isfile(os.path.join(skills_dir, name, "SKILL.md"))


def _skill_names_from_transcript(path, skills_dir=None):
    """Distinct skill names this session invoked: a Skill tool_use block's `skill`
    input, plus a user-typed `/<skill>`, which reaches the transcript only as a
    `<command-name>` marker in a user entry with no Skill tool_use at all. A set, so a
    name invoked both ways counts once. A subagent/reviewer turn (isSidechain) is
    excluded: this counts the skills THIS session itself invoked, not a forked
    helper's."""
    names = set()
    for _, line in find_session.iter_lines(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue          # torn last line of a live append target
        if entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "user":
            texts = [content] if isinstance(content, str) else [
                b.get("text") for b in (content or []) if isinstance(b, dict)
                and b.get("type") == "text" and isinstance(b.get("text"), str)]
            for text in texts:
                for typed in COMMAND_NAME_RE.findall(text):
                    if _is_typed_skill(typed, skills_dir):
                        names.add(typed)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "Skill":
                continue
            skill = (block.get("input") or {}).get("skill")
            if isinstance(skill, str) and skill:
                names.add(skill)
    return names


def cmd_canary(args):
    cfg = notes_config.load()
    session = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        raise SkillFeedbackError("no session: pass --session or set CLAUDE_CODE_SESSION_ID")

    if args.transcript:
        transcript = os.path.abspath(os.path.expanduser(args.transcript))
        if not os.path.isfile(transcript):
            raise SkillFeedbackError("no such transcript: %s" % transcript)
    else:
        transcripts_dir, _ = find_session.paths(cfg)
        matches = find_session.transcripts_for(transcripts_dir, [session])
        if not matches:
            raise SkillFeedbackError(
                "no transcript found for session %s under %s: pass --transcript"
                % (session, transcripts_dir))
        # Deterministic: a session id can match in more than one project dir (a
        # resumed session re-rooted elsewhere); take the newest and say which.
        transcript = max(matches, key=lambda p: (os.path.getmtime(p), p))
        if len(matches) > 1:
            print("canary: %d transcripts match session %s; using the newest, %s"
                  % (len(matches), session, transcript), file=sys.stderr)

    skills_dir = os.path.join(cfg["paths"]["claude_home"], "skills")
    names = _skill_names_from_transcript(transcript, skills_dir) - SELF_SKILLS
    n = len(names)

    ledger_dir = ledger_dir_for(cfg)
    m = 0
    if os.path.isdir(ledger_dir):
        for fn in sorted(os.listdir(ledger_dir)):
            if not fn.endswith(".jsonl"):
                continue
            for _, row in read_rows(os.path.join(ledger_dir, fn)):
                if row.get("session") == session:
                    m += 1

    print("skill feedback: %d skills used, %d rows logged" % (n, m))
    return 0


# --------------------------------------------------------------------- CLI
def _positive_int(raw):
    try:
        n = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not an integer" % raw)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1, got %d" % n)
    return n


def build_parser():
    ap = argparse.ArgumentParser(prog="skill_feedback.py",
                                  description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="log one feedback row for a skill")
    a.add_argument("skill")
    a.add_argument("--verdict", required=True, choices=VERDICTS)
    a.add_argument("--habit", required=True)
    a.add_argument("--before")
    a.add_argument("--before-file")
    a.add_argument("--after")
    a.add_argument("--after-file")
    a.add_argument("--session")
    a.add_argument("--date")
    a.add_argument("--skill-dir", help="where the skill itself lives (existence check only)")
    a.add_argument("--ledger", help="override the ledger file location (mainly for tests)")
    a.set_defaults(fn=cmd_add)

    p = sub.add_parser("propose", help="group unresolved rows by habit; report qualifying ones")
    p.add_argument("skill")
    p.add_argument("--min-rows", type=_positive_int, default=3)
    p.add_argument("--min-sessions", type=_positive_int, default=2)
    p.add_argument("--ledger", help="override the ledger file location (mainly for tests)")
    p.set_defaults(fn=cmd_propose)

    ad = sub.add_parser("applied", help="mark rows as applied by a commit")
    ad.add_argument("skill")
    ad.add_argument("--rows", required=True)
    ad.add_argument("--commit", required=True)
    ad.add_argument("--ledger", help="override the ledger file location (mainly for tests)")
    ad.set_defaults(fn=cmd_applied)

    cn = sub.add_parser("canary", help="print the derived skill-feedback canary line")
    cn.add_argument("--transcript", help="a transcript JSONL path (default: locate by --session)")
    cn.add_argument("--session", help="default: $CLAUDE_CODE_SESSION_ID")
    cn.set_defaults(fn=cmd_canary)

    return ap


def main():
    if "--selftest" in sys.argv[1:]:
        return selftest()
    ap = build_parser()
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    try:
        return args.fn(args)
    except SkillFeedbackError as exc:
        print("skill_feedback: %s" % exc, file=sys.stderr)
        return 2


# -------------------------------------------------------------------- selftest
def selftest():
    import io
    import contextlib

    failures = []

    def check(desc, cond):
        print(("PASS " if cond else "FAIL ") + desc)
        if not cond:
            failures.append(desc)

    def run(argv):
        """Run main() against argv, capturing stdout/stderr and the return code."""
        out, err = io.StringIO(), io.StringIO()
        old_argv = sys.argv
        sys.argv = ["skill_feedback.py"] + argv
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = main()
                except SystemExit as exc:
                    # argparse itself calls sys.exit() on a usage error (e.g. an
                    # invalid --verdict choice); treat that like any other exit code.
                    rc = exc.code if isinstance(exc.code, int) else 2
        finally:
            sys.argv = old_argv
        return rc, out.getvalue(), err.getvalue()

    tmp = tempfile.mkdtemp(prefix="skill_feedback_selftest_")
    # Sandbox BOTH knobs notes_config.load() can resolve from: HOME (so a bare
    # "~/.claude" default never touches the real home) and SESSION_NOTES_CONFIG
    # (pointed at an empty file, so a real ~/.config/session-notes/config.toml is
    # never read either). This script never touches the real Claude home.
    orig_home = os.environ.get("HOME")
    orig_config_env = os.environ.get("SESSION_NOTES_CONFIG")
    os.environ["HOME"] = tmp
    empty_cfg = os.path.join(tmp, "empty-config.toml")
    with open(empty_cfg, "w", encoding="utf-8"):
        pass
    os.environ["SESSION_NOTES_CONFIG"] = empty_cfg
    try:
        skill_dir = os.path.join(tmp, "a-skill")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: a-skill\n---\n\nbody\n")
        ledger = os.path.join(tmp, "ledgers", "a-skill.jsonl")
        LB = os.path.basename(ledger)

        # -- add: missing skill dir / SKILL.md refuses loudly
        rc, out, err = run(["add", "ghost-skill", "--verdict", "edit", "--habit", "x",
                             "--before", "b", "--after", "a", "--session", "s1"])
        check("add refuses a nonexistent skill dir", rc == 2 and "no such skill directory" in err)

        # -- add: a clean row, at an explicit --ledger override
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "edit", "--habit", "no-trailing-summary",
                           "--before", "before text", "--after", "after text",
                           "--session", "sess-a", "--date", "2026-10-01"])
        check("add logs the first row", rc == 0 and "logged" in out and ledger in out)
        check("printed line number is 1", out.strip().endswith(":1"))
        rows = read_rows(ledger)
        check("one row on disk", len(rows) == 1)
        check("applied starts null", rows[0][1]["applied"] is None)
        check("date/session/habit stored literally",
              rows[0][1]["date"] == "2026-10-01" and rows[0][1]["session"] == "sess-a"
              and rows[0][1]["habit"] == "no-trailing-summary")

        # -- add: a missing trailing newline on the last line is fixed before the next
        #    append, so the new row never fuses onto the previous one.
        with open(ledger, "rb") as fh:
            content = fh.read()
        check("fixture ledger ends with a newline (precondition)", content.endswith(b"\n"))
        with open(ledger, "wb") as fh:
            fh.write(content.rstrip(b"\n"))  # simulate a ledger with no trailing newline
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "edit", "--habit", "no-trailing-summary",
                           "--before", "second before", "--after", "second after",
                           "--session", "sess-a"])
        check("add after a missing trailing newline logs line 2, not a fused line 1",
              rc == 0 and out.strip().endswith(":2"))
        lines_on_disk = open(ledger, encoding="utf-8").read().splitlines()
        check("the two rows are on separate lines", len(lines_on_disk) == 2)
        check("first row's text is untouched by the fix-up",
              json.loads(lines_on_disk[0])["before"] == "before text")

        # -- add: exact duplicate (even with different surrounding whitespace) is
        #    skipped, not re-logged
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "edit", "--habit", "no-trailing-summary",
                           "--before", "  second before  ", "--after", "second after\n",
                           "--session", "sess-a"])
        check("duplicate add (modulo surrounding whitespace) is skipped",
              rc == 0 and "skipped duplicate" in out)
        check("duplicate did not grow the ledger", len(read_rows(ledger)) == 2)

        # -- the dedupe key includes verdict: an approve of the same before/after/
        #    session/habit as the earlier EDIT is a different fact, not a duplicate
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "approve", "--habit", "no-trailing-summary",
                           "--before", "second before", "--after", "second after",
                           "--session", "sess-a"])
        check("same before/after/session/habit but a different verdict is NOT a duplicate",
              rc == 0 and "logged" in out)
        check("ledger now has 3 rows (the approve was logged, not skipped)",
              len(read_rows(ledger)) == 3)
        check("the new row's verdict is approve", read_rows(ledger)[2][1]["verdict"] == "approve")

        # -- add: bad habit (including an empty one) / bad verdict / missing session
        rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "edit", "--habit", "Not_Kebab", "--before", "b",
                           "--after", "a", "--session", "s1"])
        check("bad habit slug refused", rc == 2 and "kebab-case" in err)

        rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "edit", "--habit", "", "--before", "b",
                           "--after", "a", "--session", "s1"])
        check("empty habit refused", rc == 2 and "kebab-case" in err)

        rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "bogus", "--habit", "x", "--before", "b",
                           "--after", "a", "--session", "s1"])
        check("bad verdict refused by argparse", rc == 2)

        env_had = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                               "--verdict", "edit", "--habit", "x", "--before", "b",
                               "--after", "a"])
            check("missing session refused", rc == 2 and "no session" in err)
        finally:
            if env_had is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = env_had

        # -- add: --before-file / --after-file, including stdin via "-"
        bf = os.path.join(tmp, "before.txt")
        with open(bf, "w", encoding="utf-8") as fh:
            fh.write("multi\nline\nbefore\n")
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "reject", "--habit", "no-trailing-summary",
                           "--before-file", bf, "--after", "no, drop it",
                           "--session", "sess-b"])
        check("add accepts --before-file", rc == 0 and "logged" in out)
        check("file content stored verbatim",
              read_rows(ledger)[-1][1]["before"] == "multi\nline\nbefore\n")

        # -- add: invalid --date is refused (not just shape, a real calendar date)
        rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--ledger", ledger,
                           "--verdict", "edit", "--habit", "x", "--before", "b",
                           "--after", "a", "--session", "s1", "--date", "2026-13-40"])
        check("invalid calendar date refused", rc == 2 and "--date must be" in err)

        # Ledger now has exactly 4 rows: 1 edit, 2 edit, 3 approve (same text as 2,
        # different verdict), 4 reject (a different session). habit no-trailing-summary
        # throughout. edit_reject = rows 1, 2, 4 (3 rows, sessions sess-a + sess-b = 2):
        # this ALREADY meets the default 3-rows/2-sessions threshold.
        check("ledger has exactly 4 rows at this point", len(read_rows(ledger)) == 4)

        # -- propose: qualifies on the first call (no extra row needed), and shows the
        #    approve row as a "keep" example
        rc, out, _ = run(["propose", "a-skill", "--ledger", ledger])
        check("propose qualifies the habit with the approve row as a keep example",
              rc == 0 and "habit: no-trailing-summary" in out
              and ("Skill-feedback: a-skill no-trailing-summary rows 1,2,4" in out)
              and "keep (approve) examples" in out)
        check("propose footer is short and points at docs/tools.md",
              "see docs/tools.md" in out)

        # -- raising --min-rows past the edit/reject count (3) drops it back to
        #    tentative; the approve row never counts toward that count
        rc, out, _ = run(["propose", "a-skill", "--ledger", ledger, "--min-rows", "10"])
        check("an approve-only-inflated group with too few edit/reject rows stays tentative",
              rc == 0 and "no qualifying habits yet" in out
              and "no-trailing-summary (3 row(s), 2 session(s))" in out)

        # -- --min-rows / --min-sessions must be >= 1
        rc, _, err = run(["propose", "a-skill", "--ledger", ledger, "--min-rows", "0"])
        check("--min-rows 0 refused", rc == 2)
        rc, _, err = run(["propose", "a-skill", "--ledger", ledger, "--min-sessions", "-1"])
        check("--min-sessions -1 refused", rc == 2)

        # -- propose threshold boundary, on a dedicated ledger so the arithmetic is
        #    unambiguous: a NEGATIVE case (3 edit/reject rows, but only 1 distinct
        #    session: stays tentative) and a POSITIVE case (exactly 3 rows from
        #    exactly 2 distinct sessions: qualifies at the boundary), asserted on the
        #    printed report text, not just the exit code.
        threshold_ledger = os.path.join(tmp, "ledgers", "threshold-skill.jsonl")
        threshold_dir = os.path.join(tmp, "threshold-skill")
        os.makedirs(threshold_dir)
        with open(os.path.join(threshold_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: threshold-skill\n---\n")

        def add_t(verdict, habit, before, after, session):
            rc, _, _ = run(["add", "threshold-skill", "--skill-dir", threshold_dir,
                             "--ledger", threshold_ledger, "--verdict", verdict,
                             "--habit", habit, "--before", before, "--after", after,
                             "--session", session])
            return rc

        # h-one: 3 rows, all from the SAME session -- negative boundary.
        for i in range(3):
            rc = add_t("edit" if i != 1 else "reject", "h-one", "b%d" % i, "a%d" % i,
                       "only-session")
            check("threshold fixture row %d added" % (i + 1), rc == 0)
        # h-two: exactly 3 rows, from exactly 2 distinct sessions -- positive boundary.
        add_t("edit", "h-two", "c1", "d1", "sess-x")
        add_t("reject", "h-two", "c2", "d2", "sess-y")
        add_t("edit", "h-two", "c3", "d3", "sess-x")

        rc, out, _ = run(["propose", "threshold-skill", "--ledger", threshold_ledger])
        check("negative boundary: 3 rows from 1 session stays tentative",
              rc == 0 and "h-one (3 row(s), 1 session(s))" in out
              and "habit: h-one" not in out)
        check("positive boundary: exactly 3 rows from 2 sessions qualifies",
              "habit: h-two" in out
              and "Skill-feedback: threshold-skill h-two rows 4,5,6" in out)

        # -- applied: sets rows in place, preserves order and other lines byte-identical,
        #    preserves file mode, and refuses a bad commit shape. Back to the main
        #    a-skill ledger (4 rows): apply rows 1 and 4.
        os.chmod(ledger, 0o640)
        before_bytes = open(ledger, "rb").read()
        before_mode = os.stat(ledger).st_mode

        rc, _, err = run(["applied", "a-skill", "--ledger", ledger,
                           "--rows", "1,4", "--commit", "not-hex-ZZ"])
        check("bad commit shape refused", rc == 2 and "git sha" in err)

        rc, out, _ = run(["applied", "a-skill", "--ledger", ledger,
                           "--rows", "1,4", "--commit", "deadbee"])
        check("applied exits 0", rc == 0 and "deadbee" in out)
        rows_after = read_rows(ledger)
        check("line order unchanged", len(rows_after) == 4)
        check("row 1 now applied", rows_after[0][1]["applied"] == "deadbee")
        check("row 4 now applied", rows_after[3][1]["applied"] == "deadbee")
        check("untouched rows 2 and 3 stay unapplied",
              rows_after[1][1]["applied"] is None and rows_after[2][1]["applied"] is None)
        after_lines = open(ledger, "rb").read().splitlines(keepends=True)
        before_lines = before_bytes.splitlines(keepends=True)
        check("rows other than 1 and 4 are byte-identical to before the rewrite",
              after_lines[1] == before_lines[1] and after_lines[2] == before_lines[2])
        check("file mode is preserved across the atomic rewrite",
              os.stat(ledger).st_mode == before_mode)
        check("no leftover <ledger>.tmp after a successful applied",
              not os.path.exists(ledger + ".tmp"))

        # -- applied: refuses a double-apply and an out-of-range row, without mutating
        rc, _, err = run(["applied", "a-skill", "--ledger", ledger,
                           "--rows", "1", "--commit", "0ffa5ba"])
        check("double-apply refused", rc == 2 and "already applied" in err)
        check("double-apply refusal left row 1 unchanged",
              read_rows(ledger)[0][1]["applied"] == "deadbee")

        rc, _, err = run(["applied", "a-skill", "--ledger", ledger,
                           "--rows", "99", "--commit", "abc1234"])
        check("out-of-range row refused", rc == 2 and "no row at line 99" in err)

        rc, _, err = run(["applied", "a-skill", "--ledger", ledger,
                           "--rows", "2,99", "--commit", "abc1234"])
        check("a mixed valid+out-of-range request mutates nothing",
              rc == 2 and read_rows(ledger)[1][1]["applied"] is None)

        # -- propose excludes applied rows from future qualification
        rc, out, _ = run(["propose", "a-skill", "--ledger", ledger, "--min-rows", "1",
                          "--min-sessions", "1"])
        check("applied rows (1, 4) are excluded from a later propose",
              ("%s:1" % LB) not in out and ("%s:4" % LB) not in out)

        # -- malformed line fails loudly, never a silent skip
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        rc, _, err = run(["propose", "a-skill", "--ledger", ledger])
        check("malformed line fails loudly", rc == 2 and ":5: malformed JSON line" in err)

        # -- bad row SHAPE (valid JSON, wrong shape) also fails loudly, via read_rows
        #    directly: a non-object row, and a row missing a required field.
        shaped_ledger = os.path.join(tmp, "ledgers", "shaped-skill.jsonl")
        os.makedirs(os.path.dirname(shaped_ledger), exist_ok=True)
        with open(shaped_ledger, "w", encoding="utf-8") as fh:
            fh.write("[]\n")
        try:
            read_rows(shaped_ledger)
            check("a non-object row ([]) fails loudly as a bad row", False)
        except SkillFeedbackError as exc:
            check("a non-object row ([]) fails loudly as a bad row", "bad row" in str(exc))

        with open(shaped_ledger, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"date": "2026-01-01", "habit": "x", "before": "b",
                                  "after_or_quote": "a", "verdict": "edit",
                                  "applied": None}) + "\n")  # missing "session"
        try:
            read_rows(shaped_ledger)
            check("a row missing session fails loudly as a bad row", False)
        except SkillFeedbackError as exc:
            check("a row missing session fails loudly as a bad row",
                  "bad row" in str(exc) and "session" in str(exc))

        # -- SKILL.md bytes are unchanged after propose: this command never touches it
        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        before_skill_md = open(skill_md_path, "rb").read()
        run(["propose", "a-skill", "--ledger", ledger])
        check("SKILL.md bytes unchanged after propose",
              open(skill_md_path, "rb").read() == before_skill_md)

        # -- a missing ledger (no skill directory needed at all for propose/applied
        #    now): propose says so and exits 0
        missing_ledger = os.path.join(tmp, "ledgers", "clean-skill.jsonl")
        rc, out, _ = run(["propose", "clean-skill", "--ledger", missing_ledger])
        check("missing ledger: propose exits 0 and says so", rc == 0 and "no feedback rows" in out)

        # -- the count subcommand is gone (canary replaced its only consumer)
        rc, _, _ = run(["count", "a-skill", "--ledger", ledger])
        check("count subcommand no longer exists", rc == 2)

        # -- skill name validation (add always checks it, --skill-dir or not)
        rc, _, err = run(["add", "bad/name", "--verdict", "edit", "--habit", "x",
                           "--before", "b", "--after", "a", "--session", "s1"])
        check("skill name with a slash refused", rc == 2 and "invalid skill name" in err)
        rc, _, err = run(["add", "..", "--verdict", "edit", "--habit", "x",
                           "--before", "b", "--after", "a", "--session", "s1"])
        check("skill name of '..' refused", rc == 2 and "invalid skill name" in err)

        # -- no-args and bad --rows usage
        rc, out, _ = run([])
        check("no subcommand prints help and exits 2", rc == 2)
        rc, _, err = run(["applied", "a-skill", "--ledger", ledger,
                           "--rows", "not-a-number", "--commit", "abc1234"])
        check("non-numeric --rows refused", rc == 2 and "--rows must be" in err)

        # -- DESIGN: the default ledger path (no --ledger) resolves under
        #    <claude_home>/skill-feedback/<skill>.jsonl and the directory is created
        #    on first write. HOME is sandboxed to `tmp` above, so this never touches
        #    the real Claude home.
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "edit",
                           "--habit", "default-path-test", "--before", "b", "--after", "a",
                           "--session", "s1"])
        default_ledger = os.path.join(tmp, ".claude", "skill-feedback", "a-skill.jsonl")
        check("default ledger path resolves under <claude_home>/skill-feedback/<skill>.jsonl",
              rc == 0 and os.path.isfile(default_ledger))
        check("default-path row content matches what was logged",
              read_rows(default_ledger)[0][1]["habit"] == "default-path-test")

        # -- canary: counts distinct Skill tool_use names across a transcript
        #    (excluding reflect/rnr and sidechain/subagent turns), plus the sum of
        #    this session's rows across every ledger under skill-feedback/.
        transcripts_dir = os.path.join(tmp, ".claude", "projects", "-tmp-proj")
        os.makedirs(transcripts_dir)
        canary_session = "canary-session-1"
        transcript_path = os.path.join(transcripts_dir, canary_session + ".jsonl")

        def skill_call(name, sidechain=False):
            e = {"type": "assistant",
                 "message": {"content": [{"type": "tool_use", "name": "Skill",
                                           "input": {"skill": name}}]}}
            if sidechain:
                e["isSidechain"] = True
            return json.dumps(e)

        def typed(name):
            return json.dumps({"type": "user", "message": {"role": "user", "content":
                "<command-message>%s</command-message>\n<command-name>/%s</command-name>"
                % (name, name)}})

        delta_dir = os.path.join(tmp, ".claude", "skills", "delta-skill")
        os.makedirs(delta_dir)
        with open(os.path.join(delta_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: delta-skill\n---\n")

        with open(transcript_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join([
                skill_call("alpha-skill"),
                skill_call("alpha-skill"),       # same name again: still 1 distinct name
                skill_call("beta-skill"),
                skill_call("reflect"),            # excluded: the step running this capture
                skill_call("gamma-skill", sidechain=True),  # excluded: a subagent's call
                typed("delta-skill"),             # typed /delta-skill: counted
                typed("beta-skill"),              # typed AND called: still 1 name
                typed("clear"),                   # built-in command, not a skill
            ]) + "\n")

        alpha_dir = os.path.join(tmp, "alpha-skill")
        beta_dir = os.path.join(tmp, "beta-skill")
        os.makedirs(alpha_dir)
        os.makedirs(beta_dir)
        with open(os.path.join(alpha_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: alpha-skill\n---\n")
        with open(os.path.join(beta_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: beta-skill\n---\n")

        # One row for alpha, two for beta, all this session -- M should be 3. A row
        # under a DIFFERENT session must NOT be counted.
        run(["add", "alpha-skill", "--skill-dir", alpha_dir, "--verdict", "edit",
             "--habit", "h1", "--before", "b1", "--after", "a1", "--session", canary_session])
        run(["add", "beta-skill", "--skill-dir", beta_dir, "--verdict", "edit",
             "--habit", "h2", "--before", "b2", "--after", "a2", "--session", canary_session])
        run(["add", "beta-skill", "--skill-dir", beta_dir, "--verdict", "reject",
             "--habit", "h2", "--before", "b3", "--after", "a3", "--session", canary_session])
        run(["add", "alpha-skill", "--skill-dir", alpha_dir, "--verdict", "edit",
             "--habit", "h3", "--before", "bb", "--after", "aa", "--session", "other-session"])

        rc, out, _ = run(["canary", "--transcript", transcript_path, "--session", canary_session])
        check("canary prints the exact derived line (N=3 skills incl. a typed one, M=3 rows)",
              rc == 0 and out.strip() == "skill feedback: 3 skills used, 3 rows logged")

        rc, out, _ = run(["canary", "--session", canary_session])
        check("canary locates the transcript by session id without --transcript",
              rc == 0 and out.strip() == "skill feedback: 3 skills used, 3 rows logged")

        # Same session id in a second project dir, older: still picks the newer one.
        older_dir = os.path.join(tmp, ".claude", "projects", "-tmp-older")
        os.makedirs(older_dir)
        older = os.path.join(older_dir, canary_session + ".jsonl")
        with open(older, "w", encoding="utf-8") as fh:
            fh.write(skill_call("alpha-skill") + "\n")
        os.utime(older, (1, 1))
        rc, out, err = run(["canary", "--session", canary_session])
        check("canary picks the newest of several matching transcripts and names it",
              rc == 0 and out.strip() == "skill feedback: 3 skills used, 3 rows logged"
              and "using the newest" in err and transcript_path in err)

        rc, _, err = run(["add", "reflect", "--skill-dir", alpha_dir, "--verdict", "edit",
                          "--habit", "h1", "--before", "b", "--after", "a",
                          "--session", canary_session])
        check("add refuses a self skill (reflect)", rc == 2 and "capture step" in err)

        rc, _, err = run(["canary", "--session", "no-such-session"])
        check("canary refuses loudly when no transcript is found",
              rc == 2 and "no transcript found" in err)

        env_had2 = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            rc, _, err = run(["canary"])
            check("canary refuses with no session given", rc == 2 and "no session" in err)
        finally:
            if env_had2 is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = env_had2
    finally:
        os.environ.pop("SESSION_NOTES_CONFIG", None)
        if orig_config_env is not None:
            os.environ["SESSION_NOTES_CONFIG"] = orig_config_env
        if orig_home is not None:
            os.environ["HOME"] = orig_home
        else:
            os.environ.pop("HOME", None)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
