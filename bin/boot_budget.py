#!/usr/bin/env python3
"""boot_budget.py -- the ONE home for Claude Code boot-prefix char budgets.

Claude Code loads a boot set of instruction files at session start: the global
`<CLAUDE_CONFIG_DIR>/CLAUDE.md`, plus the CLAUDE.md files from `~` down to the cwd's
scope root, each recursively `@import`-ing more files (memory indexes, RESUME notes,
running logs, ...). It warns when the TOTAL exceeds 150,000 chars. Separately,
Claude Code's NATIVE auto memory (on unless settings `autoMemoryEnabled` is false in
`settings.json` or `settings.local.json` -- the local file overrides the shared one --
or env `CLAUDE_CODE_DISABLE_AUTO_MEMORY` is one of "1"/"true"/"yes") independently loads
`<CLAUDE_CONFIG_DIR>/projects/<cwd-slug>/memory/MEMORY.md`, TRUNCATED at
`NATIVE_AUTO_MEMORY_CAP` (Claude Code reports this as ~24.4KB), for whatever scope a
session starts in -- and when THAT file is also reached via an `@import` in the
CLAUDE.md chain, it loads TWICE: once at its full size via the import, once (capped) via
native auto memory. If you @import your stores yourself, turning auto memory off is
what keeps every index from double-loading.

This script is the single source of truth for:
  - what the boot set for a given scope root actually is (import resolution)
  - the per-file and total char budgets
  - whether a scope's native auto memory store double-loads with its @import

Other scripts (`memory_hygiene_canary.py`, `memory_index_lint.py`, a pre-commit hook)
call into this module rather than re-deriving any of the above.

Scopes and budgets come from the config (see notes_config.py):
  - the home directory is always a scope ("~"), whose store is paths.global_store
  - every [[scopes]] entry adds its root, with its own store
  - budgets.* sets the per-file ceilings: the global CLAUDE.md and each scope's
    CLAUDE.md (claude_md_bytes), the global MEMORY.md (global_index_bytes), each scope's
    MEMORY.md (scope_index_bytes), and every store's RESUME.md (resume_bytes)
Nothing here is scope-specific beyond that list.

CLI:
  boot_budget.py report                 -- table per scope: file, chars, budget, flag
  boot_budget.py check                  -- exit 1 + one line per violation, silent+exit 0 clean
  boot_budget.py check-file <path> [--as <real-path>]
                                         -- exit 0 (ok/no budget) or 1 (over, message on stdout)
  boot_budget.py --selftest             -- hermetic selftest, never touches real files

Character counting: `len()` of the file decoded as UTF-8 (errors="replace") -- the same
method used to measure the budget values, so a budget and a live count are always
comparable apples-to-apples. (The config keys say "bytes"; for ASCII text the two are
equal, and a char count is what Claude Code's own warning measures.)
"""

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import notes_config  # noqa: E402

# --- Test-overridable "constants" --------------------------------------------
# These are read via helper functions (never inlined as frozen constants) so --selftest
# can monkeypatch them to point at a temp tree without touching anything real.
HOME = os.path.expanduser("~")
CONFIG_DIR_OVERRIDE = None  # set by --selftest; normally None (use env/config default)

# Overrides layered on top of the config. SCOPES = None means "from config"; a list of
# roots replaces the config's scope list outright. BUDGETS holds extra or overriding
# per-file entries ({path: chars}) merged OVER the config-derived table, so a caller can
# budget one more file (or tighten one) without a config edit.
SCOPES = None
BUDGETS = {}
DISCOVERY_ROOTS = None  # None = budgets.sweep_roots from config

# --- Defaults for keys the config may not carry --------------------------------
# Ceilings, NOT reservations: a file under budget does not "earn" headroom for another
# file, and the scope/TOTAL_LIMIT check is independent of any single file being in
# budget. A file with no budget has no per-file ceiling but still counts toward the
# total.
DEFAULT_BUDGETS = {
    "global_index_bytes": 45_000,
    "scope_index_bytes": 30_000,
    "resume_bytes": 12_000,
    "claude_md_bytes": 20_000,
}

# CLAUDE.md files OUTSIDE the scope list (a repo a session happens to work in) are swept
# too, so a folder nobody registered still gets a ceiling. This exists because an
# empty-string str.replace() in an ad-hoc edit once inflated an unregistered folder's
# CLAUDE.md from 4KB to 1.8MB and nothing noticed. 40K sits well above normal growth
# for a real CLAUDE.md, so it catches corruption without firing on everyday edits.
DEFAULT_SWEEP_ROOTS = ["~/Projects"]
DISCOVERY_MAX_DEPTH = 3
UNLISTED_CLAUDE_MD_BUDGET = 40_000
_DISCOVERY_SKIP = {"node_modules", "__pycache__", "venv"}

TOTAL_LIMIT = 150_000
WARN_AT = 145_000

# Claude Code reports native auto memory's own cap as ~24.4KB; a file loaded natively
# never contributes more than this, even when its real @import'd size is larger.
# 24.4 KiB = 24,986 bytes.
NATIVE_AUTO_MEMORY_CAP = 24_986
NATIVE_AUTO_MEMORY_LINES = 200  # native auto memory also stops at the first 200 lines


