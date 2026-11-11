#!/usr/bin/env python3
"""memory_index_lint.py -- refuse an over-cap /reflect memory-index hook line.

PREVENT, not just report. Prose alone fails to stop hook growth: hooks that already say
"keep this short" still grow, because a correction gets appended where it loads at boot
and nothing pushes back. The real chokepoint lives in `append_memory_index.py` (the
only path a new index line is ever written through: it imports `check_line()` from this
module and refuses an over-cap NEW line before writing, not as a follow-up lint anyone
could skip). This module holds the shared primitives plus a whole-FILE lint (offenders
across every new/changed line) for a broader pass:

  - An index is a MEMORY.md made of `- [Title](file.md) - hook` lines (an em dash in
    place of the hyphen separator is accepted too). The hook is the text after it.
  - CAP: 110 chars for a `feedback_*.md` target, 80 chars for anything else.
  - Only a NEW or CHANGED line is checked. `lint_hooks()`/`lint()` need a BASELINE (the
    pre-write snapshot) to know which lines are pre-existing: pass `--baseline <path>`
    explicitly, or let it default to `git show HEAD:<path>` when the index lives inside
    a git repo. **If neither is available, it REFUSES to run** rather than silently
    treating every legacy line in a large, established index as "new" (which would
    flag hundreds of grandfathered hooks at once and train everyone to ignore it).
  - Also runs boot_budget's check-file on the SAME index (one source of truth for the
    file's own total-size budget, independent of any single hook's length).

Which index: pass its path, or `--scope NAME` to lint a configured store's MEMORY.md
("global" is paths.global_store; any other NAME is a [[scopes]] entry's store).

Exit 0 (clean) or 1 (refused; offenders + budget message on stdout) from the CLI, 2 when
it refuses to run (no baseline) or the scope name is unknown.
`--selftest` is hermetic: temp files/temp git repos only, never touches the real store.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import boot_budget  # noqa: E402
import notes_config  # noqa: E402

FEEDBACK_CAP = 110
OTHER_CAP = 80

# `- [Title](file.md) - hook text`. Same `](...)` anchor as memory_hygiene_canary's
# INDEX_LINK_RE (prose parentheticals like "(see foo.md)" never match), plus the
# trailing " - hook" capture. The separator may be a hyphen-minus or an em dash
# (written as an escape so this file stays dash-free).
INDEX_LINE_RE = re.compile(r"^-\s*\[[^\]]*\]\(([^)]+\.md)\)\s*[\u2014-]\s*(.*)$")


class UnknownScopeError(ValueError):
    """--scope named a store the config does not define."""


def _load_config_or_exit():
    try:
        return notes_config.load()
    except notes_config.ConfigError as exc:
        sys.exit(f"memory_index_lint: bad config: {exc}")


def index_path_for_scope(name, cfg=None):
    """The MEMORY.md of a configured store: "global" is paths.global_store, anything
    else must match a [[scopes]] entry's name. Raises UnknownScopeError otherwise."""
    cfg = cfg or _load_config_or_exit()
    if name == "global":
        return os.path.join(cfg["paths"]["global_store"], "MEMORY.md")
    for s in cfg.get("scopes", []):
        if s["name"] == name:
            return os.path.join(s["store"], "MEMORY.md")
    known = ["global"] + [s["name"] for s in cfg.get("scopes", [])]
    raise UnknownScopeError(f"unknown scope {name!r} (known: {', '.join(known)})")


class HookCapError(ValueError):
    """A single index line's hook exceeds its cap (see check_line()). Raised by the
    append_memory_index.py chokepoint; carries the ready-to-print refusal message."""


class NoIndexError(RuntimeError):
    """The index file to lint is missing or unreadable (reported as a clean refusal)."""


class NoBaselineError(RuntimeError):
    """lint_hooks()/lint() were given no --baseline and no git HEAD copy could be
    found either -- refusing rather than silently treating every existing line as
    new (see module docstring)."""


def parse_index_lines(text):
    """Every index-shaped line, in order, as (target, full_line, hook_text). A target
    may legitimately appear on more than one line; callers that must not lose any
    (the lint) use this rather than parse_index()."""
    out = []
    for line in text.splitlines():
        m = INDEX_LINE_RE.match(line.rstrip())
        if not m:
            continue
        out.append((m.group(1), line, m.group(2).strip()))
    return out


def parse_index(text):
    """Return {target: (full_line, hook_text)} for every index-shaped line. Last
    occurrence of a given target wins (an index should have one line per target)."""
    return {t: (line, hook) for t, line, hook in parse_index_lines(text)}


