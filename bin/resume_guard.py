#!/usr/bin/env python3
"""resume_guard.py -- the ONE predicate that refuses a RESUME.md write which leaves it over budget.

A scope's RESUME.md has a budget (boot_budget's `resume_bytes`, per-file overrides included).
Nothing used to stop a write that crossed it, so the file turned into a log. This module is the
write-time chokepoint every writer calls: `codex_save.py commit` for Codex, and
`hooks/resume_size_guard.py` (a Claude PreToolUse hook) for Edit/Write/Bash on Claude Code.

The rule, `check_write(path, old_text, new_text)`:
  * a result at or under the budget passes (and, for a store Codex boots, one Codex's
    SessionStart pack would still load: a dry run of hooks/codex_session_start.plan_context);
  * a result over it passes only if it removes text and adds none (every non-blank line of the
    new text is in the old text, as a multiset), so a file can be trimmed back in steps but no
    new line goes in while it is over;
  * anything else is refused with REFUSAL text: move finished items to RESUME-archive.md.
  * the old RESUME.md of a scope that migrated to a shared store is never writable.
The budget is never typed here: it comes from boot_budget.budget_for(path), falling back to
boot_budget's `resume_bytes` setting for a RESUME.md in a store that has no table entry (a
Codex git-root store). A config that cannot be read raises ConfigError: the callers fail loud.

    python3 resume_guard.py --selftest
"""
import os
import re
import sys

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import notes_config  # noqa: E402

REFUSAL = ("%s would be %d chars after this write, over its %d-char budget (it is %d now). "
           "Move finished items to RESUME-archive.md beside it, verbatim, then retry.")
CODEX_REFUSAL = ("%s would not load in Codex after this write: its SessionStart pack (the "
                 "agent.codex_boot_bytes cap, %d chars) would cut or drop it (%s). Move finished items to RESUME-archive.md beside it, verbatim, then retry.")
WARN_TEXT = ("warning: %s would use %d of the %d chars Codex's SessionStart pack has left for it "
             "(agent.codex_boot_bytes cap %d): near the point where older entries get cut. "
             "Move finished items to RESUME-archive.md soon.")
ADD_REFUSAL = " While it is over budget a write may only remove lines, not add or change any."
LEGACY_REFUSAL = ("%s is the old RESUME.md of a scope that migrated to a shared store: it is no "
                  "longer read. Write the shared store's RESUME.md instead: %s")


def budget_for_resume(path):
    """The char budget for a RESUME.md path (boot_budget's lookup). Raises ConfigError when
    the config cannot be loaded."""
    import boot_budget
    boot_budget.reload_config()
    b = boot_budget.budget_for(path)
    if b is None:
        b = boot_budget._budget_setting("resume_bytes")
    return b


def _nonblank_counter(text):
    import collections
    return collections.Counter(ln for ln in text.split("\n") if ln.strip())


def only_removes(old_text, new_text):
    """True when every non-blank line of new_text is in old_text, as a multiset: the write
    removes text and adds none."""
    extra = _nonblank_counter(new_text) - _nonblank_counter(old_text)
    return not extra


def _codex_boot_entry(path, new_text, cfg, cwd=None):
    """(cap, entry) where entry is plan_context's dict for `path` (a RESUME.md) if it held
    new_text, else None (not a store Codex boots, or the path is not packed). Only for a store
    Codex boots: every store in codex mode, a shared scope's store otherwise. It dry-runs
    hooks/codex_session_start.plan_context with the candidate text, so the cap and the packing
    live only there."""
    rp = os.path.realpath(path)
    found = None
    shared_roots = [s["root"] for s in cfg.get("scopes", []) if s.get("shared_store")
                    and os.path.realpath(os.path.join(s["store"], "RESUME.md")) == rp]
    if cfg["agent"]["kind"] != "codex" and not shared_roots:
        return None
    saved = os.environ.get("SESSION_NOTES_AGENT")
    sys.path.insert(0, os.path.join(os.path.dirname(BIN), "hooks"))
    try:
        import codex_session_start as css  # forces codex mode in os.environ at first import
    finally:
        sys.path.pop(0)
        if saved is None:
            os.environ.pop("SESSION_NOTES_AGENT", None)
        else:
            os.environ["SESSION_NOTES_AGENT"] = saved
    env = dict(os.environ, SESSION_NOTES_AGENT="codex")
    ccfg = notes_config.load(env=env)
    roots = shared_roots or ([cwd] if cwd else [s["root"] for s in ccfg["scopes"]]) or [os.getcwd()]
    real_read = css._read_checked

    def patched(p):
        return (new_text.strip(), None) if os.path.realpath(p) == rp else real_read(p)
    css._read_checked = patched
    try:
        for root in roots:
            _text, entries = css.plan_context(root, ccfg)
            for e in entries:
                if os.path.realpath(e["path"]) == rp:
                    # the worst entry over every root wins: a refusal beats a warning
                    if e["status"] != "loaded":
                        return ccfg["agent"]["codex_boot_bytes"], e
                    if e.get("warn") and found is None:
                        found = (ccfg["agent"]["codex_boot_bytes"], e)
    finally:
        css._read_checked = real_read
    return found