def native_load_chars(path):
    """Chars of `path` that native auto memory actually loads: the first 200 lines, at most
    NATIVE_AUTO_MEMORY_CAP chars (docs: "first 200 lines ... or the first 25KB, whichever comes
    first")."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = "".join(line for _, line in zip(range(NATIVE_AUTO_MEMORY_LINES), fh))
    except OSError:
        return 0
    return min(len(head), NATIVE_AUTO_MEMORY_CAP)

# A line whose first non-whitespace char is "@" is a Claude Code import.
_IMPORT_RE = re.compile(r"^[ \t]*@(\S.*)$")

# A fenced code block delimiter (``` or ~~~, 3+ chars, optional leading whitespace and
# a language tag after). An @-line INSIDE a fence is markdown showing example syntax,
# not a real import -- resolve_imports() must not follow it.
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


# --- Config (loaded lazily, cached; reload_config() drops the cache) ------------------
_CFG = None


def _cfg():
    global _CFG
    if _CFG is None:
        _CFG = notes_config.load()
    return _CFG


def reload_config():
    """Forget the cached config so the next lookup re-reads it (selftests use this)."""
    global _CFG
    _CFG = None


def _budget_setting(key):
    return _cfg().get("budgets", {}).get(key, DEFAULT_BUDGETS[key])


def unlisted_budget():
    return _cfg().get("budgets", {}).get("unlisted_claude_md_bytes", UNLISTED_CLAUDE_MD_BUDGET)


def discovery_roots():
    if DISCOVERY_ROOTS is not None:
        return list(DISCOVERY_ROOTS)
    return list(_cfg().get("budgets", {}).get("sweep_roots", DEFAULT_SWEEP_ROOTS))


def scope_roots():
    """Every scope root to check: "~" (the global scope) plus each configured scope."""
    if SCOPES is not None:
        return list(SCOPES)
    return ["~"] + [s["root"] for s in _cfg().get("scopes", [])]


def global_store():
    return _cfg()["paths"]["global_store"]


def budget_table():
    """{absolute path: char budget} for every budgeted boot file, BUDGETS merged last."""
    claude_md = _budget_setting("claude_md_bytes")
    resume = _budget_setting("resume_bytes")
    table = {os.path.join(_config_dir(), "CLAUDE.md"): claude_md}
    gs = global_store()
    table[os.path.join(gs, "MEMORY.md")] = _budget_setting("global_index_bytes")
    table[os.path.join(gs, "RESUME.md")] = resume
    for s in _cfg().get("scopes", []):
        table[os.path.join(s["root"], "CLAUDE.md")] = claude_md
        table[os.path.join(s["store"], "MEMORY.md")] = _budget_setting("scope_index_bytes")
        table[os.path.join(s["store"], "RESUME.md")] = resume
    table.update(BUDGETS)
    return table


# --- "~" expansion using module HOME (NOT os.path.expanduser, which reads the real
#      $HOME env and is not test-overridable) ----------------------------------------
def _tilde(path):
    path = str(path)
    if path == "~":
        return HOME
    if path.startswith("~/"):
        return os.path.join(HOME, path[2:])
    return path


def _config_dir():
    """Claude Code's config dir: the selftest override, else $CLAUDE_CONFIG_DIR (what
    Claude Code itself honors), else paths.claude_home from the config."""
    if CONFIG_DIR_OVERRIDE:
        return CONFIG_DIR_OVERRIDE
    return os.environ.get("CLAUDE_CONFIG_DIR") or _cfg()["paths"]["claude_home"]


def _display(path):
    """Render an absolute path back in '~'-relative form for the report, else as-is."""
    ap = os.path.abspath(path)
    home = os.path.abspath(HOME)
    if ap == home:
        return "~"
    if ap.startswith(home + os.sep):
        return "~" + ap[len(home):]
    return path


def char_count(path):
    try:
        return len(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return 0


# --- Import resolution ---------------------------------------------------------------
def resolve_imports(path, visited=None, order=None):
    """Recursively resolve @imports starting at `path`. Dedupe by realpath.

    `visited` / `order` are shared across a whole scope's chain resolution so the same
    file reached via two different CLAUDE.md files in the chain is only counted once.
    Returns `order` (list of the original path strings, in first-visit order).
    """
    if visited is None:
        visited = set()
    if order is None:
        order = []
    rp = os.path.realpath(path)
    if rp in visited:
        return order
    visited.add(rp)
    if not os.path.isfile(path):
        return order
    order.append(path)
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return order
    base_dir = os.path.dirname(os.path.abspath(path))
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _IMPORT_RE.match(line)
        if not m:
            continue
        raw = m.group(1).strip()
        if not raw:
            continue
        expanded = _tilde(raw)
        if not os.path.isabs(expanded):
            expanded = os.path.join(base_dir, expanded)
        expanded = os.path.normpath(expanded)
        resolve_imports(expanded, visited, order)
    return order


def boot_files_for_scope(root):
    """The boot set for a scope root: the global <CLAUDE_CONFIG_DIR>/CLAUDE.md + the
    scope's own CLAUDE.md + CLAUDE.md files of parent dirs strictly between ~ and the
    scope root, each recursively @import-resolved and deduped by realpath across the
    whole chain.

    Claude Code auto-loads BOTH <dir>/CLAUDE.md and <dir>/.claude/CLAUDE.md at every
    level of the chain, so each level below HOME contributes its .claude/CLAUDE.md right
    after its own CLAUDE.md, in that load order. This is how a project can keep its
    CLAUDE.md gitignored (a .claude/CLAUDE.md that @imports its memory store) without
    losing boot-budget coverage. The HOME level is the exception: its
    ".claude/CLAUDE.md" IS the global file already first in `chain`, so it is never
    appended a second time (resolve_imports' shared `visited` set would dedupe it by
    realpath anyway, but not adding it is the clearer fix).

    The global file is read from _config_dir(), NOT a hardcoded ~/.claude -- under a
    second CLAUDE_CONFIG_DIR (e.g. a separate login's config dir) that's the file Claude
    Code actually boots from, even when it's a symlink back to the same canonical content (realpath
    dedup in budget_for()/scope_report() still matches it correctly either way). A
    scope's OWN CLAUDE.md is always a real project file under HOME, independent of
    config dir, so only the global lookup changes.
    """
    root = os.path.normpath(_tilde(root))
    home = os.path.normpath(HOME)
    global_claude = os.path.join(_config_dir(), "CLAUDE.md")
    chain = [global_claude]

    if os.path.abspath(root) != os.path.abspath(home):
        rel = os.path.relpath(root, home)
        if rel.startswith(".."):
            # Scope root isn't under HOME at all (unusual for a configured scope, but
            # don't silently drop it): just add its own CLAUDE.md + .claude/CLAUDE.md.
            chain.append(os.path.join(root, "CLAUDE.md"))
            chain.append(os.path.join(root, ".claude", "CLAUDE.md"))
        else:
            parts = [] if rel in ("", ".") else rel.split(os.sep)
            cur = home
            for part in parts:
                cur = os.path.join(cur, part)
                chain.append(os.path.join(cur, "CLAUDE.md"))
                chain.append(os.path.join(cur, ".claude", "CLAUDE.md"))

    visited = set()
    order = []
    for f in chain:
        resolve_imports(f, visited, order)
    return order


def scope_slug(root):
    """Claude Code's projects/ folder name for a scope root (one shared encoder)."""
    return notes_config.encode_project_dir(os.path.abspath(_tilde(root)))