def cap_for(target):
    name = os.path.basename(target)
    return FEEDBACK_CAP if name.startswith("feedback_") else OTHER_CAP


def check_line(line):
    """The single-line primitive both this module's lint AND
    append_memory_index.py's chokepoint call (one source of truth for what "over cap"
    means). No baseline involved -- purely "is THIS line, on its own, over its cap."

    Returns {"ok": bool, "target": str|None, "hook_len": int|None, "cap": int|None,
    "message": str|None}. A line that isn't index-shaped is trivially ok (target=None,
    not this check's concern)."""
    m = INDEX_LINE_RE.match(line.rstrip())
    if not m:
        return {"ok": True, "target": None, "hook_len": None, "cap": None, "message": None}
    target, hook = m.group(1), m.group(2).strip()
    cap = cap_for(target)
    hook_len = len(hook)
    if hook_len > cap:
        message = (
            f"REFUSED: hook for {target} is {hook_len} chars, over the {cap}-char cap "
            "-- the hook is the CURRENT RULE; move the incident/edge case into the "
            "memory file's BODY, then shorten the hook and retry."
        )
        return {"ok": False, "target": target, "hook_len": hook_len, "cap": cap, "message": message}
    return {"ok": True, "target": target, "hook_len": hook_len, "cap": cap, "message": None}


# --- Default baseline: git HEAD, when the index lives in a git repo -------------------
# Every git call here strips GIT_*: run from inside a git hook (which exports GIT_DIR for the
# repo being committed to), an inherited GIT_DIR overrides -C and would point a call at the
# hook's repo instead of the one the path lives in.
def _git_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git_root_for(path):
    """The git repo root containing `path`'s directory, or None (not a repo / no git).
    realpath'd (not just abspath'd): `git rev-parse --show-toplevel` always resolves
    symlinks (e.g. macOS /var -> /private/var, or a symlinked config dir), so a
    caller comparing this against a non-realpath'd path would get a bogus relpath that
    escapes the repo entirely."""
    try:
        out = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.realpath(path)), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def default_baseline_text(index_path):
    """Best-effort `git show HEAD:<relpath>` for index_path, or None if index_path
    isn't in a git repo, has no HEAD copy, or git isn't available. Never raises."""
    root = _git_root_for(index_path)
    if not root:
        return None
    realpath = os.path.realpath(index_path)  # see _git_root_for's docstring
    try:
        rel = os.path.relpath(realpath, root)
    except ValueError:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", root, "show", f"HEAD:{rel}"],
            capture_output=True, text=True, timeout=10, env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _load_baseline(index_path, baseline_path):
    """Returns the baseline as parse_index_lines() output, or raises NoBaselineError."""
    if baseline_path:
        if not os.path.isfile(baseline_path):
            raise NoBaselineError(f"--baseline path does not exist: {baseline_path}")
        text = Path(baseline_path).read_text(encoding="utf-8", errors="replace")
        return parse_index_lines(text)
    text = default_baseline_text(index_path)
    if text is None:
        raise NoBaselineError(
            f"no --baseline given and no git HEAD copy found for {index_path} -- "
            "refusing to run rather than treat every existing line as new "
            "(pass --baseline explicitly, e.g. a pre-write snapshot)"
        )
    return parse_index_lines(text)


def lint_hooks(index_path, baseline_path=None):
    """Offenders: NEW or CHANGED index lines whose hook exceeds its cap (via
    check_line() -- the single shared primitive, not a re-derived length check).

    A line is "unchanged" only when its target existed in the baseline AND the full
    line text is byte-identical -- so a hook that got LONGER (even by one char) via an
    edit is treated as changed, never silently grandfathered. Raises NoBaselineError
    if `baseline_path` is None and no git HEAD copy is available (see module
    docstring) -- never silently falls back to "everything is new."
    """
    try:
        text = Path(index_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise NoIndexError(f"cannot read index {index_path}: {exc.strerror or exc}")
    current = parse_index_lines(text)
    baseline_lines = {}
    for t, bline, _ in _load_baseline(index_path, baseline_path):
        baseline_lines.setdefault(t, set()).add(bline)

    offenders = []
    # EVERY line is judged on its own: a second line for a target must not ride on an
    # older identical-target line being grandfathered.
    for target, line, hook in current:
        base = baseline_lines.get(target)
        if base is not None and line in base:
            continue  # pre-existing, unchanged: grandfathered
        result = check_line(line)
        if not result["ok"]:
            offenders.append(
                {
                    "target": target,
                    "hook_len": result["hook_len"],
                    "cap": result["cap"],
                    "line": line.strip(),
                    "status": "new" if base is None else "changed",
                }
            )
    return offenders


def lint(index_path, baseline_path=None, as_path=None):
    """Returns (ok, report_lines). May raise NoBaselineError (propagated, not caught
    here -- the caller decides how to report a refusal-to-run vs a real violation)."""
    offenders = lint_hooks(index_path, baseline_path)
    budget_ok, budget_msg = boot_budget.check_file(index_path, as_path)

    lines = []
    if offenders:
        lines.append(
            f"REFUSED: {len(offenders)} new/changed index hook(s) over cap in {index_path}"
        )
        for o in offenders:
            lines.append(
                f"  [{o['status']}] {o['target']}: hook {o['hook_len']} chars "
                f"> cap {o['cap']}: {o['line']}"
            )
        lines.append(
            "  Fix: the hook is the CURRENT RULE; move incidents/edge cases to the "
            "memory file BODY, then shorten the hook and re-run."
        )
    if not budget_ok:
        lines.append(budget_msg)

    ok = not offenders and budget_ok
    if ok:
        lines = [f"OK: hooks + budget clean for {index_path}"]
    return ok, lines


# --- Selftest --------------------------------------------------------------------
def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=_git_env())