def codex_boot_drop(path, new_text, cfg, cwd=None):
    """(cap, reason) for why Codex's SessionStart pack would not load `path` (a RESUME.md) whole
    if it held new_text (dropped, or truncated with older entries cut), else None."""
    hit = _codex_boot_entry(path, new_text, cfg, cwd)
    if hit and hit[1]["status"] != "loaded":
        return hit[0], hit[1]["reason"]
    return None


def codex_boot_warning(path, new_text, cfg=None, cwd=None):
    """Warning text (never a refusal) when `path` would still load whole in Codex's pack but use
    >= the plan_context warn share of the room it has left, else None. The share is
    codex_session_start.RESUME_WARN_FRACTION, read from the entry's own `warn` mark."""
    if os.path.basename(str(path)) != "RESUME.md":
        return None
    hit = _codex_boot_entry(path, new_text, cfg or notes_config.load(), cwd)
    if hit and hit[1]["status"] == "loaded" and hit[1].get("warn"):
        e = hit[1]
        return WARN_TEXT % (path, e["block_chars"], e["room"], hit[0])
    return None


def check_write(path, old_text, new_text, budget=None, cwd=None):
    """None when the write is allowed, else the refusal text. Only a file named RESUME.md is
    judged; any other name returns None.

    A result at or under the budget (and one Codex would still load, for a store it boots)
    passes. A result over it passes only if it removes text and adds none (every non-blank line
    of the new text is in the old text, as a multiset), so a file can be walked back under in
    steps but nothing new goes in while it is over."""
    if os.path.basename(str(path)) != "RESUME.md":
        return None
    if budget is None:
        budget = budget_for_resume(path)
    new_n, old_n = len(new_text), len(old_text)
    why = None
    if new_n > budget:
        why = REFUSAL % (path, new_n, budget, old_n)
    else:
        drop = codex_boot_drop(path, new_text, notes_config.load(), cwd)
        if drop:
            why = CODEX_REFUSAL % (path, drop[0], drop[1])
    if why is None or only_removes(old_text, new_text):
        return None
    return why + ADD_REFUSAL


def configured_resumes(cfg):
    """Realpaths of every configured store's RESUME.md: the global store and each scope's
    (in the loaded agent mode, plus a shared scope's shared store)."""
    out = {os.path.realpath(os.path.join(cfg["paths"]["global_store"], "RESUME.md"))}
    for s in cfg.get("scopes", []):
        out.add(os.path.realpath(os.path.join(s["store"], "RESUME.md")))
    return out


def legacy_resumes(cfg):
    """{realpath: shared RESUME.md} of the OLD per-harness RESUME.md files of every scope that
    opted into a shared store. They are no longer read, so nothing may write them."""
    import shared_store
    out = {}
    for s in cfg.get("scopes", []):
        if s.get("shared_store"):
            shared = os.path.join(s["store"], "RESUME.md")
            for st in shared_store.legacy_stores(cfg, s).values():
                out[os.path.realpath(os.path.join(st, "RESUME.md"))] = shared
    return out


def is_configured_resume(path, cfg):
    return os.path.realpath(os.path.expanduser(str(path))) in configured_resumes(cfg)


def legacy_refusal(path, cfg):
    """The refusal for a write to a migrated scope's old RESUME.md, else None."""
    rp = os.path.realpath(os.path.expanduser(str(path)))
    shared = legacy_resumes(cfg).get(rp)
    return LEGACY_REFUSAL % (path, shared) if shared else None


