#!/usr/bin/env python3
"""memory_hygiene_canary.py -- deterministic hygiene checker for /reflect memory stores.

A pure-Python, no-LLM, no-network sanity check over every memory store the
config knows about: `paths.global_store` plus each `[[scopes]]` entry's `store`. It
looks for the cruft and drift that accumulate as /reflect writes to a store:

  1. Orphan files        -- a *.md on disk not referenced by any MEMORY.md OR
                           REFERENCE.md index line.
  2. Dangling entries    -- a MEMORY.md or REFERENCE.md index line whose linked file.md
                           does not exist.
  3. Duplicate entries   -- the same file.md referenced by more than one index line
                           within the SAME index file.
  4. Duplicate slugs     -- >1 file resolving to the same `name:` frontmatter slug.
  5. Frontmatter drift   -- top-level `type:` instead of the canonical `metadata: type:`
                           nesting, or a file missing `name:`/`description:` entirely.
  6. Count mismatch      -- files-on-disk (excl MEMORY/RESUME/REFERENCE) vs distinct
                           index entries (derived/summary; folded into the report header).
  7. Index bloat         -- MEMORY.md over its store's size threshold (the config's
                           `budgets.global_index_bytes` for the global store,
                           `budgets.scope_index_bytes` for a scope store). This is the
                           "time to run /reflect consolidate" trigger.
  8. Both-indexed        -- a file.md indexed in BOTH MEMORY.md and REFERENCE.md at once
                           (it should be in exactly one tier).
  9. REFERENCE.md        -- a REFERENCE.md that IS reached by @import from a CLAUDE.md
     imported              entry point. It must never be: the whole point of the cold
                           tier is that it does not pay boot-prefix rent.
 10. Orphan STORE        -- a whole store (`<claude_home>/projects/<slug>/memory/`) that
                           is non-empty per _non_empty_stores() (real memory files, or a
                           real RESUME.md narrative) but whose MEMORY.md no CLAUDE.md
                           entry point @imports. Everything it holds loads NOWHERE: the
                           store-level version of check 1's per-file orphan. Spans every
                           store found on disk, not just the configured ones, since an
                           unconfigured store is exactly the kind nobody is watching.

Checks 9 and 10 follow @imports with boot_budget.resolve_imports(), the one home for
import-resolution rules (fenced code blocks skipped, realpath dedupe), so the two scripts
can never disagree about what loads. Entry points are `<claude_home>/CLAUDE.md`,
`~/CLAUDE.md`, and each configured scope root's CLAUDE.md. Whole-scope boot-prefix
budgets are boot_budget.py's own job (`boot_budget.py check`); this canary does not
re-derive them.

Why check 7 exists. Checks 1-6 are all REFERENTIAL INTEGRITY, so a store that is
perfectly well-formed and enormous reads as CLEAN. In practice an index grew steadily
(over 1k chars a day, no cap anywhere) until it tripped Claude Code's own 40k per-file
startup warning for a large memory file, while this canary stayed correctly silent the whole time: a registry only
answers questions about what it registered. A separate dated "consolidate monthly"
reminder inherited the same blind spot (it said "no hygiene report means the store is
clean", which on an oversized store was affirmatively wrong). So the canary itself is the
consolidate reminder, rather than a calendar entry.

THRESHOLD RATIONALE -- do not "tighten" this to 40k. Claude Code warns when one memory file passes 40k (separate
from the 150k total boot limit boot_budget.py checks), but that
warning is a non-configurable floor that never truncates (cosmetic), and a realistic
consolidate pass on a large store lands around 45-60k, not under 40k. A threshold below
what consolidation can actually reach never goes silent, and a canary that always fires
is worse than none (silence must mean all-clear). Equally, a threshold set far above
the size consolidation reaches blinds the only consolidate trigger. The number lives in
the config's `budgets` table, the same place boot_budget.py reads it, so it can only
drift in one place.

Intentionally NOT flagged: broken `[[wikilinks]]`. Per the memory spec a `[[name]]`
with no matching file yet is intentional (it marks something worth writing), so it is
not an error.

Behavior:
  - Silent when clean: no stdout, exit 0.
  - On findings: print a concise Markdown report to stdout and exit 3. Nothing is
    written or sent anywhere else; a scheduler or wrapper that wants an alert keys on
    the exit code and forwards stdout.
  - `--selftest`: build tiny temp stores seeded with one instance of each drift class,
    assert each is detected and a clean store yields zero findings / exit 0. Hermetic:
    every path comes from a synthetic config pointed at a temp dir.
  - `--report` (default): run against the configured stores.
"""

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import notes_config  # noqa: E402

# Exit code on findings (distinct from 1, which a crash or bad config produces).
EXIT_FINDINGS = 3

# Fallback thresholds if a config predates the budgets table. Check 7 reads
# cfg["budgets"]["global_index_bytes"] / ["scope_index_bytes"] first.
DEFAULT_GLOBAL_INDEX_THRESHOLD = 45_000
DEFAULT_SCOPE_INDEX_THRESHOLD = 30_000

# Growth rate is best-effort context on the bloat finding, read from git history when
# the store happens to live in a git repo. Never load-bearing: if git is unavailable or
# the file has no history, the finding still fires on size alone.
GROWTH_LOOKBACK_DAYS = 30

# Files that are structural, not indexed memory entries. RESUME-archive.md is where
# /reflect Step 5b hand-trims finished RESUME narrative.
NON_MEMORY = {"MEMORY.md", "RESUME.md", "REFERENCE.md", "RESUME-archive.md"}

# Check 10 (store-level, not index-level): a store holding only structural files
# and no actual memory files has nothing worth flagging as "unreachable".
STRUCTURAL = NON_MEMORY

# Markdown link target ending in .md, e.g. `- [Title](file.md) -- hook`.
# Requires the `](` so prose parentheticals like "(see foo.md)" are never matched.
INDEX_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")