def selftest():
    ok = True

    def check(cond, msg):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"  [{status}] {msg}")

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        # --- Cap classification: feedback_ (110) vs other (80), via check_line() ---
        short_feedback_hook = "x" * 110
        long_feedback_hook = "x" * 111
        short_ref_hook = "y" * 80
        long_ref_hook = "y" * 81

        r1 = check_line(f"- [F](feedback_a.md) - {short_feedback_hook}")
        check(r1["ok"] and r1["cap"] == 110, "check_line: feedback_ hook exactly at 110 is ok, cap=110")
        r2 = check_line(f"- [F2](feedback_b.md) - {long_feedback_hook}")
        check(not r2["ok"] and r2["message"] is not None,
              "check_line: feedback_ hook at 111 is refused (positive control)")
        r3 = check_line(f"- [R](reference_c.md) - {short_ref_hook}")
        check(r3["ok"] and r3["cap"] == 80, "check_line: non-feedback hook exactly at 80 is ok, cap=80")
        r4 = check_line(f"- [R2](reference_d.md) - {long_ref_hook}")
        check(not r4["ok"], "check_line: non-feedback hook at 81 is refused (positive control)")
        r5 = check_line("not an index line at all")
        check(r5["ok"] and r5["target"] is None, "check_line: a non-index line is trivially ok")

        # --- Build an index + an EMPTY baseline (explicit "everything is new") ---
        index_new = "# Memory Index\n\n"
        index_new += f"- [F](feedback_a.md) - {short_feedback_hook}\n"
        index_new += f"- [F2](feedback_b.md) - {long_feedback_hook}\n"
        index_new += f"- [R](reference_c.md) - {short_ref_hook}\n"
        index_new += f"- [R2](reference_d.md) - {long_ref_hook}\n"
        idx = d / "MEMORY.md"
        _write(idx, index_new)
        empty_baseline = d / "empty_baseline.md"
        _write(empty_baseline, "# Memory Index\n\n")

        offenders = lint_hooks(str(idx), str(empty_baseline))  # explicit empty baseline: all new
        targets = {o["target"] for o in offenders}
        check("feedback_a.md" not in targets, "feedback_ hook exactly at 110 chars is NOT an offender")
        check("feedback_b.md" in targets, "feedback_ hook at 111 chars IS an offender (positive control)")
        check("reference_c.md" not in targets, "non-feedback hook exactly at 80 chars is NOT an offender")
        check("reference_d.md" in targets, "non-feedback hook at 81 chars IS an offender (positive control)")
        b_offender = next(o for o in offenders if o["target"] == "feedback_b.md")
        check(b_offender["cap"] == 110, "feedback_ offender records the 110 cap")
        d_offender = next(o for o in offenders if o["target"] == "reference_d.md")
        check(d_offender["cap"] == 80, "non-feedback offender records the 80 cap")

        # --- Refuse to run: no --baseline AND no git repo (never treats all-as-new) ---
        no_git_dir = d / "no_git"
        no_git_dir.mkdir()
        idx_no_git = no_git_dir / "MEMORY.md"
        _write(idx_no_git, f"- [R](reference_c.md) - {short_ref_hook}\n")
        raised_no_baseline = False
        try:
            lint_hooks(str(idx_no_git))
        except NoBaselineError:
            raised_no_baseline = True
        check(
            raised_no_baseline,
            "no --baseline and no git repo: lint_hooks refuses (NoBaselineError), "
            "never silently treats every existing line as new",
        )

        # --- Grandfathering: a pre-existing over-cap line is NOT refused ---
        baseline = d / "MEMORY.baseline.md"
        _write(baseline, index_new)  # baseline == current, byte-identical
        offenders_gf = lint_hooks(str(idx), str(baseline))
        check(offenders_gf == [], "byte-identical baseline grandfathers every pre-existing offender")

        # --- Only the CHANGED/NEW line is checked, not untouched siblings ---
        index_v2 = index_new + f"- [R3](reference_e.md) - {long_ref_hook}\n"  # new line
        idx2 = d / "MEMORY2.md"
        _write(idx2, index_v2)
        offenders_v2 = lint_hooks(str(idx2), str(baseline))
        targets_v2 = {o["target"] for o in offenders_v2}
        check("reference_d.md" not in targets_v2, "an untouched pre-existing offender (in baseline) stays grandfathered")
        check("reference_e.md" in targets_v2, "a genuinely NEW over-cap line is flagged even with a baseline present")

        # --- A hook that got LONGER on an existing target is "changed", flagged ---
        index_v3 = index_new.replace(
            f"- [R](reference_c.md) - {short_ref_hook}\n",
            f"- [R](reference_c.md) - {long_ref_hook}\n",
        )
        idx3 = d / "MEMORY3.md"
        _write(idx3, index_v3)
        offenders_v3 = lint_hooks(str(idx3), str(baseline))
        changed = [o for o in offenders_v3 if o["target"] == "reference_c.md"]
        check(len(changed) == 1 and changed[0]["status"] == "changed",
              "a pre-existing target whose hook grew past cap is flagged as CHANGED")

        # --- Default baseline: git HEAD, when the index is committed in a git repo ---
        gd = d / "gitrepo"
        gd.mkdir()
        _git(gd, "init", "-q")
        _git(gd, "config", "user.email", "test@example.com")
        _git(gd, "config", "user.name", "Selftest")
        idx_git = gd / "MEMORY.md"
        _write(idx_git, f"- [R](reference_c.md) - {short_ref_hook}\n")
        _git(gd, "add", "MEMORY.md")
        _git(gd, "commit", "-q", "-m", "seed")
        # Working copy now diverges from HEAD: one unchanged line, one new over-cap line
        # (simulating exactly the /reflect scenario -- append happened, no explicit
        # --baseline passed, but the index lives in a git repo with a committed HEAD).
        _write(idx_git, f"- [R](reference_c.md) - {short_ref_hook}\n" + f"- [R2](reference_d.md) - {long_ref_hook}\n")
        offenders_git = lint_hooks(str(idx_git))  # NO --baseline: must default to git HEAD
        targets_git = {o["target"] for o in offenders_git}
        check("reference_d.md" in targets_git,
              "git-default baseline: a line not present in HEAD is flagged as new (positive control)")
        check("reference_c.md" not in targets_git,
              "git-default baseline: the unchanged HEAD line stays grandfathered")

        # --- boot_budget integration: check-file result folds into lint() ---
        orig_budgets = dict(boot_budget.BUDGETS)
        try:
            boot_budget.BUDGETS[str(idx)] = 10  # deliberately tiny, forces an over
            lint_ok, lines = lint(str(idx), baseline_path=str(baseline))
            check(not lint_ok, "lint() fails when boot_budget flags the index over its own budget")
            check(any("over budget" in ln for ln in lines),
                  "lint() output includes the boot_budget over-budget message")
        finally:
            boot_budget.BUDGETS.clear()
            boot_budget.BUDGETS.update(orig_budgets)

        # --- lint(): clean case (grandfathered, in budget) is OK ---
        lint_ok2, lines2 = lint(str(idx), baseline_path=str(baseline))
        check(lint_ok2, "lint() is OK when every offender is grandfathered and unbudgeted")

        # --- --as: check content of a staged file against a different real path ---
        real_path_name = str(d / "real_index.md")
        boot_budget.BUDGETS[real_path_name] = 5  # tiny budget on the "real" destination
        try:
            lint_ok3, lines3 = lint(str(idx), baseline_path=str(baseline), as_path=real_path_name)
            check(not lint_ok3,
                  "lint() --as: staged content checked against a DIFFERENT real path's tiny budget")
        finally:
            del boot_budget.BUDGETS[real_path_name]

        # --- --scope resolution from the config ---
        cfg = {"paths": {"global_store": "/tmp/alpha-global"},
               "scopes": [{"name": "beta", "root": "/tmp/beta", "store": "/tmp/beta-store"}]}
        check(index_path_for_scope("global", cfg) == "/tmp/alpha-global/MEMORY.md",
              "--scope global resolves to paths.global_store/MEMORY.md")
        check(index_path_for_scope("beta", cfg) == "/tmp/beta-store/MEMORY.md",
              "--scope NAME resolves to that scope's store/MEMORY.md")
        try:
            index_path_for_scope("gamma", cfg)
            check(False, "an unknown --scope is refused")
        except UnknownScopeError:
            check(True, "an unknown --scope is refused")

        # --- an em-dash separator is still parsed (older indexes use one) ---
        em = "- [E](feedback_em.md) " + chr(0x2014) + " " + long_feedback_hook
        check(not check_line(em)["ok"] and check_line(em)["target"] == "feedback_em.md",
              "check_line: an em-dash separator is parsed like a hyphen")

        # --- Non-index lines and malformed lines never crash the parser ---
        messy = "# Memory Index\n\nSome prose (see foo.md) with no link.\n- not a link line\n"
        messy_idx = d / "messy.md"
        _write(messy_idx, messy)
        offenders_messy = lint_hooks(str(messy_idx), str(empty_baseline))
        check(offenders_messy == [], "prose / malformed lines are ignored, never crash")

        # --- a target on two lines: a new over-cap line is not grandfathered by an
        # older line for the same target (the dict-per-target parse lost the old one) ---
        old_line = "- [R](reference_dup.md) - short hook"
        new_line = f"- [R](reference_dup.md) - {long_ref_hook}"
        dup_base = d / "dup_base.md"
        _write(dup_base, old_line + "\n")
        dup_idx = d / "dup_idx.md"
        _write(dup_idx, new_line + "\n" + old_line + "\n")
        check([o["target"] for o in lint_hooks(str(dup_idx), str(dup_base))] == ["reference_dup.md"],
              "a new over-cap line is flagged even when an older line for its target is unchanged")

        # --- a missing index and a bad config are clean exits, not tracebacks ---
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                rc_missing = main([str(d / "nope.md"), "--baseline", str(dup_base)])
            except Exception:
                rc_missing = "traceback"
        check(rc_missing == 2, "a missing index file is refused (rc 2), not a traceback")

        bad_cfg = d / "bad.toml"
        bad_cfg.write_text("this is not toml\n")
        saved_env = os.environ.get("SESSION_NOTES_CONFIG")
        os.environ["SESSION_NOTES_CONFIG"] = str(bad_cfg)
        try:
            try:
                index_path_for_scope("global")
                outcome = "no error"
            except SystemExit as exc:
                outcome = "exit" if exc.code else "exit0"
            except Exception:
                outcome = "traceback"
        finally:
            if saved_env is None:
                os.environ.pop("SESSION_NOTES_CONFIG", None)
            else:
                os.environ["SESSION_NOTES_CONFIG"] = saved_env
        check(outcome == "exit", "a bad config is a clean sys.exit(message), not a traceback")

    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("index_file", nargs="?", help="the MEMORY.md (or other index) to lint")
    ap.add_argument(
        "--baseline",
        default=None,
        help="prior copy of the index (snapshot before the write); pre-existing "
        "unchanged lines are grandfathered. Omit to default to `git show HEAD:...` "
        "when index_file is in a git repo -- refuses to run if neither is available.",
    )
    ap.add_argument(
        "--as",
        dest="as_path",
        default=None,
        help="check the index's total-size budget against this real path instead of "
        "index_file's own path",
    )
    ap.add_argument(
        "--scope",
        default=None,
        help='lint a configured store\'s MEMORY.md instead of a path: "global" or a '
        "[[scopes]] name from the config",
    )
    ap.add_argument("--selftest", action="store_true", help="run the built-in hermetic selftest")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.scope and args.index_file:
        ap.error("pass index_file or --scope, not both")
    if args.scope:
        try:
            args.index_file = index_path_for_scope(args.scope)
        except UnknownScopeError as e:
            print(f"REFUSED TO RUN: {e}", file=sys.stderr)
            return 2
    if not args.index_file:
        ap.error("index_file (or --scope) is required unless --selftest")

    try:
        ok, lines = lint(args.index_file, args.baseline, args.as_path)
    except (NoBaselineError, NoIndexError) as e:
        print(f"REFUSED TO RUN: {e}", file=sys.stderr)
        return 2
    except notes_config.ConfigError as e:
        sys.exit(f"memory_index_lint: bad config: {e}")

    for ln in lines:
        print(ln)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