def selftest():
    import tempfile
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
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
            L = lambda n, c="x": "".join("%s%d\n" % (c, i) for i in range(n))  # noqa: E731
            check("under budget passes", check_write(r, "", "x" * 100) is None)
            msg = check_write(r, "x" * 90, "x" * 101)
            check("crossing the budget is refused with the archive instruction",
                  msg and "RESUME-archive.md" in msg and "verbatim" in msg and "101" in msg)
            old = L(40)  # 40 lines, over the 100-char budget
            check("a same-size rewrite over budget is refused", check_write(r, old, old.replace("x1\n", "y1\n")) is not None)
            check("the 1-char swap sequence is refused (adds a line while over budget)",
                  check_write(r, old, old.replace("x39\n", "z39\n")) is not None)
            check("a trim that only removes lines passes while still over budget",
                  check_write(r, old, "".join(old.splitlines(True)[:30])) is None)
            check("a trim under budget passes", check_write(r, old, "x1\nx2\n") is None)
            check("a rewrite that lands under budget passes whatever it contains",
                  check_write(r, old, "brand new\n") is None)
            check("a multiset: a duplicated old line is an addition",
                  check_write(r, old, old + "x1\n") is not None)
            check("a non-RESUME file is ignored", check_write(os.path.join(d, "a-store", "x.md"), "", "x" * 900) is None)
            check("unlisted RESUME.md falls back to resume_bytes",
                  check_write(os.path.join(d, "other", "RESUME.md"), "", "x" * 101) is not None)
            check("configured_resumes lists the scope store and global store",
                  is_configured_resume(r, notes_config.load())
                  and is_configured_resume(os.path.join(d, "g", "RESUME.md"), notes_config.load())
                  and not is_configured_resume(os.path.join(d, "other", "RESUME.md"), notes_config.load()))
            # Codex boot cap: a shared scope's RESUME that fits the budget but not Codex's pack
            with open(cfgf, "a") as fh:
                fh.write('[[scopes]]\nname = "sh"\nroot = "%s/sh"\nshared_store = true\n' % d)
            with open(cfgf) as fh:
                txt = fh.read().replace("[budgets]", "[agent]\ncodex_home = \"%s/cx\"\ncodex_boot_bytes = 600\n[budgets]" % d, 1)
            with open(cfgf, "w") as fh:
                fh.write(txt)
            def with_cap(n):
                with open(cfgf) as fh:
                    t = re.sub(r"codex_boot_bytes = \d+", "codex_boot_bytes = %d" % n, fh.read())
                with open(cfgf, "w") as fh:
                    fh.write(t)
                return notes_config.load()
            sh = with_cap(600)
            shr = os.path.join([s for s in sh["scopes"] if s["name"] == "sh"][0]["store"], "RESUME.md")
            cap = next(n for n in range(100, 5000, 10)
                       if (with_cap(n) and check_write(shr, "", "ok\n") is None))
            msg = check_write(shr, "", "y" * 95 + "\n")
            check("a shared RESUME under budget but dropped by Codex's pack is refused, naming the cap",
                  msg and "would not load in Codex" in msg and str(cap) in msg, msg)
            check("a small shared RESUME that Codex loads passes at that cap", check_write(shr, "", "ok\n") is None)
            multi = "# R\n" + "".join("- line %02d\n" % i for i in range(8))  # 8 units, under the 100 budget
            tcap = next(n for n in range(100, 5000)
                        if (with_cap(n) and check_write(shr, "", multi) is None))
            big = "# R\n" + "".join("- line %03d padding padding\n" % i for i in range(40))  # 1.2K
            with_cap(900)
            msg = check_write(shr, "", big, budget=10000)
            drop = codex_boot_drop(shr, big, notes_config.load())
            check("a write that would TRUNCATE the shared RESUME is refused (truncated counts as not loaded)",
                  drop is not None and "older entries cut to fit" in drop[1]
                  and msg and "would not load in Codex" in msg and "cut or drop" in msg, (drop, msg))
            with_cap(tcap)
            warn = codex_boot_warning(shr, multi, notes_config.load())
            check("a write that fits exactly is allowed AND warns (non-blocking, text separate)",
                  check_write(shr, "", multi) is None and warn and warn.startswith("warning:")
                  and "RESUME-archive.md" in warn, warn)
            with_cap(tcap + 4000)
            check("plenty of room: no warning", codex_boot_warning(shr, multi, notes_config.load()) is None
                  and check_write(shr, "", multi) is None)
            check("a non-RESUME name never warns", codex_boot_warning(os.path.join(d, "x.md"), multi) is None)
            with_cap(600)
            check("a non-shared scope's RESUME is not subject to the Codex cap",
                  check_write(r, "", "y" * 99) is None)
            check("legacy RESUME of an opted-in scope is reported as migrated",
                  "migrated to a shared store" in (legacy_refusal(
                      os.path.join(d, "claude", "projects", notes_config.encode_project_dir(d + "/sh"), "memory", "RESUME.md"),
                      sh) or "")
                  and legacy_refusal(r, sh) is None)
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