# --- Config ---------------------------------------------------------------------
def configured_stores(cfg):
    """[(label, store_path, threshold)] for the global store and every scope store,
    deduped by realpath (a scope may point its store at the global one)."""
    budgets = cfg.get("budgets", {})
    g_thr = budgets.get("global_index_bytes", DEFAULT_GLOBAL_INDEX_THRESHOLD)
    s_thr = budgets.get("scope_index_bytes", DEFAULT_SCOPE_INDEX_THRESHOLD)
    out, seen = [], set()
    entries = [("global", cfg["paths"]["global_store"], g_thr)]
    entries += [(s["name"], s["store"], s_thr) for s in cfg.get("scopes", [])]
    for label, store, thr in entries:
        rp = os.path.realpath(store)
        if rp in seen:
            continue
        seen.add(rp)
        out.append((label, Path(store), thr))
    return out


def entry_points(cfg):
    """CLAUDE.md files Claude Code may boot from, as far as the config knows."""
    home = os.path.expanduser("~")
    pts = [os.path.join(cfg["paths"]["claude_home"], "CLAUDE.md"),
           os.path.join(home, "CLAUDE.md")]
    pts += [os.path.join(s["root"], "CLAUDE.md") for s in cfg.get("scopes", [])]
    return pts


def reached_files(cfg):
    """{realpath: entry point that reached it} for every file the entry points
    @import, recursively. Import rules come from boot_budget (see module docstring)."""
    import boot_budget  # lazy: only checks 9/10 need it

    reached = {}
    for ep in entry_points(cfg):
        if not os.path.isfile(ep):
            continue
        for f in boot_budget.resolve_imports(ep):
            reached.setdefault(os.path.realpath(f), ep)
    return reached