def native_memory_path(root):
    return os.path.join(_config_dir(), "projects", scope_slug(root), "memory", "MEMORY.md")


_DISABLE_ENV_TRUE = {"1", "true", "yes"}


def _settings_value(key):
    """Merge <config_dir>/settings.json and settings.local.json for `key`, with
    settings.local.json (checked second, so it wins) overriding the shared file.
    Returns None if neither file sets the key (or neither is readable)."""
    value = None
    for name in ("settings.json", "settings.local.json"):
        p = os.path.join(_config_dir(), name)
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if key in data:
            value = data[key]
    return value


def _project_settings_value(root, key):
    """`key` from a scope root's own .claude/settings.json then settings.local.json (the local
    file wins), or None. Claude Code ranks project settings above user settings, so a
    project's autoMemoryEnabled overrides the user-level one for sessions started there."""
    value = None
    for name in ("settings.json", "settings.local.json"):
        p = os.path.join(_tilde(root), ".claude", name)
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if key in data:
            value = data[key]
    return value


def is_auto_memory_enabled(root=None):
    """Native auto memory on for sessions started at `root` (or user-wide when root is None)."""
    env_val = os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")
    if env_val is not None and env_val.strip().lower() in _DISABLE_ENV_TRUE:
        return False
    value = _settings_value("autoMemoryEnabled")
    if root is not None:
        proj = _project_settings_value(root, "autoMemoryEnabled")
        if proj is not None:
            value = proj
    return value is not False


def budget_for(path):
    """Return the per-file budget for `path`, matched by realpath against budget_table()
    keys. None if unbudgeted."""
    rp = os.path.realpath(os.path.abspath(_tilde(str(path))))
    for key, val in budget_table().items():
        krp = os.path.realpath(os.path.abspath(_tilde(key)))
        if krp == rp:
            return val
    return None


def scope_report(root):
    """Return a dict describing one scope's boot set: files (with chars/budget), the
    scope total (with the native-auto memory double-load folded in when it applies),
    and which file(s) double-loaded.

    The native auto memory load is CAPPED (NATIVE_AUTO_MEMORY_CAP, ~24.4KB per
    Claude Code) even when the @import'd copy is larger, so a doubled file adds
    min(chars, NATIVE_AUTO_MEMORY_CAP) for its second load, not its full size twice.
    """
    files = boot_files_for_scope(root)
    native = native_memory_path(root)
    native_rp = os.path.realpath(native) if os.path.isfile(native) else None
    auto_on = is_auto_memory_enabled(root)

    entries = []
    total = 0
    double_load = []
    for f in files:
        if not os.path.isfile(f):
            continue
        chars = char_count(f)
        budget = budget_for(f)
        doubled = bool(auto_on and native_rp and os.path.realpath(f) == native_rp)
        entries.append({"path": f, "chars": chars, "budget": budget, "doubled": doubled})
        total += chars
        if doubled:
            total += native_load_chars(f)
            double_load.append(f)

    # A scope that lets native auto memory load its MEMORY.md instead of @importing it: count
    # that load (capped) so the boot total is true and the store reads as reached.
    if auto_on and native_rp and not any(os.path.realpath(e["path"]) == native_rp for e in entries):
        chars = native_load_chars(native)
        entries.append({"path": native, "chars": chars, "budget": budget_for(native), "doubled": False,
                        "native": True})
        total += chars

    return {
        "root": root,
        "entries": entries,
        "total": total,
        "double_load": double_load,
        "native_memory_path": native,
        "auto_memory_enabled": auto_on,
    }


def all_scope_reports():
    reports = []
    for root in scope_roots():
        abs_root = _tilde(root)
        if not os.path.isdir(abs_root):
            continue
        reports.append(scope_report(root))
    return reports


def discovered_claude_mds():
    """Every CLAUDE.md under discovery_roots(), at most DISCOVERY_MAX_DEPTH dirs down,
    skipping hidden and dependency dirs. Sorted absolute paths.

    Also picks up a bare <dir>/.claude/CLAUDE.md (consistent with boot_files_for_scope:
    Claude Code loads it the same as <dir>/CLAUDE.md). This is a direct, one-level-only
    peek inside ".claude": ".claude" itself is still never added to `dirs`, so os.walk
    never recurses INTO it (no sweep of .claude/worktrees, .claude/projects, etc; those
    are scratch/worktree trees, not boot files, and would flood this sweep if walked).
    """
    found = []
    for root in discovery_roots():
        base = _tilde(root)
        if not os.path.isdir(base):
            continue
        base_depth = base.rstrip(os.sep).count(os.sep)
        for cur, dirs, files in os.walk(base):
            if cur.count(os.sep) - base_depth >= DISCOVERY_MAX_DEPTH:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _DISCOVERY_SKIP]
            if "CLAUDE.md" in files:
                found.append(os.path.join(cur, "CLAUDE.md"))
            dotclaude_md = os.path.join(cur, ".claude", "CLAUDE.md")
            if os.path.isfile(dotclaude_md):
                found.append(dotclaude_md)
    return sorted(found)


