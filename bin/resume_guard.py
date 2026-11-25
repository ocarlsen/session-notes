#!/usr/bin/env python3
"""resume_guard.py -- the ONE predicate that refuses a RESUME.md write which leaves it over budget.

A scope's RESUME.md has a budget (boot_budget's `resume_bytes`, per-file overrides included).
Nothing used to stop a write that crossed it, so the file turned into a log. This module is the
write-time chokepoint every writer calls: `codex_save.py commit` for Codex, and
`hooks/resume_size_guard.py` (a Claude PreToolUse hook) for Edit/Write/Bash on Claude Code.

The rule, `check_write(path, old_text, new_text)`:
  * a result at or under the budget passes;
  * a result over the budget passes only if it is SHORTER than what was there (a trim that is
    still over budget must be allowed, or the file could never be walked back under in steps);
  * anything else is refused with REFUSAL text: move finished items to RESUME-archive.md.
The budget is never typed here: it comes from boot_budget.budget_for(path), falling back to
boot_budget's `resume_bytes` setting for a RESUME.md in a store that has no table entry (a
Codex git-root store). A config that cannot be read raises ConfigError: the callers fail loud.

    python3 resume_guard.py --selftest
"""
import os
import sys

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import notes_config  # noqa: E402

REFUSAL = ("%s would be %d chars after this write, over its %d-char budget (it is %d now). "
           "Move finished items to RESUME-archive.md beside it, verbatim, then retry.")


def budget_for_resume(path):
    """The char budget for a RESUME.md path (boot_budget's lookup). Raises ConfigError when
    the config cannot be loaded."""
    import boot_budget
    boot_budget.reload_config()
    b = boot_budget.budget_for(path)
    if b is None:
        b = boot_budget._budget_setting("resume_bytes")
    return b


def check_write(path, old_text, new_text, budget=None):
    """None when the write is allowed, else the refusal text. Only a file named RESUME.md is
    judged; any other name returns None."""
    if os.path.basename(str(path)) != "RESUME.md":
        return None
    if budget is None:
        budget = budget_for_resume(path)
    new_n, old_n = len(new_text), len(old_text)
    if new_n <= budget or new_n < old_n:
        return None
    return REFUSAL % (path, new_n, budget, old_n)


def configured_resumes(cfg):
    """Realpaths of every configured store's RESUME.md: the global store and each scope's
    (in the loaded agent mode, plus a shared scope's shared store)."""
    out = {os.path.realpath(os.path.join(cfg["paths"]["global_store"], "RESUME.md"))}
    for s in cfg.get("scopes", []):
        out.add(os.path.realpath(os.path.join(s["store"], "RESUME.md")))
    return out


def is_configured_resume(path, cfg):
    return os.path.realpath(os.path.expanduser(str(path))) in configured_resumes(cfg)


def selftest():
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    saved = os.environ.get("SESSION_NOTES_CONFIG")
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        cfgf = os.path.join(d, "c.toml")
        with open(cfgf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\nglobal_store = "%s/g"\n'
                     '[budgets]\nresume_bytes = 100\n'
                     '[[scopes]]\nname = "a"\nroot = "%s/a"\nstore = "%s/a-store"\n' % (d, d, d, d))
        os.environ["SESSION_NOTES_CONFIG"] = cfgf
        try:
            r = os.path.join(d, "a-store", "RESUME.md")
            check("under budget passes", check_write(r, "", "x" * 100) is None)
            msg = check_write(r, "x" * 90, "x" * 101)
            check("crossing the budget is refused with the archive instruction",
                  msg and "RESUME-archive.md" in msg and "verbatim" in msg and "101" in msg)
            check("a grow that stays over budget is refused", check_write(r, "x" * 150, "x" * 151) is not None)
            check("a same-size rewrite over budget is refused", check_write(r, "x" * 150, "y" * 150) is not None)
            check("a trim that is still over budget passes", check_write(r, "x" * 450, "x" * 300) is None)
            check("a trim under budget passes", check_write(r, "x" * 450, "x" * 50) is None)
            check("a non-RESUME file is ignored", check_write(os.path.join(d, "a-store", "x.md"), "", "x" * 900) is None)
            check("unlisted RESUME.md falls back to resume_bytes",
                  check_write(os.path.join(d, "other", "RESUME.md"), "", "x" * 101) is not None)
            check("configured_resumes lists the scope store and global store",
                  is_configured_resume(r, notes_config.load())
                  and is_configured_resume(os.path.join(d, "g", "RESUME.md"), notes_config.load())
                  and not is_configured_resume(os.path.join(d, "other", "RESUME.md"), notes_config.load()))
            os.environ["SESSION_NOTES_CONFIG"] = os.path.join(d, "missing.toml")
            try:
                check_write(r, "", "x")
                check("missing config fails loud", False)
            except notes_config.ConfigError:
                check("missing config fails loud", True)
        finally:
            if saved is None:
                os.environ.pop("SESSION_NOTES_CONFIG", None)
            else:
                os.environ["SESSION_NOTES_CONFIG"] = saved
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(__doc__)