# --- Frontmatter parsing (stdlib only, no yaml dep) --------------------------
def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_frontmatter(text):
    """Return a dict describing the YAML frontmatter, or None if absent/unterminated.

    Keys: name, description, top_type (a top-level `type:`), meta_type (a `type:`
    nested under `metadata:`), has_metadata.
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return None
    i += 1
    block = []
    while i < len(lines) and lines[i].strip() != "---":
        block.append(lines[i])
        i += 1
    if i >= len(lines):
        return None  # unterminated frontmatter

    result = {
        "name": None,
        "description": None,
        "top_type": None,
        "meta_type": None,
        "has_metadata": False,
    }
    in_metadata = False
    for line in block:
        if line.strip() == "":
            continue
        is_indented = line[0].isspace()
        m = re.match(r"^\s*([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if not is_indented:
            in_metadata = key == "metadata"
            if key == "metadata":
                result["has_metadata"] = True
            elif key == "name":
                result["name"] = _unquote(val)
            elif key == "description":
                result["description"] = _unquote(val)
            elif key == "type":
                result["top_type"] = _unquote(val)
        elif in_metadata and key == "type":
            result["meta_type"] = _unquote(val)
    return result


# --- Per-store checks ----------------------------------------------------------
def _read_index_targets(index_md):
    """Return list of (lineno, file.md) for every index line carrying an .md link."""
    targets = []
    if not index_md.exists():
        return targets
    for n, line in enumerate(index_md.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = INDEX_LINK_RE.search(line)
        if m:
            targets.append((n, m.group(1)))
    return targets


def _index_growth(memory_md, now_size):
    """Best-effort '+N chars/day over M days' context for the bloat finding.

    Reads the size of MEMORY.md as of ~GROWTH_LOOKBACK_DAYS ago from the enclosing git
    repo, if any. Returns a human string, or None if git/history is unavailable. NEVER
    raises and is NEVER load-bearing: the bloat finding fires on size alone (a canary
    must not go silent because a subprocess failed).
    """
    import subprocess

    try:
        memory_md = Path(memory_md)
        since = "%d days ago" % GROWTH_LOOKBACK_DAYS
        sha = subprocess.run(
            ["git", "-C", str(memory_md.parent), "rev-list", "-1",
             "--before=" + since, "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not sha:
            return None
        rel = subprocess.run(
            ["git", "-C", str(memory_md.parent), "ls-files", "--full-name", memory_md.name],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not rel:
            return None
        old = subprocess.run(
            ["git", "-C", str(memory_md.parent), "show", "%s:%s" % (sha, rel)],
            capture_output=True, text=True, timeout=10,
        )
        if old.returncode != 0 or not old.stdout:
            return None
        old_size = len(old.stdout)
        delta = now_size - old_size
        per_day = delta / float(GROWTH_LOOKBACK_DAYS)
        return (
            "{:,} chars {}d ago -> {:,} now ({:+,} chars, {:+,.0f}/day)".format(
                old_size, GROWTH_LOOKBACK_DAYS, now_size, delta, per_day)
        )
    except Exception:
        return None


def _empty_findings():
    return {k: [] for k, _ in _CATEGORY_TITLES}


def run_checks(store_path, threshold=DEFAULT_GLOBAL_INDEX_THRESHOLD):
    """Run all per-store checks against one store dir. Returns (findings, summary).

    findings: dict category -> list[str]
    summary:  dict of derived counts for the report header.
    threshold: MEMORY.md size (chars) above which check 7 fires.
    """
    store_path = Path(store_path)
    findings = _empty_findings()

    disk_files = sorted(
        p.name for p in store_path.glob("*.md") if p.name not in NON_MEMORY
    )
    disk_set = set(disk_files)

    memory_md = store_path / "MEMORY.md"
    reference_md = store_path / "REFERENCE.md"
    index_targets = _read_index_targets(memory_md)
    indexed_set = set(t for _, t in index_targets)

    # A store may also have a REFERENCE.md (the cold tier, never @imported: see
    # check 9). It is indexed the same way MEMORY.md is; combine the two for the "is
    # this file findable at all" checks (1/2), while keeping duplicate-within-one-file
    # (3) and both-indexed (8) separate, since those are exactly the two failure modes
    # worth telling apart.
    ref_index_targets = _read_index_targets(reference_md)
    ref_indexed_set = set(t for _, t in ref_index_targets)
    combined_indexed_set = indexed_set | ref_indexed_set
    both_indexes = (("MEMORY.md", index_targets), ("REFERENCE.md", ref_index_targets))

    # 1. Orphan files: on disk but not indexed in EITHER MEMORY.md or REFERENCE.md.
    for fname in disk_files:
        if fname not in combined_indexed_set:
            findings["orphan_files"].append(fname)

    # 2. Dangling index entries: indexed but file missing on disk (checked in each
    #    index file separately so the message names which one).
    for index_name, targets in both_indexes:
        for lineno, target in targets:
            # Only care about targets that look like store-local memory files
            # (bare filename, no path separator). Path-qualified links (e.g. to
            # another scope's store) are out of this store's scope.
            if "/" in target:
                continue
            if target not in disk_set:
                findings["dangling_entries"].append(
                    "%s (%s line %d)" % (target, index_name, lineno))

    # 3. Duplicate index entries: same file.md referenced by >1 line WITHIN one index
    #    file (a hand-edit or a botched merge, not the intended one-line-per-target).
    for index_name, targets in both_indexes:
        seen = {}
        for lineno, target in targets:
            seen.setdefault(target, []).append(lineno)
        for target, lns in sorted(seen.items()):
            if len(lns) > 1:
                findings["duplicate_entries"].append(
                    "%s (%s lines %s)" % (target, index_name, ", ".join(map(str, lns)))
                )

    # 8. Both-indexed: a target present in BOTH MEMORY.md and REFERENCE.md at once.
    #    A memory file should be indexed in exactly one tier; a hand edit or a botched
    #    tier move can leave it in both.
    for target in sorted(indexed_set & ref_indexed_set):
        findings["both_indexed"].append(target)

    # 4 & 5. Frontmatter: slugs + drift.
    slug_map = {}  # normalized slug -> [files]
    for fname in disk_files:
        try:
            text = (store_path / fname).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # One undecodable file must not abort the whole run.
            findings["frontmatter_drift"].append("%s: not valid UTF-8, cannot be parsed" % fname)
            continue
        fm = parse_frontmatter(text)
        if fm is None:
            findings["frontmatter_drift"].append(
                "%s: no parseable frontmatter block" % fname
            )
            continue
        problems = []
        if fm["name"] is None:
            problems.append("missing name:")
        if fm["description"] is None:
            problems.append("missing description:")
        if fm["top_type"] is not None and fm["meta_type"] is None:
            problems.append("top-level type: (should be nested under metadata:)")
        if problems:
            findings["frontmatter_drift"].append("%s: %s" % (fname, "; ".join(problems)))
        if fm["name"]:
            slug = fm["name"].strip().lower()
            slug_map.setdefault(slug, []).append(fname)

    for slug, files in sorted(slug_map.items()):
        if len(files) > 1:
            findings["duplicate_slugs"].append(
                "'%s' -> %s" % (slug, ", ".join(sorted(files)))
            )

    # 7. Index bloat: MEMORY.md too large to consolidate in one pass.
    #    Silent below threshold: this is the ONLY signal that /reflect consolidate is
    #    due, so it must not cry wolf (see docstring on why not 40k). Each store gets
    #    its own threshold; a scope store with a smaller budget must not silently
    #    inherit the global store's larger one.
    index_size = len(memory_md.read_text(encoding="utf-8", errors="replace")) if memory_md.exists() else 0
    if index_size > threshold:
        msg = (
            "MEMORY.md is {:,} chars, over the {:,} threshold ({} index lines) -- "
            "run `/reflect consolidate`".format(index_size, threshold, len(index_targets))
        )
        growth = _index_growth(memory_md, index_size)
        if growth:
            msg += ". Growth: " + growth
        findings["index_bloat"].append(msg)

    summary = {
        "disk_count": len(disk_files),
        "index_count": len(combined_indexed_set),
        "index_lines": len(index_targets) + len(ref_index_targets),
        "count_mismatch": len(disk_files) != len(combined_indexed_set),
        "index_size": index_size,
    }
    return findings, summary


def has_findings(findings):
    return any(v for v in findings.values())


# --- Check 9: REFERENCE.md must never be @imported ---------------------------------
def reference_imported_findings(cfg, reached=None):
    """One finding per REFERENCE.md reached from any entry point. [] when clean: this
    must never fire in a correctly-wired setup."""
    reached = reached_files(cfg) if reached is None else reached
    findings = []
    for rp, ep in sorted(reached.items()):
        if os.path.basename(rp) == "REFERENCE.md":
            findings.append(
                "%s is @imported (reached from %s) -- REFERENCE.md is the cold tier "
                "and must NEVER be @imported" % (rp, ep)
            )
    return findings


# --- Check 10: orphan STORE, a whole store nothing @imports -----------------------
def _non_empty_stores(cfg):
    """Yield (store_dir, md_files, content_files) for every
    `<claude_home>/projects/*/memory` dir on disk, plus every configured store, that is
    NOT the anti-population (a bare MEMORY.md stub, or a content-free scratch dir).

    A store counts as non-empty (is yielded) when ANY of:
      - it holds at least one real (non-STRUCTURAL) .md file (content_files non-empty), or
      - it has a RESUME.md whose content is non-empty after `.strip()` (whitespace-only
        counts as empty; an unreadable RESUME.md -- OSError/UnicodeDecodeError -- counts
        as non-empty, failing toward checking rather than a silent skip).
    content_files may be empty for a yielded store (e.g. a store whose only content is a
    real RESUME.md narrative with no indexed memory files)."""
    candidates = []
    projects_dir = Path(cfg["paths"]["claude_home"]) / "projects"
    if projects_dir.is_dir():
        candidates += sorted(projects_dir.glob("*/memory"))
    candidates += [Path(s) for _, s, _ in configured_stores(cfg)]
    seen = set()
    for store_dir in candidates:
        rp = os.path.realpath(str(store_dir))
        if rp in seen or not store_dir.is_dir():
            continue
        seen.add(rp)
        md_files = sorted(store_dir.glob("*.md"))
        content_files = [p for p in md_files if p.name not in STRUCTURAL]
        if content_files:
            yield store_dir, md_files, content_files
            continue
        resume_counts = False
        resume_path = store_dir / "RESUME.md"
        if resume_path.is_file():
            try:
                resume_counts = bool(resume_path.read_text(encoding="utf-8").strip())
            except (OSError, UnicodeDecodeError):
                resume_counts = True  # fail toward checking, never toward silent skip
        if resume_counts:
            yield store_dir, md_files, content_files


def _scope_roots_for_store(cfg, store_dir):
    """Every scope root ("~" for the global store) whose configured store IS `store_dir`
    by realpath; empty when `store_dir` is not any configured scope's store (e.g. an
    unconfigured stray store). Two scopes may share one store."""
    store_rp = os.path.realpath(str(store_dir))
    roots = []
    if os.path.realpath(str(cfg["paths"]["global_store"])) == store_rp:
        roots.append("~")
    for s in cfg.get("scopes", []):
        if os.path.realpath(str(s["store"])) == store_rp:
            roots.append(s["root"])
    return roots


def _decode_slug_dir(slug):
    """The existing directory whose projects/ folder name (notes_config.encode_project_dir)
    is `slug`, or None. The encoding is lossy ('/' and '.' both become '-'), so walk the
    filesystem instead of inverting it: descend only into a directory whose own encoding
    is a prefix of the slug."""
    def walk(dirpath, depth=0):
        if depth > 40:
            return None
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            return None
        for e in entries:
            enc = notes_config.encode_project_dir(e.path)
            if not e.is_dir():
                continue
            if enc == slug:
                return e.path
            if slug.startswith(enc + "-"):
                found = walk(e.path, depth + 1)
                if found:
                    return found
        return None
    return walk(os.sep)


def orphan_store_findings(cfg, reached=None):
    """One finding per non-empty store whose MEMORY.md no entry point reaches, so the
    whole store loads nowhere. When a store has a MEMORY.md, ONLY that file is checked:
    a store can be wired in via RESUME.md alone while MEMORY.md itself is orphaned, and
    that is still a real finding. The any-.md fallback applies only when the store has
    no MEMORY.md at all.

    A store whose scope has Claude Code's native auto memory on (boot_budget's
    is_auto_memory_enabled()) is not an orphan even with no @import, PROVIDED its
    MEMORY.md sits at that scope's native auto memory path (<claude_home>/projects/
    <slug>/memory/MEMORY.md): that file loads on its own, natively, every time a
    session starts in that scope. A custom store path native auto memory does not
    know about gets no such pass. The same holds for an UNCONFIGURED store at
    <claude_home>/projects/<slug>/memory whose slug names a real project folder: with
    auto memory on (the default), Claude Code itself wrote it and loads it there, so it
    is not an orphan even though no config entry names the project. [] when clean."""
    reached = reached_files(cfg) if reached is None else reached
    known = {os.path.realpath(str(s)) for _, s, _ in configured_stores(cfg)}
    findings = []
    for store_dir, md_files, content_files in _non_empty_stores(cfg):
        memory_md = store_dir / "MEMORY.md"
        check_files = [memory_md] if memory_md.is_file() else md_files
        if any(os.path.realpath(str(p)) in reached for p in check_files):
            continue
        if memory_md.is_file():
            import boot_budget
            memory_rp = os.path.realpath(str(memory_md))
            roots = _scope_roots_for_store(cfg, store_dir)
            if any(memory_rp == os.path.realpath(boot_budget.native_memory_path(root))
                   and boot_budget.is_auto_memory_enabled(root)
                   for root in roots):
                continue  # natively loaded; not an orphan
            projects_rp = os.path.realpath(str(Path(cfg["paths"]["claude_home"]) / "projects"))
            if (not roots
                    and os.path.dirname(os.path.dirname(os.path.realpath(str(store_dir)))) == projects_rp
                    and os.path.basename(str(store_dir)) == "memory"):
                proj_root = _decode_slug_dir(os.path.basename(os.path.dirname(str(store_dir))))
                # Judged against the project's OWN settings: one that turned auto memory
                # off for itself keeps its store flagged.
                if proj_root and boot_budget.is_auto_memory_enabled(proj_root):
                    continue  # an unconfigured project's own native auto memory
        if content_files:
            holds = "%d memory files" % len(content_files)
        else:
            # No real content files -- the store was yielded only because it has a
            # real RESUME.md narrative; name what it actually holds rather than
            # misleadingly saying "0 memory files".
            holds = ("%s only" % ", ".join(p.name for p in md_files)) if md_files else "no .md files"
        msg = (
            "STORE %s: %s, no CLAUDE.md entry point @imports it "
            "(nothing it holds ever loads)" % (store_dir, holds)
        )
        if os.path.realpath(str(store_dir)) not in known:
            msg += " (not in the config: add a [[scopes]] entry and an @import in its CLAUDE.md)"
        findings.append(msg)
    return findings


# --- Report ---------------------------------------------------------------------
_CATEGORY_TITLES = [
    ("orphan_files", "Orphan files (on disk, not in MEMORY.md or REFERENCE.md index)"),
    ("dangling_entries", "Dangling index entries (indexed, file missing on disk)"),
    ("duplicate_entries", "Duplicate index entries (same file indexed twice in one index)"),
    ("both_indexed", "Indexed in BOTH MEMORY.md and REFERENCE.md"),
    ("duplicate_slugs", "Duplicate slugs (multiple files share a name:)"),
    ("frontmatter_drift", "Frontmatter format drift"),
    ("index_bloat", "Index bloat (MEMORY.md over threshold, consolidate due)"),
]


def build_report(results):
    """results: list of (label, store_path, findings, summary)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = ["# Memory-hygiene canary, %s (UTC)" % stamp, ""]
    for label, store_path, findings, summary in results:
        out.append("## Store: %s  (%s)" % (label, store_path))
        header = (
            "{} memory files on disk, {} distinct index entries ({} index lines).".format(
                summary["disk_count"], summary["index_count"], summary["index_lines"])
        )
        if summary["count_mismatch"]:
            header += "  WARNING: disk/index count MISMATCH."
        out.append(header)
        out.append("")
        total = sum(len(findings[k]) for k, _ in _CATEGORY_TITLES)
        if total == 0:
            out.append("No findings for this store.")
            out.append("")
            continue
        for key, title in _CATEGORY_TITLES:
            items = findings[key]
            if not items:
                continue
            out.append("### %s: %d" % (title, len(items)))
            for it in items:
                out.append("- " + it)
            out.append("")
    return "\n".join(out).rstrip() + "\n"