def file_violations():
    """One (path, chars, budget) tuple per FILE over its own budget, deduped by
    realpath across scopes.

    A file @imported into EVERY scope's chain (the global CLAUDE.md, the global
    MEMORY.md/RESUME.md) appears once in each scope's `entries` list, so looping
    all_scope_reports() directly would flag it once per scope that reaches it (ten
    duplicate lines with ten scopes configured). check_mode() goes through this one function
    instead of re-deriving the loop, so a global violation is reported exactly once.
    Order: the first scope (in scope_roots() order) that reaches the file wins the
    position.
    """
    seen = set()
    out = []
    for rep in all_scope_reports():
        for e in rep["entries"]:
            if e["budget"] is None or e["chars"] <= e["budget"]:
                continue
            rp = os.path.realpath(e["path"])
            if rp in seen:
                continue
            seen.add(rp)
            out.append((e["path"], e["chars"], e["budget"]))
    return out


def unlisted_claude_md_findings():
    """One finding per discovered CLAUDE.md over its budget (its budget_table() entry,
    else unlisted_budget()). Files already in a scope's boot set are skipped: the scope
    checks own them. [] when clean."""
    covered = set()
    for rep in all_scope_reports():
        covered.update(os.path.realpath(e["path"]) for e in rep["entries"])
    findings = []
    for f in discovered_claude_mds():
        if os.path.realpath(f) in covered:
            continue
        budget = budget_for(f) or unlisted_budget()
        chars = char_count(f)
        if chars > budget:
            findings.append(f"FILE {_display(f)}: {chars:,} chars over budget {budget:,} (unlisted CLAUDE.md)")
    return findings


# --- check-file ------------------------------------------------------------------------
def check_file(path, as_path=None):
    """Check the CONTENT at `path` against the budget for `as_path` (default: `path`
    itself). Returns (ok, message). ok is True when under budget OR unbudgeted; message
    is None unless over."""
    budget_key_path = as_path if as_path else path
    budget = budget_for(budget_key_path)
    if budget is None:
        return True, None
    chars = char_count(path)
    if chars > budget:
        label = _display(_tilde(str(budget_key_path)))
        msg = (
            f"{label}: {chars:,} chars over budget {budget:,} -- "
            "move detail to a linked doc; keep a one-line index entry"
        )
        return False, msg
    return True, None


# --- CLI modes ---------------------------------------------------------------------
def report_mode():
    reports = all_scope_reports()
    if not reports:
        print("No scope roots found on disk.")
        return 0
    for rep in reports:
        print(f"== scope: {_display(_tilde(rep['root']))} ==")
        for e in rep["entries"]:
            budget = e["budget"]
            if budget is None:
                flag = "-"
            elif e["chars"] > budget:
                flag = "OVER"
            else:
                flag = "ok"
            doubled = "  [DOUBLE-LOADS via native auto memory]" if e["doubled"] else ""
            if e.get("native"):
                doubled = "  [loaded natively, not @imported]"
            budget_s = f"{budget:,}" if budget is not None else "-"
            print(f"  {_display(e['path']):<70} {e['chars']:>7,} / {budget_s:>7}  {flag}{doubled}")
        # WARN_AT < TOTAL_LIMIT always (enforced by the selftest regression guard), so
        # total >= WARN_AT is the only over-state worth a distinct flag here.
        flag = "OVER WARN_AT" if rep["total"] >= WARN_AT else "ok"
        print(f"  TOTAL: {rep['total']:,} / limit {TOTAL_LIMIT:,} (warn at {WARN_AT:,})  {flag}")
        if rep["double_load"]:
            names = ", ".join(_display(f) for f in rep["double_load"])
            print(f"  double-load (native auto memory + @import): {names}")
        print()
    return 0


def check_mode():
    reports = all_scope_reports()
    violations = []
    for rep in reports:
        if rep["total"] >= WARN_AT:
            violations.append(
                f"SCOPE {_display(_tilde(rep['root']))}: total {rep['total']:,} chars "
                f">= warn threshold {WARN_AT:,} (limit {TOTAL_LIMIT:,})"
            )
    for path, chars, budget in file_violations():
        violations.append(f"FILE {_display(path)}: {chars:,} chars over budget {budget:,}")
    violations.extend(unlisted_claude_md_findings())
    if not violations:
        return 0  # silent when clean
    for v in violations:
        print(v)
    return 1


def check_file_mode(path, as_path=None):
    ok, msg = check_file(path, as_path)
    if ok:
        return 0
    print(msg)
    return 1


