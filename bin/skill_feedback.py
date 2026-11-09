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

LEDGER. One JSONL file beside the skill it's about: `<skill dir>/feedback.jsonl`. One
row per line, literal fields only -- the user's words are quoted verbatim, never
paraphrased by this script or its caller:

    {"date": "YYYY-MM-DD", "session": "<session id>", "habit": "<kebab-case slug>",
     "before": "<text the skill produced>",
     "after_or_quote": "<user's literal replacement text or rejection words>",
     "verdict": "edit" | "reject" | "approve", "applied": null}

`habit` is a short kebab-case label naming the PATTERN a row is evidence of (e.g.
`no-trailing-summary`), assigned at capture time. It is the grouping key `propose` sorts
on, so qualifying is mechanical code, never judgment made twice. `applied` stays null
until a proposal citing the row is committed, then the commit sha (a string). Rows are
append-only and cited by 1-based line number (`feedback.jsonl:3`); line order never
changes, only `applied` is ever set in place, by the `applied` command.

SKILL DIR RESOLUTION. `--skill-dir PATH` overrides; otherwise
`<paths.claude_home>/skills/<skill>` from notes_config.py. Refuses loudly (nonzero exit,
message naming the path) when the directory or its SKILL.md is missing, so a typo'd
skill name fails fast instead of quietly starting a new ledger nobody will find.

WRITE GUARD. Before `add` or `applied` write anything, the ledger's real directory is
checked against git: if it sits inside some git work tree and the ledger path is not
covered by that tree's own ignore rules, the write is refused ("ledger would be tracked
by <toplevel>: add feedback.jsonl to its .gitignore or pass --skill-dir") instead of
risking a private row landing in a commit. install.sh makes `~/.claude/skills/<name>` a
symlink POINTING INTO this checkout, so a real ledger lands inside THIS work tree by
construction; this repo's own `/skills/*/feedback.jsonl` .gitignore rule is what clears
that case. `propose` and `count` never write, so they never run this check. Even a
gitignored ledger inside a clone is lost to `git clean -fdx`; see docs/tools.md.

COMMANDS: add, propose, applied, count. Run `skill_feedback.py --help`, or a
subcommand's `-h`, for flags; docs/tools.md has the full walkthrough and a worked
example. `--selftest` exercises every command in temp directories (`--skill-dir`) and
never touches the real Claude home.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import notes_config  # noqa: E402

VERDICTS = ("edit", "reject", "approve")
HABIT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
LEDGER_NAME = "feedback.jsonl"


class SkillFeedbackError(Exception):
    """A refusal that should print one message and exit nonzero, never a traceback."""


def validate_skill_name(skill):
    if not skill or not SKILL_NAME_RE.match(skill):
        raise SkillFeedbackError(
            "invalid skill name %r (letters, digits, - and _ only): a typo'd name must "
            "not silently create a new ledger somewhere unexpected" % skill)


def skill_dir_for(skill, skill_dir=None, cfg=None):
    """The skill's directory: `skill_dir` if given, else
    <paths.claude_home>/skills/<skill>. Refuses loudly if it, or its SKILL.md, is
    missing. `cfg` (and a call to notes_config.load()) is never needed, and never
    touched, when `skill_dir` is given."""
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


def ledger_path_for(skill, skill_dir=None, cfg=None):
    return os.path.join(skill_dir_for(skill, skill_dir, cfg), LEDGER_NAME)