_EMPTY_SUMMARY = {"disk_count": 0, "index_count": 0, "index_lines": 0,
                  "count_mismatch": False, "index_size": 0}


def report_mode(cfg, out=None):
    """Run every check. Silent + 0 when clean; report on `out` + EXIT_FINDINGS otherwise."""
    out = sys.stdout if out is None else out
    results = []
    any_found = False
    for label, store_path, threshold in configured_stores(cfg):
        if not store_path.exists():
            # A configured store that does not exist is itself worth surfacing.
            # Derived from _CATEGORY_TITLES so a new category can never leave this
            # branch with a missing key (build_report indexes every category).
            findings = _empty_findings()
            findings["frontmatter_drift"] = ["STORE MISSING: %s" % store_path]
            results.append((label, store_path, findings, dict(_EMPTY_SUMMARY)))
            any_found = True
            continue
        findings, summary = run_checks(store_path, threshold)
        if has_findings(findings):
            any_found = True
        results.append((label, store_path, findings, summary))

    reached = reached_files(cfg)
    ref_imported = reference_imported_findings(cfg, reached)
    orphan_stores = orphan_store_findings(cfg, reached)
    if ref_imported or orphan_stores:
        any_found = True

    if not any_found:
        return 0  # silent when clean

    # Clean stores stay in the report for context.
    report = build_report(results)
    if ref_imported:
        report += "\n## REFERENCE.md imported into a boot set\n\n"
        report += "\n".join("- " + f for f in ref_imported) + "\n"
    if orphan_stores:
        report += "\n## Orphan STOREs (every store on disk)\n\n"
        report += "\n".join("- " + f for f in orphan_stores) + "\n"
    out.write(report)
    return EXIT_FINDINGS