# --- Selftest ------------------------------------------------------------------------
def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def selftest():
    ok = True

    def check(cond, msg):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"  [{status}] {msg}")

    global HOME, CONFIG_DIR_OVERRIDE, SCOPES, BUDGETS, TOTAL_LIMIT, WARN_AT
    orig_home, orig_cfg = HOME, CONFIG_DIR_OVERRIDE
    orig_scopes, orig_budgets = SCOPES, dict(BUDGETS)
    orig_total, orig_warn = TOTAL_LIMIT, WARN_AT
    orig_env_disable = os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
    orig_env_cfgdir = os.environ.pop("CLAUDE_CONFIG_DIR", None)
    orig_env_config = os.environ.get("SESSION_NOTES_CONFIG")

    try:
        with tempfile.TemporaryDirectory() as d:
            tmp_home = Path(d) / "home"
            tmp_home.mkdir()
            HOME = str(tmp_home)
            CONFIG_DIR_OVERRIDE = str(tmp_home / ".claude")

            # --- Build a small fake boot tree ---
            # Slugs are derived from the REAL scope_slug() function (not hand-rolled) so
            # the fixture stays honest about what native_memory_path() will compute.
            global_slug = scope_slug("~")
            widget_root = tmp_home / "Projects" / "Widget"
            widget_slug = scope_slug(str(widget_root))
            global_claude = tmp_home / ".claude" / "CLAUDE.md"
            global_store_dir = tmp_home / ".claude" / "projects" / global_slug / "memory"
            global_mem = global_store_dir / "MEMORY.md"
            global_resume = global_store_dir / "RESUME.md"
            scope_claude = widget_root / "CLAUDE.md"
            scope_mem = tmp_home / ".claude" / "projects" / widget_slug / "memory" / "MEMORY.md"

            _write(global_claude, f"# global\n@{global_mem}\n@{global_resume}\n")
            _write(global_mem, "x" * 50)
            _write(global_resume, "y" * 20)
            _write(scope_claude, f"# scope\n@{scope_mem}\n")
            _write(scope_mem, "z" * 30)
            _write(tmp_home / ".claude" / "settings.json", "{}")

            # Scopes and budgets come from a real (temp) config, so the
            # config-to-budget mapping is exercised end to end. The scope's store is
            # left empty so notes_config derives it, which must land on the same
            # path native auto memory reads (the double-load cases below rely on it).
            cfg_file = Path(d) / "config.toml"
            _write(cfg_file, (
                "[paths]\n"
                f'claude_home = "{tmp_home / ".claude"}"\n'
                f'global_store = "{global_store_dir}"\n'
                "[budgets]\n"
                "global_index_bytes = 100\n"
                "scope_index_bytes = 100\n"
                "resume_bytes = 100\n"
                "claude_md_bytes = 1000\n"
                "[[scopes]]\n"
                'name = "widget"\n'
                f'root = "{widget_root}"\n'
            ))
            os.environ["SESSION_NOTES_CONFIG"] = str(cfg_file)
            reload_config()
            SCOPES = None
            BUDGETS = {}
            TOTAL_LIMIT = 150_000
            WARN_AT = 145_000

            # --- config mapping ---
            check(scope_roots() == ["~", str(widget_root)],
                  "scope_roots(): '~' plus each configured scope root")
            table = budget_table()
            check(table.get(str(global_mem)) == 100, "global MEMORY.md budget comes from global_index_bytes")
            check(table.get(str(scope_mem)) == 100, "scope MEMORY.md budget comes from scope_index_bytes (derived store)")
            check(table.get(str(global_resume)) == 100, "RESUME.md budget comes from resume_bytes")
            check(table.get(str(scope_claude)) == 1000, "scope CLAUDE.md budget comes from claude_md_bytes")
            check(table.get(str(global_claude)) == 1000, "global CLAUDE.md budget comes from claude_md_bytes")
            BUDGETS[str(global_resume)] = 7
            check(budget_for(str(global_resume)) == 7, "a BUDGETS override wins over the config-derived entry")
            del BUDGETS[str(global_resume)]

            # --- resolve_imports: basic + recursive ---
            order = resolve_imports(str(global_claude))
            check(str(global_claude) in order, "resolve_imports includes the root file")
            check(str(global_mem) in order, "resolve_imports follows an @import")
            check(str(global_resume) in order, "resolve_imports follows a second @import")

            # --- resolve_imports: cycle/dedup guard ---
            a = tmp_home / "a.md"
            b = tmp_home / "b.md"
            _write(a, f"@{b}\n")
            _write(b, f"@{a}\n")  # cycle back to a
            order2 = resolve_imports(str(a))
            check(order2.count(str(a)) == 1, "cyclic imports don't infinite-loop or double-count")
            check(str(b) in order2, "cyclic imports still resolve the other side once")

            # --- resolve_imports: an @-line inside a fenced code block is NOT an import ---
            fenced = tmp_home / "fenced.md"
            real_target = tmp_home / "real_target.md"
            _write(real_target, "q" * 5)
            fenced_text = (
                "# doc\n\n"
                "Example usage:\n"
                "```bash\n"
                f"@{tmp_home / 'not_a_real_import.md'}\n"
                "```\n\n"
                f"@{real_target}\n"  # this one IS a real import, outside the fence
            )
            _write(fenced, fenced_text)
            order_fence = resolve_imports(str(fenced))
            check(
                str(tmp_home / "not_a_real_import.md") not in order_fence,
                "an @-line inside a fenced code block is skipped, not followed",
            )
            check(
                str(real_target) in order_fence,
                "an @-line outside the fence is still followed as a real import",
            )

            # --- boot_files_for_scope: global-only for '~' ---
            root_files = boot_files_for_scope("~")
            check(str(global_claude) in root_files, "~ scope includes global CLAUDE.md")
            check(str(scope_claude) not in root_files, "~ scope excludes an unrelated scope's CLAUDE.md")

            # --- boot_files_for_scope: nested scope pulls in global + its own chain ---
            widget_files = boot_files_for_scope(str(widget_root))
            check(str(global_claude) in widget_files, "nested scope still includes global CLAUDE.md")
            check(str(scope_claude) in widget_files, "nested scope includes its own CLAUDE.md")
            check(str(scope_mem) in widget_files, "nested scope resolves its own @import")

            # --- boot_files_for_scope: HOME level's .claude/CLAUDE.md is the global file
            # already in `chain`, never appended (and counted) a second time ---
            check(
                root_files.count(str(global_claude)) == 1,
                "HOME level's .claude/CLAUDE.md is not double-counted against the global CLAUDE.md",
            )

            # --- boot_files_for_scope: a scope whose ONLY CLAUDE.md lives at
            # .claude/CLAUDE.md (a project that keeps its CLAUDE.md gitignored) is still
            # read. Without this, such a scope measures as if its store were never
            # imported at all. ---
            dotclaude_scope = tmp_home / "Projects" / "DotOnly"
            dotclaude_claude = dotclaude_scope / ".claude" / "CLAUDE.md"
            dotclaude_store = (
                tmp_home / ".claude" / "projects" / scope_slug(str(dotclaude_scope)) / "memory" / "MEMORY.md"
            )
            _write(dotclaude_claude, f"# dotclaude\n@{dotclaude_store}\n")
            _write(dotclaude_store, "d" * 40)

            dotonly_files = boot_files_for_scope(str(dotclaude_scope))
            check(
                str(dotclaude_claude) in dotonly_files,
                "boot_files_for_scope reads a scope's .claude/CLAUDE.md",
            )
            check(
                str(dotclaude_store) in dotonly_files,
                "boot_files_for_scope follows an @import reached only via a .claude/CLAUDE.md",
            )

            rep_dotonly = scope_report(str(dotclaude_scope))
            store_entries = [e for e in rep_dotonly["entries"] if e["path"] == str(dotclaude_store)]
            check(
                len(store_entries) == 1 and rep_dotonly["total"] >= char_count(dotclaude_store),
                "the .claude/CLAUDE.md's @import target is counted in the scope total",
            )

            # --- discovered_claude_mds: also sees a bare .claude/CLAUDE.md, not just
            # <dir>/CLAUDE.md (consistent with boot_files_for_scope above), but still
            # never recurses INTO .claude (a file under .claude/worktrees/... is not
            # found) ---
            unlisted_dotclaude = tmp_home / "Projects" / "UnlistedDot" / ".claude" / "CLAUDE.md"
            _write(unlisted_dotclaude, "# unlisted dotclaude\n")
            scratch_in_dotclaude = (
                tmp_home / "Projects" / "UnlistedDot" / ".claude" / "worktrees" / "wt1" / "CLAUDE.md"
            )
            _write(scratch_in_dotclaude, "# should not be swept\n")
            found_paths = discovered_claude_mds()
            check(
                str(unlisted_dotclaude) in found_paths,
                "discovered_claude_mds also finds a project's bare .claude/CLAUDE.md",
            )
            check(
                str(scratch_in_dotclaude) not in found_paths,
                "discovered_claude_mds does not recurse into .claude (worktree/scratch dirs inside it are not swept)",
            )
            check(
                str(global_claude) not in found_paths,
                "discovered_claude_mds (sweep roots under ~/Projects) never reports the global CLAUDE.md",
            )
            shutil.rmtree(tmp_home / "Projects" / "UnlistedDot")
            shutil.rmtree(dotclaude_scope)

            # --- budget_for ---
            check(budget_for(str(global_mem)) == 100, "budget_for matches an exact real path")
            check(budget_for(str(tmp_home / "nope.md")) is None, "budget_for returns None for an unbudgeted file")

            # --- Dedup: a file @imported into EVERY scope's chain (here the global
            # RESUME.md) is over budget in BOTH the "~" and the widget scope's entries
            # (positive control that the duplicate exists before any dedup), but
            # file_violations(), the one source check_mode() consumes, reports it
            # exactly once ---
            _write(global_resume, "y" * 200)  # over its 100-char resume_bytes budget
            dup_count = sum(
                1
                for rep in all_scope_reports()
                for e in rep["entries"]
                if e["path"] == str(global_resume) and e["budget"] is not None and e["chars"] > e["budget"]
            )
            check(
                dup_count >= 2,
                "positive control: an over-budget global file is counted by >=2 scopes before dedup",
            )
            # Looked up via globals() so this assertion reports FAIL (not a NameError
            # crash) if the function is ever removed.
            fv = globals().get("file_violations")
            dedup_matches = [v for v in fv() if v[0] == str(global_resume)] if fv else []
            check(
                len(dedup_matches) == 1,
                "file_violations() reports a global over-budget file exactly once",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_dedup = check_mode()
            printed = buf.getvalue().splitlines()
            check(
                rc_dedup == 1
                and sum(1 for ln in printed if _display(str(global_resume)) in ln) == 1,
                "check_mode()'s printed output names the global over-budget file exactly once",
            )
            _write(global_resume, "y" * 20)  # restore

            # --- is_auto_memory_enabled: default True, explicit False, env override ---
            check(is_auto_memory_enabled() is True, "auto memory defaults on with empty settings")
            _write(tmp_home / ".claude" / "settings.json", '{"autoMemoryEnabled": false}')
            check(is_auto_memory_enabled() is False, "autoMemoryEnabled: false is honored")
            _write(tmp_home / ".claude" / "settings.json", "{}")

            # Env var: only "1"/"true"/"yes" (case-insensitive) disable; anything else
            # (e.g. "0", "false", an unrelated truthy-looking string) does NOT.
            for v in ("1", "true", "TRUE", "yes", "Yes"):
                os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = v
                check(is_auto_memory_enabled() is False, f"env value {v!r} disables auto memory")
            for v in ("0", "false", "no", "banana"):
                os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = v
                check(is_auto_memory_enabled() is True, f"env value {v!r} does NOT disable auto memory")
            del os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"]

            # --- settings.local.json overrides settings.json ---
            _write(tmp_home / ".claude" / "settings.json", '{"autoMemoryEnabled": true}')
            _write(tmp_home / ".claude" / "settings.local.json", '{"autoMemoryEnabled": false}')
            check(
                is_auto_memory_enabled() is False,
                "settings.local.json's autoMemoryEnabled overrides settings.json's",
            )
            _write(tmp_home / ".claude" / "settings.json", '{"autoMemoryEnabled": false}')
            _write(tmp_home / ".claude" / "settings.local.json", '{"autoMemoryEnabled": true}')
            check(
                is_auto_memory_enabled() is True,
                "settings.local.json can also override settings.json back to enabled",
            )
            os.remove(tmp_home / ".claude" / "settings.local.json")
            _write(tmp_home / ".claude" / "settings.json", "{}")

            # --- a scope's own .claude settings override the user-level autoMemoryEnabled ---
            nat_root = "~/Projects/NativeScope"
            nat_dir = tmp_home / "Projects" / "NativeScope"
            _write(nat_dir / "CLAUDE.md", "# no memory import: native auto memory loads it\n")
            nat_mem = Path(native_memory_path(nat_root))
            _write(nat_mem, "- [x](x.md) one memory\n")
            _write(tmp_home / ".claude" / "settings.json", '{"autoMemoryEnabled": false}')
            check(is_auto_memory_enabled(nat_root) is False, "no project setting: the user-level false applies")
            rep_off = scope_report(nat_root)
            check(not any(os.path.realpath(e["path"]) == os.path.realpath(nat_mem) for e in rep_off["entries"]),
                  "auto memory off: an un-imported store is not counted as loading")
            _write(nat_dir / ".claude" / "settings.json", '{"autoMemoryEnabled": true}')
            check(is_auto_memory_enabled(nat_root) is True, "a project's autoMemoryEnabled: true overrides the user's false")
            check(is_auto_memory_enabled() is False, "...for that project only: user-wide stays off")
            rep_on = scope_report(nat_root)
            native_entries = [e for e in rep_on["entries"] if e.get("native")]
            check(len(native_entries) == 1 and os.path.realpath(native_entries[0]["path"]) == os.path.realpath(nat_mem),
                  "auto memory on and not @imported: the natively loaded MEMORY.md is in the boot set")
            check(rep_on["total"] == rep_off["total"] + char_count(nat_mem), "...and counted in the scope total")
            check(not rep_on["double_load"], "...and not reported as a double load")
            _write(nat_dir / ".claude" / "settings.local.json", '{"autoMemoryEnabled": false}')
            check(is_auto_memory_enabled(nat_root) is False, "a project's settings.local.json overrides its settings.json")
            _write(tmp_home / ".claude" / "settings.json", "{}")

            # --- native load stops at 200 lines even when the file is far under the char cap ---
            long_idx = tmp_home / "long_index.md"
            _write(long_idx, "".join("- line %d\n" % i for i in range(300)))
            first200 = "".join("- line %d\n" % i for i in range(200))
            check(native_load_chars(long_idx) == len(first200), "native load counts the first 200 lines only")

            # --- double-load: native store IS the @import'd scope memory file ---
            native_widget = Path(native_memory_path(str(widget_root)))
            check(native_widget == scope_mem, "native memory path for the scope matches the @import target (positive control setup)")

            rep = scope_report(str(widget_root))
            check(str(scope_mem) in rep["double_load"], "double-load detected when native store == an @import target")
            single_count_total = sum(e["chars"] for e in rep["entries"])
            check(
                rep["total"] == single_count_total + char_count(scope_mem),
                "double-loaded file's chars counted twice in the scope total",
            )

            # Now disable auto memory and confirm the double no longer applies.
            _write(tmp_home / ".claude" / "settings.json", '{"autoMemoryEnabled": false}')
            rep2 = scope_report(str(widget_root))
            check(not rep2["double_load"], "double-load cleared when autoMemoryEnabled is false")
            check(
                rep2["total"] == sum(e["chars"] for e in rep2["entries"]),
                "total drops to single-count when auto memory is off",
            )
            _write(tmp_home / ".claude" / "settings.json", "{}")

            # --- double-load is CAPPED at NATIVE_AUTO_MEMORY_CAP, not counted at full size ---
            orig_cap = NATIVE_AUTO_MEMORY_CAP
            globals()["NATIVE_AUTO_MEMORY_CAP"] = 40  # tiny cap for the test
            try:
                _write(scope_mem, "w" * 200)  # real size (200) is well over the 40-char cap
                rep_cap = scope_report(str(widget_root))
                check(
                    str(scope_mem) in rep_cap["double_load"],
                    "over-cap file still detected as double-loading",
                )
                single_count_total_cap = sum(e["chars"] for e in rep_cap["entries"])
                check(
                    rep_cap["total"] == single_count_total_cap + 40,
                    "doubled file over the native cap adds exactly the cap (40), not its full size (200)",
                )
            finally:
                globals()["NATIVE_AUTO_MEMORY_CAP"] = orig_cap
                _write(scope_mem, "z" * 30)  # restore

            # --- positive control: over-budget file is flagged ---
            _write(scope_mem, "z" * 500)  # now over its 100-char budget
            rep3 = scope_report(str(widget_root))
            over_entries = [e for e in rep3["entries"] if e["path"] == str(scope_mem)]
            check(len(over_entries) == 1 and over_entries[0]["chars"] > over_entries[0]["budget"],
                  "positive control: an over-budget file is measured over its budget")
            _write(scope_mem, "z" * 30)  # restore

            # --- check_mode / report_mode: clean vs dirty (positive control) ---
            check(check_mode() == 0, "check exits 0 clean (silent) on an in-budget tree")

            _write(scope_mem, "z" * 500)  # positive control: force a violation
            rc = check_mode()
            check(rc == 1, "check exits 1 when a file is over its budget (positive control)")
            _write(scope_mem, "z" * 30)

            rc_report = report_mode()
            check(rc_report == 0, "report_mode runs cleanly (exit 0) over the fake tree")

            # --- check-file, incl. --as ---
            # (named ok1, not ok: rebinding `ok` here would reset the selftest's own
            # pass/fail flag and hide every earlier FAIL behind a final PASS)
            ok1, msg = check_file(str(global_mem))
            check(ok1 and msg is None, "check_file: in-budget file is ok, silent")

            tmp_probe = Path(d) / "probe.md"
            _write(tmp_probe, "x" * 500)  # content of an unrelated temp file
            ok2, msg2 = check_file(str(tmp_probe))
            check(ok2, "check_file: a path with no known budget is ok (no budget = pass)")

            ok3, msg3 = check_file(str(tmp_probe), as_path=str(global_mem))
            check(not ok3 and msg3 is not None,
                  "check_file --as: staged content checked against a DIFFERENT real path's budget (positive control)")

            ok4, msg4 = check_file(str(a), as_path=str(global_mem))
            check(ok4, "check_file --as: small staged content under the target's budget passes")

            check(check_file_mode(str(tmp_probe), str(global_mem)) == 1,
                  "check_file_mode --as exits 1 on an over-budget staged blob")
            check(check_file_mode(str(a), str(global_mem)) == 0,
                  "check_file_mode --as exits 0 on an in-budget staged blob")

            # --- main(): a crash in dispatch returns 2, never masquerades as exit 1 ---
            # Patch via globals() (not a fresh `import boot_budget`, which would bind a
            # SEPARATE module object when this file is run directly as __main__ and
            # never actually affect what main() calls) so the swap reaches the exact
            # name main() looks up.
            def _boom(*_a, **_kw):
                raise RuntimeError("injected selftest failure")

            orig_check_file_mode = globals()["check_file_mode"]
            globals()["check_file_mode"] = _boom
            try:
                rc_main = main(["check-file", str(global_mem)])
            finally:
                globals()["check_file_mode"] = orig_check_file_mode
            check(rc_main == 2, "main() returns 2 (not 0/1) when dispatch raises (positive control)")

            # A normal, non-crashing invocation through main() still works end to end.
            check(main(["check-file", str(global_mem)]) == 0, "main() check-file: normal path still returns 0")

            # --- Unlisted CLAUDE.md sweep (DISCOVERY_ROOTS) ---
            proj = tmp_home / "Projects"
            _write(proj / "Small" / "CLAUDE.md", "# fine\n")
            check(unlisted_claude_md_findings() == [], "unlisted sweep: clean tree yields no findings")
            # The failure this sweep exists for: str.replace("", note) puts the note
            # between every char.
            bloated = ("# Notes\n" + "- a line\n" * 20).replace("", "- a long note line " * 20 + "\n")
            _write(proj / "Bloated" / "CLAUDE.md", bloated)
            found = unlisted_claude_md_findings()
            check(len(found) == 1 and "Bloated" in found[0],
                  "unlisted sweep: an empty-string-replace blowup in an unregistered repo is flagged (positive control)")
            check(check_mode() == 1, "check_mode exits 1 on an unlisted over-budget CLAUDE.md")
            (proj / "Bloated" / "CLAUDE.md").unlink()
            _write(proj / ".hidden" / "CLAUDE.md", "x" * (unlisted_budget() + 1))
            _write(proj / "a" / "b" / "c" / "d" / "CLAUDE.md", "x" * (unlisted_budget() + 1))
            check(unlisted_claude_md_findings() == [],
                  "unlisted sweep: hidden dirs and dirs past DISCOVERY_MAX_DEPTH are not walked")
            _write(proj / "a" / "b" / "CLAUDE.md", "x" * (unlisted_budget() + 1))
            check(len(unlisted_claude_md_findings()) == 1,
                  "unlisted sweep: a dir within DISCOVERY_MAX_DEPTH is walked (positive control for the line above)")
            (proj / "a" / "b" / "CLAUDE.md").unlink()

        # --- Regression guard: WARN_AT must stay reachable below TOTAL_LIMIT ---
        check(orig_warn < orig_total, "WARN_AT stays strictly below TOTAL_LIMIT in the real config")
        check(orig_total == 150_000, "TOTAL_LIMIT matches Claude Code's documented boot-prefix limit")

    finally:
        HOME = orig_home
        CONFIG_DIR_OVERRIDE = orig_cfg
        SCOPES = orig_scopes
        BUDGETS.clear()
        BUDGETS.update(orig_budgets)
        TOTAL_LIMIT = orig_total
        WARN_AT = orig_warn
        os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        if orig_env_disable is not None:
            os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = orig_env_disable
        if orig_env_cfgdir is not None:
            os.environ["CLAUDE_CONFIG_DIR"] = orig_env_cfgdir
        if orig_env_config is None:
            os.environ.pop("SESSION_NOTES_CONFIG", None)
        else:
            os.environ["SESSION_NOTES_CONFIG"] = orig_env_config
        reload_config()

    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="run the built-in hermetic selftest")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("report", help="table per scope: file, chars, budget, flag")
    sub.add_parser("check", help="exit 1 + one line per violation; silent + exit 0 when clean")

    cf = sub.add_parser("check-file", help="check one file's content against a budget")
    cf.add_argument("path", help="the file whose CONTENT to measure")
    cf.add_argument("--as", dest="as_path", default=None,
                     help="check that content against THIS real path's budget instead of path's own")

    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    # A crash here must NEVER look like exit 1 ("over budget") to a caller (e.g. a
    # pre-commit hook branching on the exit code) -- exit 2 is reserved for "this tool
    # itself broke," distinct from both 0 (ok) and 1 (a real over-budget finding).
    try:
        if args.cmd == "report":
            return report_mode()
        if args.cmd == "check":
            return check_mode()
        if args.cmd == "check-file":
            return check_file_mode(args.path, args.as_path)
    except Exception as e:  # noqa: BLE001 -- deliberately broad, see comment above
        print(f"boot_budget: internal error ({type(e).__name__}): {e}", file=sys.stderr)
        return 2

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