# ------------------------------------------------------------------- write guard
def _git_env():
    """Every git subprocess call below uses this: a git hook exports GIT_DIR (and
    friends) for the repo being committed to, and an inherited GIT_DIR overrides -C,
    silently redirecting the call at the hook's repo instead of the ledger's."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git_toplevel(dirpath):
    """The git work tree root containing `dirpath`, or None (not inside one / no git)."""
    try:
        out = subprocess.run(
            ["git", "-C", dirpath, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _git_is_ignored(dirpath, path):
    try:
        out = subprocess.run(
            ["git", "-C", dirpath, "check-ignore", "-q", path],
            timeout=10, env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def refuse_if_ledger_would_be_tracked(path):
    """The write-side chokepoint (called by `add` and `applied` only, never by the
    read-only `propose`/`count`): refuse rather than silently risk a private row
    landing inside a commit. See the module docstring's WRITE GUARD section."""
    real_dir = os.path.dirname(os.path.realpath(path))
    toplevel = _git_toplevel(real_dir)
    if toplevel is None:
        return
    if not _git_is_ignored(real_dir, os.path.realpath(path)):
        raise SkillFeedbackError(
            "ledger would be tracked by %s: add feedback.jsonl to its .gitignore or "
            "pass --skill-dir" % toplevel)


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
    fuse onto the previous line."""
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
    path = ledger_path_for(args.skill, args.skill_dir, None)
    refuse_if_ledger_would_be_tracked(path)

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
        if (row.get("session") == session and row.get("habit") == args.habit
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
    path = ledger_path_for(args.skill, args.skill_dir, None)
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
    for habit, items in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0])):
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
    path = ledger_path_for(args.skill, args.skill_dir, None)
    refuse_if_ledger_would_be_tracked(path)
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
    tmp_path = os.path.join(directory, LEDGER_NAME + ".tmp")
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


# ----------------------------------------------------------------------- count
def cmd_count(args):
    path = ledger_path_for(args.skill, args.skill_dir, None)
    rows = read_rows(path)
    total = len(rows)
    if args.session:
        session_total = sum(1 for _, r in rows if r.get("session") == args.session)
        print("%s: %d rows total, %d for session %s" % (args.skill, total, session_total, args.session))
    else:
        print("%s: %d rows total" % (args.skill, total))
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
    a.add_argument("--skill-dir")
    a.set_defaults(fn=cmd_add)

    p = sub.add_parser("propose", help="group unresolved rows by habit; report qualifying ones")
    p.add_argument("skill")
    p.add_argument("--min-rows", type=_positive_int, default=3)
    p.add_argument("--min-sessions", type=_positive_int, default=2)
    p.add_argument("--skill-dir")
    p.set_defaults(fn=cmd_propose)

    ad = sub.add_parser("applied", help="mark rows as applied by a commit")
    ad.add_argument("skill")
    ad.add_argument("--rows", required=True)
    ad.add_argument("--commit", required=True)
    ad.add_argument("--skill-dir")
    ad.set_defaults(fn=cmd_applied)

    c = sub.add_parser("count", help="row counts for a skill's ledger")
    c.add_argument("skill")
    c.add_argument("--session")
    c.add_argument("--skill-dir")
    c.set_defaults(fn=cmd_count)

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
    # Never let a bare `notes_config.load()` (the no-skill-dir refusal path) resolve the
    # REAL Claude home or a real ~/.config/session-notes/config.toml: point it at an
    # empty file in the sandbox, which resolves to built-in defaults.
    orig_config_env = os.environ.get("SESSION_NOTES_CONFIG")
    empty_cfg = os.path.join(tmp, "empty-config.toml")
    with open(empty_cfg, "w", encoding="utf-8"):
        pass
    os.environ["SESSION_NOTES_CONFIG"] = empty_cfg
    try:
        skill_dir = os.path.join(tmp, "a-skill")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: a-skill\n---\n\nbody\n")
        ledger = os.path.join(skill_dir, LEDGER_NAME)

        # -- add: missing skill dir / SKILL.md refuses loudly
        rc, out, err = run(["add", "ghost-skill", "--verdict", "edit", "--habit", "x",
                             "--before", "b", "--after", "a", "--session", "s1"])
        check("add refuses a nonexistent skill dir", rc == 2 and "no such skill directory" in err)

        # -- add: a clean row
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "edit",
                           "--habit", "no-trailing-summary", "--before", "before text",
                           "--after", "after text", "--session", "sess-a", "--date", "2026-10-01"])
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
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "edit",
                           "--habit", "no-trailing-summary", "--before", "second before",
                           "--after", "second after", "--session", "sess-a"])
        check("add after a missing trailing newline logs line 2, not a fused line 1",
              rc == 0 and out.strip().endswith(":2"))
        lines_on_disk = open(ledger, encoding="utf-8").read().splitlines()
        check("the two rows are on separate lines", len(lines_on_disk) == 2)
        check("first row's text is untouched by the fix-up",
              json.loads(lines_on_disk[0])["before"] == "before text")

        # -- add: exact duplicate (even with different surrounding whitespace) is
        #    skipped, not re-logged
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "edit",
                           "--habit", "no-trailing-summary", "--before", "  second before  ",
                           "--after", "second after\n", "--session", "sess-a"])
        check("duplicate add (modulo surrounding whitespace) is skipped",
              rc == 0 and "skipped duplicate" in out)
        check("duplicate did not grow the ledger", len(read_rows(ledger)) == 2)

        # -- add: bad habit (including an empty one) / bad verdict / missing session
        rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "edit",
                           "--habit", "Not_Kebab", "--before", "b", "--after", "a",
                           "--session", "s1"])
        check("bad habit slug refused", rc == 2 and "kebab-case" in err)

        rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "edit",
                           "--habit", "", "--before", "b", "--after", "a", "--session", "s1"])
        check("empty habit refused", rc == 2 and "kebab-case" in err)

        rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "bogus",
                           "--habit", "x", "--before", "b", "--after", "a", "--session", "s1"])
        check("bad verdict refused by argparse", rc == 2)

        env_had = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "edit",
                               "--habit", "x", "--before", "b", "--after", "a"])
            check("missing session refused", rc == 2 and "no session" in err)
        finally:
            if env_had is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = env_had

        # -- add: --before-file / --after-file, including stdin via "-"
        bf = os.path.join(tmp, "before.txt")
        with open(bf, "w", encoding="utf-8") as fh:
            fh.write("multi\nline\nbefore\n")
        rc, out, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "reject",
                           "--habit", "no-trailing-summary", "--before-file", bf,
                           "--after", "no, drop it", "--session", "sess-b"])
        check("add accepts --before-file", rc == 0 and "logged" in out)
        check("file content stored verbatim",
              read_rows(ledger)[-1][1]["before"] == "multi\nline\nbefore\n")

        # -- add: invalid --date is refused (not just shape, a real calendar date)
        rc, _, err = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "edit",
                           "--habit", "x", "--before", "b", "--after", "a", "--session", "s1",
                           "--date", "2026-13-40"])
        check("invalid calendar date refused", rc == 2 and "--date must be" in err)

        # -- propose: below threshold is tentative, not qualifying (3 rows so far: 1
        #    edit, 1 edit after the newline fix, 1 reject, all habit no-trailing-summary,
        #    sessions sess-a and sess-b: 2 sessions already, but min-rows defaults to 3
        #    so one more row is needed to prove the boundary)
        rc, out, _ = run(["propose", "a-skill", "--skill-dir", skill_dir])
        check("propose exits 0 below threshold", rc == 0)

        # -- propose: a fourth edit/reject row qualifies the habit
        rc, _, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "reject",
                        "--habit", "no-trailing-summary", "--before", "before text 2",
                        "--after", "no, cut the closing line", "--session", "sess-b"])
        check("fourth row added", rc == 0)
        rc, out, _ = run(["propose", "a-skill", "--skill-dir", skill_dir])
        check("propose now qualifies the habit",
              rc == 0 and "habit: no-trailing-summary" in out
              and "Skill-feedback: a-skill no-trailing-summary rows" in out)
        check("propose footer is short and points at docs/tools.md",
              "see docs/tools.md" in out)

        # -- an approve row for the same habit shows as a keep example, never counts
        #    toward the edit/reject threshold on its own
        rc, _, _ = run(["add", "a-skill", "--skill-dir", skill_dir, "--verdict", "approve",
                        "--habit", "no-trailing-summary", "--before", "kept as-is",
                        "--after", "exactly, keep it", "--session", "sess-a"])
        rc, out, _ = run(["propose", "a-skill", "--skill-dir", skill_dir, "--min-rows", "10"])
        check("an approve-only group with too few edit/reject rows stays tentative",
              rc == 0 and "no qualifying habits yet" in out
              and "no-trailing-summary (4 row(s)" in out)

        # -- --min-rows / --min-sessions must be >= 1
        rc, _, err = run(["propose", "a-skill", "--skill-dir", skill_dir, "--min-rows", "0"])
        check("--min-rows 0 refused", rc == 2)
        rc, _, err = run(["propose", "a-skill", "--skill-dir", skill_dir, "--min-sessions", "-1"])
        check("--min-sessions -1 refused", rc == 2)

        # -- applied: sets rows in place, preserves order and other lines byte-identical,
        #    preserves file mode, and refuses a bad commit shape. Ledger has 5 rows now.
        os.chmod(ledger, 0o640)
        before_bytes = open(ledger, "rb").read()
        before_mode = os.stat(ledger).st_mode

        rc, _, err = run(["applied", "a-skill", "--skill-dir", skill_dir,
                           "--rows", "1,5", "--commit", "not-hex-ZZ"])
        check("bad commit shape refused", rc == 2 and "git sha" in err)

        rc, out, _ = run(["applied", "a-skill", "--skill-dir", skill_dir,
                           "--rows", "1,5", "--commit", "deadbee"])
        check("applied exits 0", rc == 0 and "deadbee" in out)
        rows_after = read_rows(ledger)
        check("line order unchanged", len(rows_after) == 5)
        check("row 1 now applied", rows_after[0][1]["applied"] == "deadbee")
        check("row 5 now applied", rows_after[4][1]["applied"] == "deadbee")
        check("untouched rows 2, 3, 4 stay unapplied",
              all(rows_after[i][1]["applied"] is None for i in (1, 2, 3)))
        after_lines = open(ledger, "rb").read().splitlines(keepends=True)
        before_lines = before_bytes.splitlines(keepends=True)
        check("rows other than 1 and 5 are byte-identical to before the rewrite",
              all(after_lines[i] == before_lines[i] for i in (1, 2, 3)))
        check("file mode is preserved across the atomic rewrite",
              os.stat(ledger).st_mode == before_mode)
        check("no leftover feedback.jsonl.tmp after a successful applied",
              not os.path.exists(os.path.join(skill_dir, LEDGER_NAME + ".tmp")))

        # -- applied: refuses a double-apply and an out-of-range row, without mutating
        rc, _, err = run(["applied", "a-skill", "--skill-dir", skill_dir,
                           "--rows", "1", "--commit", "0ffa5ba"])
        check("double-apply refused", rc == 2 and "already applied" in err)
        check("double-apply refusal left row 1 unchanged",
              read_rows(ledger)[0][1]["applied"] == "deadbee")

        rc, _, err = run(["applied", "a-skill", "--skill-dir", skill_dir,
                           "--rows", "99", "--commit", "abc1234"])
        check("out-of-range row refused", rc == 2 and "no row at line 99" in err)

        rc, _, err = run(["applied", "a-skill", "--skill-dir", skill_dir,
                           "--rows", "2,99", "--commit", "abc1234"])
        check("a mixed valid+out-of-range request mutates nothing",
              rc == 2 and read_rows(ledger)[1][1]["applied"] is None)

        # -- propose excludes applied rows from future qualification
        rc, out, _ = run(["propose", "a-skill", "--skill-dir", skill_dir, "--min-rows", "1",
                          "--min-sessions", "1"])
        check("applied rows (1, 5) are excluded from a later propose",
              "feedback.jsonl:1" not in out and "feedback.jsonl:5" not in out)

        # -- malformed / malshaped lines fail loudly, never a silent skip
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        rc, _, err = run(["propose", "a-skill", "--skill-dir", skill_dir])
        check("malformed line fails loudly", rc == 2 and ":6: malformed JSON line" in err)
        rc, _, err = run(["count", "a-skill", "--skill-dir", skill_dir])
        check("count also fails loudly on a malformed line", rc == 2 and "malformed JSON" in err)

        shaped_dir = os.path.join(tmp, "shaped-skill")
        os.makedirs(shaped_dir)
        with open(os.path.join(shaped_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: shaped-skill\n---\n")
        shaped_ledger = os.path.join(shaped_dir, LEDGER_NAME)
        with open(shaped_ledger, "w", encoding="utf-8") as fh:
            fh.write("[]\n")
        rc, _, err = run(["count", "shaped-skill", "--skill-dir", shaped_dir])
        check("a non-object row ([]) fails loudly as a bad row", rc == 2 and "bad row" in err)

        with open(shaped_ledger, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"date": "2026-01-01", "habit": "x", "before": "b",
                                  "after_or_quote": "a", "verdict": "edit",
                                  "applied": None}) + "\n")  # missing "session"
        rc, _, err = run(["count", "shaped-skill", "--skill-dir", shaped_dir])
        check("a row missing session fails loudly as a bad row",
              rc == 2 and "bad row" in err and "session" in err)

        # -- SKILL.md bytes are unchanged after propose: this command never edits the skill
        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        before_skill_md = open(skill_md_path, "rb").read()
        run(["propose", "a-skill", "--skill-dir", skill_dir])
        check("SKILL.md bytes unchanged after propose",
              open(skill_md_path, "rb").read() == before_skill_md)

        # -- a clean skill with no ledger: propose says so and exits 0
        clean_dir = os.path.join(tmp, "clean-skill")
        os.makedirs(clean_dir)
        with open(os.path.join(clean_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: clean-skill\n---\n")
        rc, out, _ = run(["propose", "clean-skill", "--skill-dir", clean_dir])
        check("missing ledger: propose exits 0 and says so", rc == 0 and "no feedback rows" in out)
        rc, out, _ = run(["count", "clean-skill", "--skill-dir", clean_dir])
        check("missing ledger: count reports zero", rc == 0 and "0 rows total" in out)

        # -- skill name validation
        rc, _, err = run(["add", "bad/name", "--verdict", "edit", "--habit", "x",
                           "--before", "b", "--after", "a", "--session", "s1"])
        check("skill name with a slash refused", rc == 2 and "invalid skill name" in err)
        rc, _, err = run(["add", "..", "--verdict", "edit", "--habit", "x",
                           "--before", "b", "--after", "a", "--session", "s1"])
        check("skill name of '..' refused", rc == 2 and "invalid skill name" in err)

        # -- no-args and bad --rows usage
        rc, out, _ = run([])
        check("no subcommand prints help and exits 2", rc == 2)
        rc, _, err = run(["applied", "a-skill", "--skill-dir", skill_dir,
                           "--rows", "not-a-number", "--commit", "abc1234"])
        check("non-numeric --rows refused", rc == 2 and "--rows must be" in err)

        # -- write guard: a skill dir inside an unignored git work tree refuses to
        #    write; the same dir with a covering .gitignore rule succeeds. Every git
        #    call here goes through _git_env() for the same reason production code
        #    does (a hook's inherited GIT_DIR must never redirect it).
        git_repo = os.path.join(tmp, "git-repo")
        os.makedirs(git_repo)
        subprocess.run(["git", "init", "-q"], cwd=git_repo, env=_git_env(), check=True)
        tracked_skill = os.path.join(git_repo, "tracked-skill")
        os.makedirs(tracked_skill)
        with open(os.path.join(tracked_skill, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: tracked-skill\n---\n")

        rc, _, err = run(["add", "tracked-skill", "--skill-dir", tracked_skill,
                           "--verdict", "edit", "--habit", "x", "--before", "b",
                           "--after", "a", "--session", "s1"])
        check("write inside an unignored git work tree refuses",
              rc == 2 and "would be tracked by" in err)
        check("the refused write never created a ledger",
              not os.path.exists(os.path.join(tracked_skill, LEDGER_NAME)))

        with open(os.path.join(git_repo, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("tracked-skill/feedback.jsonl\n")
        rc, out, _ = run(["add", "tracked-skill", "--skill-dir", tracked_skill,
                           "--verdict", "edit", "--habit", "x", "--before", "b",
                           "--after", "a", "--session", "s1"])
        check("the same write succeeds once covered by .gitignore",
              rc == 0 and "logged" in out)

        # applied is guarded the same way, now that the ledger is gitignored and has
        # one row from the add above.
        rc, out, _ = run(["applied", "tracked-skill", "--skill-dir", tracked_skill,
                           "--rows", "1", "--commit", "abc1234"])
        check("applied succeeds too, once the ledger is gitignored",
              rc == 0 and "abc1234" in out)
    finally:
        os.environ.pop("SESSION_NOTES_CONFIG", None)
        if orig_config_env is not None:
            os.environ["SESSION_NOTES_CONFIG"] = orig_config_env
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