# --- Selftest ----------------------------------------------------------------
def _write(p, text):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(text, encoding="utf-8")


def _mem_file(slug, drift=False, no_name=False, no_desc=False, no_fm=False):
    if no_fm:
        return "Just a body, no frontmatter.\n"
    lines = ["---"]
    if not no_name:
        lines.append("name: " + slug)
    if not no_desc:
        lines.append("description: a test memory")
    if drift:
        lines.append("type: reference")  # top-level type = drift
    else:
        lines.append("metadata:")
        lines.append("  type: reference")
    lines.append("---")
    lines.append("body")
    return "\n".join(lines) + "\n"


def _test_cfg(root, scopes=()):
    """A resolved config dict rooted entirely under the temp dir `root`."""
    claude_home = os.path.join(root, "claude")
    return {
        "paths": {
            "claude_home": claude_home,
            "state_dir": os.path.join(root, "state"),
            "global_store": os.path.join(claude_home, "projects", "-home", "memory"),
        },
        "budgets": {"global_index_bytes": 500, "scope_index_bytes": 300},
        "scopes": [dict(s) for s in scopes],
    }


class _Capture(object):
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    def text(self):
        return "".join(self.parts)


def selftest():
    ok = [True]

    def check(cond, msg):
        if not cond:
            ok[0] = False
        print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))

    # --- Dirty store: one seeded instance of each drift class ---
    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        _write(store / "reference_alpha.md", _mem_file("alpha"))
        _write(store / "feedback_beta.md", _mem_file("beta"))
        # Orphans: on disk, not in index. (Two, so disk count != distinct index
        # count even though one indexed file is also dangling.)
        _write(store / "reference_orphan.md", _mem_file("orphan"))
        _write(store / "reference_orphan2.md", _mem_file("orphan2"))
        # Duplicate slug: two files, same name:.
        _write(store / "reference_dupa.md", _mem_file("dupslug"))
        _write(store / "reference_dupb.md", _mem_file("dupslug"))
        _write(store / "reference_drift.md", _mem_file("drift", drift=True))
        _write(store / "reference_noname.md", _mem_file("x", no_name=True))
        _write(store / "reference_nofm.md", _mem_file("x", no_fm=True))
        _write(store / "RESUME.md", "narrative\n")  # structural, ignored
        _write(store / "MEMORY.md", (
            "# Memory Index\n\n"
            "- [Alpha](reference_alpha.md) -- hook\n"
            "- [Beta](feedback_beta.md) -- hook\n"
            "- [Alpha dup](reference_alpha.md) -- duplicate index entry\n"
            "- [DupA](reference_dupa.md) -- hook\n"
            "- [DupB](reference_dupb.md) -- hook\n"
            "- [Drift](reference_drift.md) -- hook\n"
            "- [NoName](reference_noname.md) -- hook\n"
            "- [NoFM](reference_nofm.md) -- hook\n"
            "- [Gone](reference_missing.md) -- dangling\n"
            "- Prose line with no link (see foo.md), should be ignored\n"
        ))

        findings, summary = run_checks(store)
        check("reference_orphan.md" in findings["orphan_files"], "orphan file detected")
        check(any("reference_missing.md" in x for x in findings["dangling_entries"]),
              "dangling index entry detected")
        check(not any("foo.md" in x for x in findings["dangling_entries"]),
              "prose parenthetical (see foo.md) is not read as an index link")
        check(any("reference_alpha.md" in x for x in findings["duplicate_entries"]),
              "duplicate index entry detected")
        check(any("dupslug" in x for x in findings["duplicate_slugs"]),
              "duplicate slug detected")
        check(any(x.startswith("reference_drift.md") for x in findings["frontmatter_drift"]),
              "top-level type: drift detected")
        check(any(x.startswith("reference_noname.md") for x in findings["frontmatter_drift"]),
              "missing name: detected")
        check(any(x.startswith("reference_nofm.md") for x in findings["frontmatter_drift"]),
              "no-frontmatter detected")
        check(summary["count_mismatch"], "count mismatch reported")
        check(has_findings(findings), "has_findings true on dirty store")

    # --- Clean store: zero findings ---
    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        _write(store / "reference_a.md", _mem_file("aa"))
        _write(store / "feedback_b.md", _mem_file("bb"))
        _write(store / "user_c.md", _mem_file("cc"))
        _write(store / "RESUME.md", "narrative\n")
        # /reflect Step 5b's hand-trim archive: no frontmatter, never indexed.
        _write(store / "RESUME-archive.md", "# RESUME archive\n\nold narrative\n")
        _write(store / "MEMORY.md", (
            "# Memory Index\n\n"
            "- [A](reference_a.md) -- hook\n"
            "- [B](feedback_b.md) -- hook\n"
            "- [C](user_c.md) -- hook\n"
        ))
        findings, summary = run_checks(store)
        check(not has_findings(findings), "clean store yields zero findings")
        check("RESUME-archive.md" not in findings["orphan_files"]
              and not any(x.startswith("RESUME-archive.md") for x in findings["frontmatter_drift"]),
              "RESUME-archive.md is structural: not an orphan, not frontmatter drift")
        check(not summary["count_mismatch"], "clean store: no count mismatch")

    # --- Check 7: index bloat fires ONLY above the threshold ---
    # Both directions matter. A canary that never fires is useless; one that always
    # fires trains you to ignore it, and this is the sole consolidate trigger.
    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        thr = 2000
        _write(store / "reference_a.md", _mem_file("aa"))
        under = "# Memory Index\n\n- [A](reference_a.md) -- hook\n"
        under += "x" * (thr - len(under) - 10)
        _write(store / "MEMORY.md", under)
        findings, summary = run_checks(store, thr)
        check(not findings["index_bloat"], "index just under threshold: silent")
        check(summary["index_size"] < thr, "under-threshold size reported")

        _write(store / "MEMORY.md", under + "y" * 100)
        findings, summary = run_checks(store, thr)
        check(len(findings["index_bloat"]) == 1, "index just over threshold: bloat fires")
        check(bool(findings["index_bloat"]) and "consolidate" in findings["index_bloat"][0],
              "bloat finding names the action (/reflect consolidate)")
        check(has_findings(findings), "bloat alone makes has_findings true")
        rep = build_report([("t", store, findings, summary)])
        check("Index bloat" in rep, "bloat finding renders in the report")

    # --- Check 8: both-indexed + REFERENCE.md-aware orphan/dangling ---
    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        _write(store / "reference_cold.md", _mem_file("cold"))
        _write(store / "reference_both.md", _mem_file("both"))
        _write(store / "reference_hotonly.md", _mem_file("hotonly"))
        _write(store / "MEMORY.md", (
            "# Memory Index\n\n"
            "- [Both](reference_both.md) -- hook\n"
            "- [HotOnly](reference_hotonly.md) -- hook\n"
        ))
        _write(store / "REFERENCE.md", (
            "# Cold Reference Index\n\n"
            "- [Cold](reference_cold.md) -- hook\n"
            "- [Both](reference_both.md) -- hook\n"
            "- [Gone](reference_gone.md) -- dangling in REFERENCE.md\n"
        ))
        findings, summary = run_checks(store)
        check("reference_both.md" in findings["both_indexed"],
              "a target indexed in BOTH indexes is flagged (positive control)")
        check("reference_hotonly.md" not in findings["both_indexed"]
              and "reference_cold.md" not in findings["both_indexed"],
              "a target indexed in only ONE index is not flagged as both-indexed")
        check("reference_cold.md" not in findings["orphan_files"],
              "a file indexed only in REFERENCE.md is NOT an orphan")
        check(not findings["orphan_files"], "MEMORY.md-only-indexed files are not orphans")
        check(any("reference_gone.md" in x and "REFERENCE.md" in x
                  for x in findings["dangling_entries"]),
              "a dangling entry INSIDE REFERENCE.md is detected and names REFERENCE.md")
        # Union of {both, hotonly} and {cold, both, gone} = 4 distinct targets (gone
        # is dangling but still counted as "indexed").
        check(summary["index_count"] == 4, "count summary is the UNION of both indexes")
        rep = build_report([("t", store, findings, summary)])
        check("Indexed in BOTH" in rep, "both-indexed finding renders in the report")

    # --- Config-driven stores + per-store thresholds ---
    with tempfile.TemporaryDirectory() as d:
        scope_root = os.path.join(d, "alpha")
        cfg = _test_cfg(d, [{"name": "alpha", "root": scope_root,
                             "store": os.path.join(d, "alpha-store")},
                            {"name": "beta", "root": os.path.join(d, "beta"),
                             "store": os.path.join(d, "claude", "projects", "-home", "memory")}])
        stores = configured_stores(cfg)
        check([s[0] for s in stores] == ["global", "alpha"],
              "configured_stores(): global + scopes, deduped by realpath")
        check(stores[0][2] == 500 and stores[1][2] == 300,
              "configured_stores(): global and scope stores get their OWN thresholds")
        bare = {"paths": cfg["paths"], "scopes": []}
        check(configured_stores(bare)[0][2] == DEFAULT_GLOBAL_INDEX_THRESHOLD,
              "configured_stores(): a config with no budgets table uses the fallback")

    # --- report_mode(): checks 9/10 and the silent-when-clean contract ---
    with tempfile.TemporaryDirectory() as d:
        scope_root = os.path.join(d, "alpha")
        scope_store = os.path.join(d, "alpha-store")
        cfg = _test_cfg(d, [{"name": "alpha", "root": scope_root, "store": scope_store}])
        g_store = Path(cfg["paths"]["global_store"])
        g_mem = g_store / "MEMORY.md"
        g_ref = g_store / "REFERENCE.md"
        _write(g_store / "reference_x.md", _mem_file("x"))
        _write(g_mem, "# Memory Index\n\n- [X](reference_x.md) -- hook\n")
        _write(g_ref, "# Cold Reference Index\n\n")
        s_mem = Path(scope_store) / "MEMORY.md"
        _write(Path(scope_store) / "reference_y.md", _mem_file("y"))
        _write(s_mem, "# Memory Index\n\n- [Y](reference_y.md) -- hook\n")
        global_claude = os.path.join(cfg["paths"]["claude_home"], "CLAUDE.md")
        scope_claude = os.path.join(scope_root, "CLAUDE.md")
        _write(global_claude, "# global\n@%s\n" % g_mem)
        _write(scope_claude, "# alpha\n@%s\n" % s_mem)

        # Anti-population: an empty store dir, a bare MEMORY.md stub, and a store with
        # only a whitespace-only RESUME.md alongside it (whitespace never counts).
        projects = Path(cfg["paths"]["claude_home"]) / "projects"
        (projects / "-empty" / "memory").mkdir(parents=True)
        _write(projects / "-structural" / "memory" / "MEMORY.md", "# Memory Index\n\n")
        _write(projects / "-structural" / "memory" / "RESUME.md", "   \n\t\n")

        cap = _Capture()
        rc = report_mode(cfg, cap)
        check(rc == 0 and cap.text() == "", "report_mode(): clean setup is silent, exit 0")

        # An @-line inside a fenced code block is example syntax, not an import.
        _write(global_claude, "# global\n@%s\n```\n@%s\n```\n" % (g_mem, g_ref))
        check(reference_imported_findings(cfg) == [],
              "check 9: a REFERENCE.md @-line inside a code fence is not an import")

        # Check 9 positive control.
        _write(global_claude, "# global\n@%s\n@%s\n" % (g_mem, g_ref))
        bad = reference_imported_findings(cfg)
        check(len(bad) == 1 and "must NEVER be" in bad[0],
              "check 9: an @imported REFERENCE.md IS flagged (positive control)")
        cap = _Capture()
        check(report_mode(cfg, cap) == EXIT_FINDINGS
              and "REFERENCE.md imported" in cap.text(),
              "report_mode(): exits %d with the REFERENCE.md section" % EXIT_FINDINGS)
        _write(global_claude, "# global\n@%s\n" % g_mem)

        # Check 10: anti-population stays silent (bare MEMORY.md stub, whitespace-only RESUME.md).
        clean = orphan_store_findings(cfg)
        check(clean == [], "check 10: wired stores, empty and structural-only stores are silent")

        # Check 10: a store with MEMORY.md + a real RESUME.md narrative (no indexed
        # memory files) IS flagged -- inverted from the old reading above, which treated
        # any MEMORY.md + RESUME.md store as structural-only regardless of content.
        resume_narrative = projects / "-resume-narrative" / "memory"
        _write(resume_narrative / "MEMORY.md", "# Memory Index\n\n")
        _write(resume_narrative / "RESUME.md", "- [ ] ship the thing\n")
        hit = [f for f in orphan_store_findings(cfg) if str(resume_narrative) in f]
        check(len(hit) == 1, "check 10: a store with only a real RESUME.md narrative is flagged")
        check(bool(hit) and "0 memory files" not in hit[0],
              "check 10: the RESUME-narrative store's message does not misreport '0 memory files'")
        check(bool(hit) and "RESUME.md" in hit[0] and "MEMORY.md" in hit[0],
              "check 10: the RESUME-narrative store's message names what it actually holds")

        # Check 10: an unreadable (non-UTF-8) RESUME.md beside a bare MEMORY.md fails
        # toward checking -- flagged, never a silent skip.
        undecodable = projects / "-undecodable-resume" / "memory"
        _write(undecodable / "MEMORY.md", "# Memory Index\n\n")
        (undecodable / "RESUME.md").write_bytes(b"\xff\xfe")
        check(any(str(undecodable) in f for f in orphan_store_findings(cfg)),
              "check 10: a store whose RESUME.md is unreadable IS flagged (fails toward checking)")

        # Check 10: an unconfigured content store nothing imports.
        stray = projects / "-stray" / "memory"
        _write(stray / "MEMORY.md", "# Memory Index\n\n- [Z](reference_z.md) -- hook\n")
        _write(stray / "reference_z.md", _mem_file("z"))
        hit = [f for f in orphan_store_findings(cfg) if str(stray) in f]
        check(len(hit) == 1 and "not in the config" in hit[0],
              "check 10: an unreached, unconfigured store is flagged with the config hint")

        # A non-UTF-8 memory file and a non-UTF-8 index are reported/tolerated, not fatal.
        bad_store = Path(d) / "bad-utf8-store"
        _write(bad_store / "MEMORY.md", "# Memory Index\n\n- [B](feedback_b.md) -- hook\n")
        (bad_store / "feedback_b.md").write_bytes(b"\xff\xfe not utf8")
        try:
            bad_findings, _ = run_checks(bad_store)
            check(any("feedback_b.md" in f and "UTF-8" in f
                      for f in bad_findings["frontmatter_drift"]),
                  "a non-UTF-8 memory file is a finding, not a crash")
        except UnicodeDecodeError:
            check(False, "a non-UTF-8 memory file is a finding, not a crash")
        (bad_store / "MEMORY.md").write_bytes(b"# Memory Index\n\xff\n")
        try:
            run_checks(bad_store)
            check(True, "a non-UTF-8 MEMORY.md does not abort the run")
        except UnicodeDecodeError:
            check(False, "a non-UTF-8 MEMORY.md does not abort the run")

        # A bad config exits with a one-line message, not a traceback.
        import subprocess
        bad_cfg = Path(d) / "bad.toml"
        bad_cfg.write_text("this is not toml\n")
        run = subprocess.run([sys.executable, os.path.abspath(__file__)],
                             env=dict(os.environ, SESSION_NOTES_CONFIG=str(bad_cfg)),
                             capture_output=True, text=True, timeout=60)
        check(run.returncode != 0 and "Traceback" not in run.stderr and run.stderr.strip(),
              "a bad config is a clean nonzero exit with a message, not a traceback")

        # Check 10 (default config): an UNCONFIGURED project's own native auto memory
        # store (slug names a real folder, auto memory on by default) is not an orphan.
        import boot_budget as _bb
        _orig_ovr = _bb.CONFIG_DIR_OVERRIDE
        _orig_dis = os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        _bb.CONFIG_DIR_OVERRIDE = cfg["paths"]["claude_home"]
        try:
            native_proj = os.path.join(d, "native-proj")
            os.makedirs(native_proj, exist_ok=True)
            native_store = projects / notes_config.encode_project_dir(native_proj) / "memory"
            _write(native_store / "MEMORY.md", "# Memory Index\n\n- [N](feedback_n.md) -- hook\n")
            _write(native_store / "feedback_n.md", _mem_file("n"))
            hit = [f for f in orphan_store_findings(cfg) if str(native_store) in f]
            check(hit == [], "check 10: default config, an unconfigured project's native "
                  "auto memory store is NOT flagged as an orphan")
            # The project's own settings turn auto memory off: its store is flagged again.
            _write(os.path.join(native_proj, ".claude", "settings.json"),
                   '{"autoMemoryEnabled": false}')
            hit_off = [f for f in orphan_store_findings(cfg) if str(native_store) in f]
            check(len(hit_off) == 1, "check 10: a project that turned auto memory off in its "
                  "own settings keeps its un-imported store flagged")
        finally:
            _bb.CONFIG_DIR_OVERRIDE = _orig_ovr
            if _orig_dis is not None:
                os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = _orig_dis

        # Check 10: RESUME.md reached but MEMORY.md not, still flagged.
        _write(Path(scope_store) / "RESUME.md", "narrative\n")
        _write(scope_claude, "# alpha\n@%s\n" % (Path(scope_store) / "RESUME.md"))
        hit = [f for f in orphan_store_findings(cfg) if scope_store in f]
        check(len(hit) == 1 and "not in the config" not in hit[0],
              "check 10: RESUME.md reached but MEMORY.md not, configured store still flagged")

        # Check 10 (native auto memory trial): a scope whose store sits at ITS OWN
        # native auto memory path and is never @imported anywhere. With auto memory off
        # for it, that's an ordinary orphan; with it on, native loading reaches the
        # store on its own, so it must not be flagged.
        import boot_budget
        orig_override = boot_budget.CONFIG_DIR_OVERRIDE
        orig_disable = os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        boot_budget.CONFIG_DIR_OVERRIDE = cfg["paths"]["claude_home"]
        settings_path = Path(cfg["paths"]["claude_home"]) / "settings.json"
        try:
            auto_root = os.path.join(d, "auto-scope")
            os.makedirs(auto_root, exist_ok=True)
            _write(os.path.join(auto_root, "CLAUDE.md"), "# auto-scope: no memory import\n")
            auto_native = Path(boot_budget.native_memory_path(auto_root))
            _write(auto_native, "# Memory Index\n\n- [Z](reference_autoz.md) -- hook\n")
            _write(auto_native.parent / "reference_autoz.md", _mem_file("autoz"))
            cfg["scopes"].append({"name": "auto", "root": auto_root, "store": str(auto_native.parent)})

            _write(settings_path, '{"autoMemoryEnabled": false}')
            hit_off = [f for f in orphan_store_findings(cfg) if str(auto_native.parent) in f]
            check(len(hit_off) == 1,
                  "check 10: auto memory off, an un-imported native-path store IS flagged (ordinary orphan)")

            _write(settings_path, '{"autoMemoryEnabled": true}')
            hit_on = [f for f in orphan_store_findings(cfg) if str(auto_native.parent) in f]
            check(hit_on == [],
                  "check 10: with auto memory on for the scope, an un-imported store loads "
                  "natively and is NOT flagged as an orphan")

            # Two scopes share the store; only the second one's native path is it.
            cfg["scopes"].insert(0, {"name": "decoy", "root": os.path.join(d, "decoy-scope"),
                                     "store": str(auto_native.parent)})
            hit_shared = [f for f in orphan_store_findings(cfg) if str(auto_native.parent) in f]
            cfg["scopes"].pop(0)
            check(hit_shared == [],
                  "check 10: a shared store loads natively for its second scope, NOT flagged")
        finally:
            boot_budget.CONFIG_DIR_OVERRIDE = orig_override
            if orig_disable is not None:
                os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = orig_disable
            cfg["scopes"].pop()
            if settings_path.exists():
                settings_path.unlink()

        cap = _Capture()
        check(report_mode(cfg, cap) == EXIT_FINDINGS and "Orphan STOREs" in cap.text(),
              "report_mode(): orphan-store findings alone trip exit %d" % EXIT_FINDINGS)

        # A configured store that does not exist is surfaced.
        cfg2 = _test_cfg(d, [{"name": "beta", "root": os.path.join(d, "beta"),
                              "store": os.path.join(d, "beta-store")}])
        cap = _Capture()
        check(report_mode(cfg2, cap) == EXIT_FINDINGS and "STORE MISSING" in cap.text(),
              "report_mode(): a configured but missing store is reported")

    print("\nSELFTEST:", "PASS" if ok[0] else "FAIL")
    return 0 if ok[0] else 1


def main():
    ap = argparse.ArgumentParser(
        description="Deterministic hygiene checks over the configured memory stores. "
                    "Silent + exit 0 when clean; report on stdout + exit %d on findings."
                    % EXIT_FINDINGS)
    ap.add_argument("--selftest", action="store_true", help="run the built-in selftest")
    ap.add_argument("--report", action="store_true",
                    help="run against the configured stores (default mode)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    try:
        cfg = notes_config.load()
    except notes_config.ConfigError as exc:
        sys.exit("memory_hygiene_canary: bad config: %s" % exc)
    return report_mode(cfg)


if __name__ == "__main__":
    sys.exit(main())
