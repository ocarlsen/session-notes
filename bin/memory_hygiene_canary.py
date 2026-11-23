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
                           is non-empty per non_empty_stores() (real memory files, or a
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
import shutil
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

# Codex only: warn when the files the SessionStart hook loads reach this share of
# agent.codex_boot_bytes, before the hook starts dropping files past the cap.
CODEX_BOOT_WARN_FRACTION = 0.9

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
def manifest_paths(cfg):
    """Realpaths of every scope's declared `manifest`. A manifest is structural: it
    describes the scope, it is not a memory entry, so it is never an orphan."""
    return frozenset(os.path.realpath(s["manifest"])
                     for s in cfg.get("scopes", []) if s.get("manifest"))


def agent_kind(cfg):
    return cfg.get("agent", {}).get("kind", "claude")


def configured_stores(cfg):
    """[(label, store_path, threshold)] for the global store and every scope store,
    deduped by realpath (a scope may point its store at the global one)."""
    budgets = cfg.get("budgets", {})
    g_thr = budgets.get("global_index_bytes", DEFAULT_GLOBAL_INDEX_THRESHOLD)
    s_thr = budgets.get("scope_index_bytes", DEFAULT_SCOPE_INDEX_THRESHOLD)
    out, seen = [], set()
    entries = [("global", cfg["paths"]["global_store"], g_thr,
                cfg["paths"].get("global_store_derived", False))]
    entries += [(s["name"], s["store"], s_thr, s.get("store_derived", False))
                for s in cfg.get("scopes", [])]
    for label, store, thr, derived in entries:
        rp = os.path.realpath(store)
        if rp in seen:
            continue
        seen.add(rp)
        # A derived store appears on the agent's first save to it; until then there
        # is nothing to check, and its absence is not a finding.
        if derived and not os.path.exists(store):
            continue
        out.append((label, Path(store), thr))
    return out


def _entry_points_scoped(cfg):
    """[(entry point, scope it speaks for)]: the scope root, or "global"."""
    home = os.path.expanduser("~")
    pts = [(os.path.join(cfg["paths"]["claude_home"], "CLAUDE.md"), "global"),
           (os.path.join(home, "CLAUDE.md"), "global")]
    for s in cfg.get("scopes", []):
        # Claude Code boots from either <root>/CLAUDE.md or <root>/.claude/CLAUDE.md.
        pts.append((os.path.join(s["root"], "CLAUDE.md"), s["root"]))
        pts.append((os.path.join(s["root"], ".claude", "CLAUDE.md"), s["root"]))
    return pts


def entry_points(cfg):
    """CLAUDE.md files Claude Code may boot from, as far as the config knows."""
    return [p for p, _ in _entry_points_scoped(cfg)]


def reached_files(cfg):
    """{realpath: entry point that reached it} for every file the entry points
    @import, recursively. Import rules come from boot_budget (see module docstring).
    An entry point (or imported file) that exists but cannot be read is recorded in
    boot_budget's blind reads, never skipped silently."""
    import boot_budget  # lazy: only checks 9/10 need it

    reached = {}
    for ep, scope in _entry_points_scoped(cfg):
        # resolve_imports is silent on an absent file and records an unreadable one.
        for f in boot_budget.resolve_imports(ep, scope=scope):
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


def run_checks(store_path, threshold=DEFAULT_GLOBAL_INDEX_THRESHOLD, skip=frozenset()):
    """Run all per-store checks against one store dir. Returns (findings, summary).

    findings: dict category -> list[str]
    summary:  dict of derived counts for the report header.
    threshold: MEMORY.md size (chars) above which check 7 fires.
    skip: realpaths of structural files beyond NON_MEMORY (the scopes' manifests).
    """
    store_path = Path(store_path)
    findings = _empty_findings()

    disk_files = sorted(
        p.name for p in store_path.glob("*.md")
        if p.name not in NON_MEMORY and os.path.realpath(str(p)) not in skip
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
def non_empty_stores(cfg):
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
        skip = manifest_paths(cfg)
        content_files = [p for p in md_files if p.name not in STRUCTURAL
                         and os.path.realpath(str(p)) not in skip]
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


def _blind_for(boot_budget, roots, uses_settings):
    """The blind reads that bear on an orphan verdict for a store owned by `roots`: those
    for the scope itself and for a global entry point, plus user settings only when the
    verdict actually reads settings."""
    wanted = {boot_budget.blind_scope(r) for r in roots} | {"global"}
    if uses_settings:
        wanted.add("user settings")
    return [(p, sc, r) for p, sc, r in boot_budget.blind_reads() if sc in wanted]


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
    if reached is None:
        import boot_budget
        boot_budget.blind_reset()  # a standalone call starts from a clean record
        reached = reached_files(cfg)
    known = {os.path.realpath(str(s)) for _, s, _ in configured_stores(cfg)}
    findings = []
    for store_dir, md_files, content_files in non_empty_stores(cfg):
        memory_md = store_dir / "MEMORY.md"
        check_files = [memory_md] if memory_md.is_file() else md_files
        if any(os.path.realpath(str(p)) in reached for p in check_files):
            continue
        import boot_budget
        roots = _scope_roots_for_store(cfg, store_dir)
        scope_roots = list(roots)  # whose entry points and settings bear on the verdict
        settings_roots = []        # whose settings the native pass below reads
        if memory_md.is_file():
            memory_rp = os.path.realpath(str(memory_md))
            settings_roots = [r for r in roots
                              if memory_rp == os.path.realpath(boot_budget.native_memory_path(r))]
            if not roots:
                projects_rp = os.path.realpath(str(Path(cfg["paths"]["claude_home"]) / "projects"))
                if (os.path.dirname(os.path.dirname(os.path.realpath(str(store_dir)))) == projects_rp
                        and os.path.basename(str(store_dir)) == "memory"):
                    proj_root = _decode_slug_dir(os.path.basename(os.path.dirname(str(store_dir))))
                    if proj_root:
                        scope_roots = settings_roots = [proj_root]
        # Populate the blind-read record for every settings file the verdict below leans
        # on, then refuse to judge from a file we could not read.
        for root in settings_roots:
            boot_budget.is_auto_memory_enabled(root)
        blind = _blind_for(boot_budget, scope_roots, bool(settings_roots))
        if blind:
            findings.append(
                "STORE %s: orphan judgment NOT made, the check is blind for scope %s: %s"
                % (store_dir, ", ".join(sorted({sc for _, sc, _ in blind})),
                   "; ".join("cannot read %s (%s)" % (p, r) for p, _, r in blind)))
            continue
        if memory_md.is_file():
            memory_rp = os.path.realpath(str(memory_md))
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


def build_report(results, kind="claude"):
    """results: list of (label, store_path, findings, summary)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = ["# Memory-hygiene canary (%s), %s (UTC)" % (kind, stamp), ""]
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


def codex_boot_findings(cfg):
    """Codex only. The SessionStart hook loads the global MEMORY.md + RESUME.md, then the
    project's MEMORY.md + RESUME.md, and drops any file past agent.codex_boot_bytes. One
    finding per label (the global store alone, then each scope with the global files) whose
    files total >= CODEX_BOOT_WARN_FRACTION of that cap."""
    import boot_budget
    cap = cfg.get("agent", {}).get(
        "codex_boot_bytes", notes_config.DEFAULTS["agent"]["codex_boot_bytes"])

    def size(store, scope):
        total = 0
        for name in ("MEMORY.md", "RESUME.md"):
            data = boot_budget.read_bytes_or_blind(os.path.join(str(store), name), scope)
            total += len(data) if data is not None else 0
        return total

    g_size = size(cfg["paths"]["global_store"], "global")
    rows = [("global", g_size)]
    for s in cfg.get("scopes", []):
        rows.append((s["name"], g_size + size(s["store"], s["root"])))
    return ["CODEX BOOT %s: %d of %d bytes (>= %d%%); the SessionStart hook drops files "
            "past the cap" % (label, n, cap, round(CODEX_BOOT_WARN_FRACTION * 100))
            for label, n in rows if n >= CODEX_BOOT_WARN_FRACTION * cap]


try:  # Python 3.11+
    import tomllib as _tomllib
except ImportError:
    try:
        import tomli as _tomllib
    except ImportError:
        _tomllib = None

_CODEX_KEYS = ("project_doc_max_bytes", "project_doc_fallback_filenames")
# Where Codex lives when the config carries no agent.codex_home. The selftest points this at
# a path that does not exist so no host ~/.codex leaks into it.
_DEFAULT_CODEX_HOME = notes_config.DEFAULTS["agent"]["codex_home"]


def _skip_string(text, i):
    """text[i] opens a TOML string; the index just past it. ValueError if unterminated."""
    n = len(text)
    for q in ('"""', "'''"):
        if text.startswith(q, i):
            j = i + 3
            while True:
                j = text.find(q, j)
                if j < 0:
                    raise ValueError("unterminated multi-line string")
                if q == "'''" or (len(text[:j]) - len(text[:j].rstrip("\\"))) % 2 == 0:
                    j += 3
                    while j < n and text[j] == q[0]:  # up to two extra quotes belong to the string
                        j += 1
                    return j
                j += 1
    q = text[i]
    j = i + 1
    while j < n and text[j] != "\n":
        if q == '"' and text[j] == "\\":
            j += 2
            continue
        if text[j] == q:
            return j + 1
        j += 1
    raise ValueError("unterminated string")


def _scan_top_level(text):
    """[(key, value_text, line)] of the top-level key/value pairs, up to the first table header
    outside any string or array. Tracks multi-line strings and bracket depth, so a key-looking
    line inside a string or array is never mistaken for a key. Comments are dropped from the
    value text. ValueError on text it cannot follow."""
    out, i, n = [], 0, len(text)
    while True:
        while i < n and (text[i].isspace()):
            i += 1
        while i < n and text[i] == "#":
            while i < n and text[i] != "\n":
                i += 1
            while i < n and text[i].isspace():
                i += 1
        if i >= n or text[i] == "[":
            return out
        line = text.count("\n", 0, i) + 1
        parts = []
        while True:
            while i < n and text[i] in " \t":
                i += 1
            if i < n and text[i] in "\"'":
                j = _skip_string(text, i)
                parts.append(_toml_string(text[i:j]))
                i = j
            else:
                m = re.compile(r"[A-Za-z0-9_-]+").match(text, i)
                if not m:
                    raise ValueError("cannot parse a key at line %d" % line)
                parts.append(m.group(0))
                i = m.end()
            while i < n and text[i] in " \t":
                i += 1
            if i < n and text[i] == ".":
                i += 1
                continue
            break
        if i >= n or text[i] != "=":
            raise ValueError("expected '=' at line %d" % line)
        i += 1
        buf, depth = [], 0
        while i < n:
            c = text[i]
            if c in "\"'":
                j = _skip_string(text, i)
                buf.append(text[i:j])
                i = j
                continue
            if c == "#":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            elif c == "\n" and depth <= 0:
                break
            buf.append(c)
            i += 1
        out.append((".".join(parts), "".join(buf).strip(), line))


def _toml_string(tok):
    if tok.startswith('"""') or tok.startswith("'''"):
        body = tok[3:-3]
        return body[1:] if body.startswith("\n") else body
    if tok[0] == "'":
        return tok[1:-1]
    import json
    try:
        return json.loads(tok)
    except ValueError:
        raise ValueError("bad string %s" % tok) from None


def _toml_value(raw):
    """int, str or list of those from the value text; ValueError for anything else."""
    raw = raw.strip()
    if re.fullmatch(r"[+-]?\d+(?:_\d+)*", raw):
        return int(raw.replace("_", ""))
    if raw in ("true", "false"):
        return raw == "true"
    if raw[:1] in "\"'":
        if _skip_string(raw, 0) != len(raw):
            raise ValueError("trailing text")
        return _toml_string(raw)
    if raw.startswith("[") and raw.endswith("]"):
        items, i, body = [], 0, raw[1:-1]
        while True:
            while i < len(body) and body[i].isspace():
                i += 1
            if i >= len(body):
                return items
            if body[i] not in "\"'":
                raise ValueError("array item is not a string")
            j = _skip_string(body, i)
            items.append(_toml_string(body[i:j]))
            i = j
            while i < len(body) and body[i].isspace():
                i += 1
            if i < len(body):
                if body[i] != ",":
                    raise ValueError("expected ',' in array")
                i += 1
    raise ValueError("unsupported value")


def _codex_config_values(text):
    """{key: python value or _Bad(reason)} for the two keys, from the Codex config text. Uses
    tomllib/tomli when importable, else the scanner above. A reason string means unreadable."""
    found = {}
    if _tomllib is not None:
        try:
            data = _tomllib.loads(text)
        except _tomllib.TOMLDecodeError as exc:
            return {"": _Bad("invalid TOML: %s" % exc)}
        return {k: data[k] for k in _CODEX_KEYS if k in data}
    try:
        pairs = _scan_top_level(text)
    except ValueError as exc:
        return {"": _Bad("cannot read the config: %s" % exc)}
    for key, raw, line in pairs:
        if key in _CODEX_KEYS:
            try:
                found[key] = _toml_value(raw)
            except ValueError as exc:
                found[key] = _Bad("%s (value %s)" % (exc, raw))
    return found


class _Bad(object):
    def __init__(self, reason):
        self.reason = reason


def _codex_home_of(cfg):
    home = cfg.get("agent", {}).get("codex_home", _DEFAULT_CODEX_HOME)
    return os.path.expanduser(home)


def _codex_doc_settings(codex_home):
    """(cap, fallback_names, problem) from <codex_home>/config.toml. The cap defaults to
    rules_block.MAX_BYTES (one source of truth). 0 means Codex reads no project docs. A key
    that is present but unreadable or invalid yields a problem string (file, key, value and
    reason) and the defaults; it never raises, so one bad config cannot kill the other checks."""
    import rules_block
    import boot_budget
    cap, fallbacks, problems = rules_block.MAX_BYTES, [], []
    cfg_path = os.path.join(codex_home, "config.toml")
    data = boot_budget.read_bytes_or_blind(cfg_path, "codex")
    if data is None:
        return cap, fallbacks, None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return cap, fallbacks, "CODEX CONFIG %s: not valid UTF-8; project_doc_max_bytes unknown, " \
            "assuming %d" % (cfg_path, cap)
    for key, v in _codex_config_values(text).items():
        where = "CODEX CONFIG %s: %s" % (cfg_path, key or "config")
        if isinstance(v, _Bad):
            problems.append("%s: %s" % (where, v.reason))
        elif key == "project_doc_max_bytes":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                problems.append("%s = %r: must be a non-negative integer" % (where, v))
            else:
                cap = v
        elif key == "project_doc_fallback_filenames":
            if not isinstance(v, list) or not all(isinstance(x, str) and x and "/" not in x for x in v):
                problems.append("%s = %r: must be a list of file names" % (where, v))
            else:
                fallbacks = v
    return cap, fallbacks, "; ".join(problems) or None


def _codex_doc_in_dir(d, fallbacks):
    """(path, bytes) of the file Codex reads from directory d, or None. Codex takes the FIRST
    existing file among AGENTS.override.md, AGENTS.md and the fallback names, then judges it:
    a blank one means the directory contributes nothing (no fall-through)."""
    import boot_budget
    for name in ["AGENTS.override.md", "AGENTS.md"] + list(fallbacks):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            data = boot_budget.read_bytes_or_blind(p, "codex")
            if data is None:  # unreadable: recorded as a blind read, counts as nothing
                return None
            return (p, len(data)) if data.strip() else None
    return None


def codex_project_doc_findings(cfg):
    """Codex's project instructions (AGENTS.md chain) are read until project_doc_max_bytes
    (32 KiB default) and the rest is dropped. Per scope root: HARD finding when the project
    chain is >= CODEX_BOOT_WARN_FRACTION of the cap; SOFT finding when global + project passes
    the cap (per the openai/codex source the limit applies to project files only, so that is a
    signal, not a loss). A cap of 0 is Codex's off switch: silent. A config this cannot read
    is ONE finding line, never an exception. Runs in either agent mode whenever a Codex home
    exists; silent when clean.
    The chain: Codex walks up from the working directory to the project root (.git, or
    project_root_markers) and reads one file per directory on the way down. This uses git for
    the root and degrades to the scope root alone when it is not in a repo."""
    codex_home = _codex_home_of(cfg)
    if not os.path.isdir(codex_home):
        return []
    cap, fallbacks, problem = _codex_doc_settings(codex_home)
    out = [problem] if problem else []
    if cap == 0:
        return out
    g = _codex_doc_in_dir(codex_home, [])
    g_size = g[1] if g else 0
    for s in cfg.get("scopes", []):
        root = os.path.realpath(s["root"])
        if not os.path.isdir(root):
            continue
        top = notes_config._git_root(root)
        top = os.path.realpath(top) if top else root
        if not (root == top or root.startswith(top + os.sep)):
            top = root
        dirs, cur = [root], root
        while cur != top:
            cur = os.path.dirname(cur)
            dirs.append(cur)
        total = 0
        for d in dirs:
            hit = _codex_doc_in_dir(d, fallbacks)
            total += hit[1] if hit else 0
        if total >= CODEX_BOOT_WARN_FRACTION * cap:
            out.append("CODEX AGENTS.md %s: project chain %d of %d bytes (>= %d%%); Codex stops "
                       "adding project files at project_doc_max_bytes and drops the rest"
                       % (s["name"], total, cap, round(CODEX_BOOT_WARN_FRACTION * 100)))
        elif total > 0 and total + g_size > cap:
            out.append("CODEX AGENTS.md %s (soft): global %d + project %d = %d bytes, over %d; "
                       "the limit applies to project files only per the Codex source, so this "
                       "is a signal, not a loss" % (s["name"], g_size, total, total + g_size, cap))
    return out


def report_mode(cfg, out=None):
    """Run every check. Silent + 0 when clean; report on `out` + EXIT_FINDINGS otherwise."""
    out = sys.stdout if out is None else out
    import boot_budget
    boot_budget.blind_reset()
    results = []
    any_found = False
    skip = manifest_paths(cfg)
    kind = agent_kind(cfg)
    # A scope whose ROOT is gone is a moved or mistyped config entry: its derived
    # store would otherwise drop out of the report silently (configured_stores skips
    # a derived store that does not exist yet, which is normal only for a real root).
    for s in cfg.get("scopes", []):
        if not os.path.isdir(s["root"]):
            findings = _empty_findings()
            findings["frontmatter_drift"] = ["SCOPE ROOT MISSING: %s (scope %r)" % (s["root"], s["name"])]
            results.append((s["name"], Path(s["store"]), findings, dict(_EMPTY_SUMMARY)))
            any_found = True
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
        findings, summary = run_checks(store_path, threshold, skip)
        if has_findings(findings):
            any_found = True
        results.append((label, store_path, findings, summary))

    # Claude Code only: @import reachability and its per-project store layout. In
    # codex mode the store checks above still run, on Codex's own stores.
    ref_imported, orphan_stores = [], []
    if kind == "claude":
        reached = reached_files(cfg)
        ref_imported = reference_imported_findings(cfg, reached)
        orphan_stores = orphan_store_findings(cfg, reached)
    codex_boot = codex_boot_findings(cfg) if kind == "codex" else []
    # Both agents run on the same folders, so a Codex home alone turns this on.
    codex_docs = codex_project_doc_findings(cfg)
    # Every file we found but could not read: the checks above are blind to it, so it is
    # a finding in its own right (an orphan verdict that leaned on one is withheld above).
    blind = ["BLIND %s: cannot read (%s); the check is blind for scope %s"
             % (p, r, sc) for p, sc, r in boot_budget.blind_reads()]
    if ref_imported or orphan_stores or codex_boot or codex_docs or blind:
        any_found = True

    if not any_found:
        return 0  # silent when clean

    # Clean stores stay in the report for context.
    report = build_report(results, kind)
    if ref_imported:
        report += "\n## REFERENCE.md imported into a boot set\n\n"
        report += "\n".join("- " + f for f in ref_imported) + "\n"
    if orphan_stores:
        report += "\n## Orphan STOREs (every store on disk)\n\n"
        report += "\n".join("- " + f for f in orphan_stores) + "\n"
    if codex_boot:
        report += "\n## Codex boot size\n\n"
        report += "\n".join("- " + f for f in codex_boot) + "\n"
    if codex_docs:
        report += "\n## Codex AGENTS.md project size\n\n"
        report += "\n".join("- " + f for f in codex_docs) + "\n"
    if blind:
        report += "\n## Unreadable files (present but cannot be read; the check is blind)\n\n"
        report += "\n".join("- " + f for f in blind) + "\n"
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

    # --- a declared scope manifest is structural; the same file undeclared is not ---
    with tempfile.TemporaryDirectory() as d:
        scope_root = os.path.join(d, "alpha")
        scope_store = os.path.join(d, "alpha-store")
        _write(Path(scope_store) / "reference_y.md", _mem_file("y"))
        _write(Path(scope_store) / "MEMORY.md", "# Memory Index\n\n- [Y](reference_y.md) -- hook\n")
        _write(Path(scope_store) / "scope-manifest.md", "Routing for alpha.\nkey: value\n")
        undeclared = _test_cfg(d, [{"name": "alpha", "root": scope_root, "store": scope_store}])
        f, _ = run_checks(scope_store, 300, manifest_paths(undeclared))
        check(any("scope-manifest.md" in x for x in f["orphan_files"]),
              "an undeclared non-memory file is still an orphan (anti-population)")
        declared = _test_cfg(d, [{"name": "alpha", "root": scope_root, "store": scope_store,
                                  "manifest": os.path.join(scope_store, "scope-manifest.md")}])
        f, _ = run_checks(scope_store, 300, manifest_paths(declared))
        check(not has_findings(f), "a declared manifest is neither an orphan nor frontmatter drift")
        check(not any(c.name == "scope-manifest.md" for _, _, c in non_empty_stores(declared)
                      for c in c),
              "a declared manifest does not count as store content")

    # --- a derived store that does not exist yet is skipped; a named one is reported ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "fresh"))
        cfg = _test_cfg(d, [{"name": "fresh", "root": os.path.join(d, "fresh"),
                             "store": os.path.join(d, "never-saved"), "store_derived": True}])
        cfg["paths"]["global_store_derived"] = True
        cap = _Capture()
        check(report_mode(cfg, cap) == 0 and cap.text() == "",
              "fresh install: derived stores not created yet are silent, exit 0")
        cfg["scopes"][0]["store_derived"] = False
        cap = _Capture()
        check(report_mode(cfg, cap) == EXIT_FINDINGS and "STORE MISSING" in cap.text()
              and "never-saved" in cap.text(),
              "a NAMED store that is missing is still reported")
        cfg["scopes"][0]["store_derived"] = True
        cfg["scopes"][0]["root"] = os.path.join(d, "frsh")  # a typo in the root
        cap = _Capture()
        check(report_mode(cfg, cap) == EXIT_FINDINGS and "SCOPE ROOT MISSING" in cap.text()
              and "frsh" in cap.text(),
              "a scope whose root does not exist is reported, even with a derived store")

    # --- codex mode: store checks run on the selected stores, Claude-only checks do not ---
    with tempfile.TemporaryDirectory() as d:
        cfg = _test_cfg(d)
        cfg["agent"] = {"kind": "codex"}
        g_store = Path(cfg["paths"]["global_store"])
        _write(g_store / "MEMORY.md", "# Memory Index\n\n- [Gone](reference_gone.md) -- hook\n")
        saved = reached_files
        called = []
        globals()["reached_files"] = lambda c: called.append(1) or {}
        try:
            cap = _Capture()
            rc = report_mode(cfg, cap)
        finally:
            globals()["reached_files"] = saved
        check(rc == EXIT_FINDINGS and "reference_gone.md" in cap.text(),
              "codex mode: the store checks run and report a dangling entry")
        check(not called, "codex mode: the Claude-only @import checks are skipped")
        check("(codex)" in cap.text(), "the report names the agent kind")

    # --- .claude/CLAUDE.md entry points; unreadable files are loud, never a silent skip ---
    import builtins
    import contextlib
    import boot_budget as bb

    @contextlib.contextmanager
    def deny_open(*suffixes):
        """open() raises PermissionError(1, ...) for paths ending in a suffix, as macOS
        privacy (TCC) does under launchd for a file stat() can see."""
        real_open = builtins.open

        def fake_open(f, *a, **k):
            if str(f).endswith(suffixes):
                raise PermissionError(1, "Operation not permitted")
            return real_open(f, *a, **k)
        builtins.open = fake_open
        try:
            yield
        finally:
            builtins.open = real_open

    orig_ovr = bb.CONFIG_DIR_OVERRIDE
    orig_dis = os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
    try:
        with tempfile.TemporaryDirectory() as d:
            cfg = _test_cfg(d)
            claude_home = cfg["paths"]["claude_home"]
            bb.CONFIG_DIR_OVERRIDE = claude_home
            root = os.path.join(d, "proj")
            store = os.path.join(claude_home, "elsewhere", "proj-store")
            _write(os.path.join(store, "MEMORY.md"), "# Memory Index\n\n- [A](reference_a.md) -- hook\n")
            _write(os.path.join(store, "reference_a.md"), _mem_file("a"))
            cfg["scopes"] = [{"name": "proj", "root": root, "store": store}]
            os.makedirs(root, exist_ok=True)

            # (a) CLAUDE.md at <root>/.claude/CLAUDE.md is an entry point.
            _write(os.path.join(root, ".claude", "CLAUDE.md"),
                   "# proj\n@%s\n" % os.path.join(store, "MEMORY.md"))
            check(os.path.join(root, ".claude", "CLAUDE.md") in entry_points(cfg)
                  and os.path.join(root, "CLAUDE.md") in entry_points(cfg),
                  "entry_points: <root>/.claude/CLAUDE.md is included beside <root>/CLAUDE.md")
            bb.blind_reset()
            check([f for f in orphan_store_findings(cfg) if store in f] == [],
                  "a scope whose only CLAUDE.md is <root>/.claude/CLAUDE.md is not an orphan store")

            # (b) the TCC false positive: the project's settings turn auto memory ON over a
            # user-level OFF, but cannot be read. The store sits at its native path.
            os.remove(os.path.join(root, ".claude", "CLAUDE.md"))
            _write(os.path.join(root, "CLAUDE.md"), "# proj: no memory import\n")
            native = bb.native_memory_path(root)
            cfg["scopes"] = [{"name": "proj", "root": root, "store": os.path.dirname(native)}]
            _write(native, "# Memory Index\n\n- [A](reference_a.md) -- hook\n")
            _write(os.path.join(os.path.dirname(native), "reference_a.md"), _mem_file("a"))
            nstore = os.path.dirname(native)
            _write(os.path.join(claude_home, "settings.json"), '{"autoMemoryEnabled": false}')
            psettings = os.path.join(root, ".claude", "settings.json")
            _write(psettings, '{"autoMemoryEnabled": true}')
            bb.blind_reset()
            check([f for f in orphan_store_findings(cfg) if nstore in f] == [],
                  "blind control: readable project settings (on) -> native store is not an orphan")
            with deny_open("proj/.claude/settings.json"):
                bb.blind_reset()
                hit = [f for f in orphan_store_findings(cfg) if nstore in f]
            check(len(hit) == 1 and "orphan judgment NOT made" in hit[0] and "blind" in hit[0]
                  and psettings in hit[0] and "no CLAUDE.md entry point" not in hit[0],
                  "blind: unreadable project settings -> a blind finding naming the file, "
                  "NOT an orphan verdict")
            cap = _Capture()
            with deny_open("proj/.claude/settings.json"):
                rc = report_mode(cfg, cap)
            check(rc == EXIT_FINDINGS and "NOT made" in cap.text() and psettings in cap.text()
                  and "no CLAUDE.md entry point" not in cap.text(),
                  "blind: report_mode exits %d with the blind finding and no orphan line" % EXIT_FINDINGS)
            # absent settings stay silent; malformed JSON is blind
            os.remove(psettings)
            _write(os.path.join(claude_home, "settings.json"), '{}')
            bb.blind_reset()
            check(orphan_store_findings(cfg) == [] and bb.blind_reads() == [],
                  "blind: an absent project settings file is silent (auto memory defaults on)")
            _write(psettings, "{not json")
            bb.blind_reset()
            hit = [f for f in orphan_store_findings(cfg) if nstore in f]
            check(len(hit) == 1 and "NOT made" in hit[0] and "JSONDecodeError" in hit[0],
                  "blind: malformed project settings JSON is a blind finding")
            os.remove(psettings)

            # An unreadable entry point: the store it may import is not judged, and the file
            # is reported even when no store depends on it.
            cfg["scopes"] = [{"name": "proj", "root": root, "store": store}]
            with deny_open("proj/CLAUDE.md"):
                bb.blind_reset()
                hit = [f for f in orphan_store_findings(cfg) if store in f]
            check(len(hit) == 1 and "NOT made" in hit[0] and "CLAUDE.md" in hit[0],
                  "blind: an unreadable entry point withholds the orphan verdict for its store")
            cap = _Capture()
            with deny_open("proj/CLAUDE.md"):
                rc = report_mode(cfg, cap)
            check(rc == EXIT_FINDINGS and "Unreadable files" in cap.text()
                  and "BLIND" in cap.text(),
                  "blind: report_mode lists the unreadable entry point")
            bb.blind_reset()
            check([f for f in orphan_store_findings(cfg) if store in f]
                  and "no CLAUDE.md entry point" in [f for f in orphan_store_findings(cfg)
                                                       if store in f][0],
                  "blind control: readable and not imported -> the ordinary orphan verdict")

            # --- fix round 1 ---
            # An unreadable GLOBAL entry point withholds the verdict for every store.
            gclaude = os.path.join(claude_home, "CLAUDE.md")
            _write(gclaude, "# global\n")
            with deny_open(os.sep + os.path.join("claude", "CLAUDE.md")):
                bb.blind_reset()
                hit = [f for f in orphan_store_findings(cfg) if store in f]
            check(len(hit) == 1 and "NOT made" in hit[0] and gclaude in hit[0],
                  "blind: an unreadable GLOBAL entry point withholds the orphan verdict")
            os.remove(gclaude)

            # An unrelated user-settings blind read must not withhold a verdict that never
            # reads settings (custom store path), whatever order the stores come in; the
            # store that does depend on settings is withheld.
            nstore2 = os.path.dirname(bb.native_memory_path(os.path.join(d, "proj2")))
            root2 = os.path.join(d, "proj2")
            os.makedirs(root2, exist_ok=True)
            _write(os.path.join(root2, "CLAUDE.md"), "# proj2: no import\n")
            _write(os.path.join(nstore2, "MEMORY.md"), "# Memory Index\n\n- [B](reference_b.md) -- hook\n")
            _write(os.path.join(nstore2, "reference_b.md"), _mem_file("b"))
            two = [{"name": "proj", "root": root, "store": store},
                   {"name": "proj2", "root": root2, "store": nstore2}]
            for order in (two, list(reversed(two))):
                cfg["scopes"] = [dict(x) for x in order]
                reached = reached_files(cfg)
                bb.blind_reset()
                with deny_open(os.sep + os.path.join("claude", "settings.json")):
                    found = orphan_store_findings(cfg, reached)
                mine = [f for f in found if store in f]
                theirs = [f for f in found if nstore2 in f]
                check(len(mine) == 1 and "no CLAUDE.md entry point" in mine[0]
                      and len(theirs) == 1 and "NOT made" in theirs[0],
                      "blind: user settings withhold only the store whose verdict reads settings "
                      "(order %s first)" % order[0]["name"])
            cfg["scopes"] = [{"name": "proj", "root": root, "store": store}]
            shutil.rmtree(nstore2)
            shutil.rmtree(nstore)  # leftover native-path stores of earlier cases
            os.makedirs(cfg["paths"]["global_store"], exist_ok=True)

            # Blind-only: nothing depends on the unreadable file, still a finding, still listed.
            _write(os.path.join(root, "CLAUDE.md"),
                   "# proj\n@%s\n" % os.path.join(store, "MEMORY.md"))
            _write(os.path.join(root, ".claude", "CLAUDE.md"), "# other entry point\n")
            cap = _Capture()
            with deny_open(os.path.join("proj", ".claude", "CLAUDE.md")):
                rc = report_mode(cfg, cap)
            check(rc == EXIT_FINDINGS and "Unreadable files" in cap.text()
                  and "NOT made" not in cap.text() and "no CLAUDE.md entry point" not in cap.text(),
                  "blind: an unreadable file no verdict depends on is still a finding (rc 3, listed)")
            # report_mode and a standalone orphan_store_findings start from a clean record.
            os.remove(os.path.join(root, ".claude", "CLAUDE.md"))
            bb.record_blind("/stale/entry.md", PermissionError(1, "Operation not permitted"), "global")
            cap = _Capture()
            rc = report_mode(cfg, cap)
            check(rc == 0 and cap.text() == "", "blind: report_mode starts from a clean record (no stale entry)")
            _write(os.path.join(root, "CLAUDE.md"), "# proj: no import\n")
            bb.record_blind("/stale/entry.md", PermissionError(1, "Operation not permitted"), "global")
            hit = [f for f in orphan_store_findings(cfg) if store in f]
            check(len(hit) == 1 and "no CLAUDE.md entry point" in hit[0],
                  "blind: a standalone orphan_store_findings does not inherit stale entries")

            # A blind read is matched to its scope by one label: a trailing-slash root
            # still withholds its own store's verdict.
            cfg["scopes"] = [{"name": "proj", "root": root + os.sep, "store": store}]
            with deny_open("proj/CLAUDE.md"):
                bb.blind_reset()
                hit = [f for f in orphan_store_findings(cfg) if store in f]
            check(len(hit) == 1 and "NOT made" in hit[0] and "no CLAUDE.md entry point" not in hit[0],
                  "blind: a trailing-slash scope root still withholds its store's verdict")
            cfg["scopes"] = [{"name": "proj", "root": root, "store": store}]

            # The global "~" root, with the config dir apart from ~/.claude: ~/.claude/
            # settings.json is that root's PROJECT settings, recorded under the home path.
            orig_home = bb.HOME
            bb.HOME = os.path.join(d, "home")
            try:
                gstore = os.path.dirname(bb.native_memory_path("~"))
                cfg["paths"]["global_store"] = gstore
                _write(os.path.join(gstore, "MEMORY.md"), "# Memory Index\n\n- [G](reference_g.md) -- hook\n")
                _write(os.path.join(gstore, "reference_g.md"), _mem_file("g"))
                _write(os.path.join(bb.HOME, ".claude", "settings.json"), '{"autoMemoryEnabled": true}')
                bb.blind_reset()
                check([f for f in orphan_store_findings(cfg) if gstore in f] == [],
                      "blind control: readable ~/.claude/settings.json (on) -> global store not an orphan")
                with deny_open(os.path.join("home", ".claude", "settings.json")):
                    bb.blind_reset()
                    hit = [f for f in orphan_store_findings(cfg) if gstore in f]
                check(len(hit) == 1 and "NOT made" in hit[0] and "no CLAUDE.md entry point" not in hit[0],
                      "blind: unreadable ~/.claude/settings.json withholds the global store's verdict "
                      "(config dir apart from ~/.claude)")
            finally:
                bb.HOME = orig_home

        # (c) codex: warn at >= 90% of agent.codex_boot_bytes.
        with tempfile.TemporaryDirectory() as d:
            cfg = _test_cfg(d)
            cfg["agent"] = {"kind": "codex", "codex_boot_bytes": 1000}
            root = os.path.join(d, "proj")
            os.makedirs(root)
            g = cfg["paths"]["global_store"]
            sstore = os.path.join(d, "cx", "proj")
            cfg["scopes"] = [{"name": "proj", "root": root, "store": sstore}]
            _write(os.path.join(g, "MEMORY.md"), "g" * 300)
            _write(os.path.join(g, "RESUME.md"), "r" * 100)
            _write(os.path.join(sstore, "MEMORY.md"), "m" * 300)
            _write(os.path.join(sstore, "RESUME.md"), "s" * 199)
            check(codex_boot_findings(cfg) == [], "codex boot: 899 of 1000 bytes is under 90%")
            _write(os.path.join(sstore, "RESUME.md"), "s" * 200)
            got = codex_boot_findings(cfg)
            check(got == ["CODEX BOOT proj: 900 of 1000 bytes (>= 90%); the SessionStart hook "
                          "drops files past the cap"],
                  "codex boot: exactly 90% fires, naming label, size and cap")
            _write(os.path.join(g, "MEMORY.md"), "g" * 900)
            got = codex_boot_findings(cfg)
            check(len(got) == 2 and got[0].startswith("CODEX BOOT global: 1000 of 1000")
                  and got[1].startswith("CODEX BOOT proj: 1500 of 1000"),
                  "codex boot: the global store alone and each scope are judged, over the cap too")
            cap = _Capture()
            rc = report_mode(cfg, cap)
            check(rc == EXIT_FINDINGS and "CODEX BOOT global" in cap.text(),
                  "codex boot: report_mode reports it in codex mode")
            with deny_open("cx/proj/RESUME.md"):
                bb.blind_reset()
                codex_boot_findings(cfg)
                blind = bb.blind_reads()
            check(len(blind) == 1 and blind[0][0].endswith("RESUME.md"),
                  "codex boot: an unreadable file is a blind read, not size 0 silently")
            cfg["agent"] = {"kind": "codex"}  # no codex_boot_bytes: the notes_config default
            _write(os.path.join(g, "MEMORY.md"), "g" * 10800)
            check(codex_boot_findings(cfg)[0].startswith("CODEX BOOT global: 10900 of %d"
                  % notes_config.DEFAULTS["agent"]["codex_boot_bytes"]),
                  "codex boot: the cap's default lives in notes_config only")
            cfg["agent"] = {"kind": "claude", "codex_boot_bytes": 1000}
            cap = _Capture()
            report_mode(cfg, cap)
            check("CODEX BOOT" not in cap.text(), "codex boot: never fires in claude mode")
    finally:
        bb.CONFIG_DIR_OVERRIDE = orig_ovr
        if orig_dis is not None:
            os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = orig_dis

    # --- Codex project AGENTS.md chain vs project_doc_max_bytes ---
    globals()["_DEFAULT_CODEX_HOME"] = os.path.join(tempfile.gettempdir(), "no-such-codex-home-selftest")
    with tempfile.TemporaryDirectory() as d:
        ch = os.path.join(d, "cxhome")
        root = os.path.join(d, "work", "proj")
        os.makedirs(root)
        cfg = _test_cfg(d, [{"name": "proj", "root": root, "store": os.path.join(d, "st")}])
        cfg["agent"] = {"kind": "claude", "codex_home": ch}
        import rules_block as _rb
        cap = _rb.MAX_BYTES
        _write(os.path.join(root, "AGENTS.md"), "x" * cap)
        check(codex_project_doc_findings(cfg) == [], "codex docs: no Codex home -> silent even over the cap")
        os.makedirs(ch)
        _write(os.path.join(root, "AGENTS.md"), "x" * (int(0.9 * cap) - 1))
        check(codex_project_doc_findings(cfg) == [], "codex docs: just under 90% of the cap is silent")
        _write(os.path.join(root, "AGENTS.md"), "x" * int(0.9 * cap + 1))
        got = codex_project_doc_findings(cfg)
        check(len(got) == 1 and "project chain" in got[0] and "proj" in got[0] and "(soft)" not in got[0],
              "codex docs: project chain over 90% fires (hard), in claude mode too")
        cap_r = _Capture()
        check(report_mode(cfg, cap_r) == EXIT_FINDINGS and "Codex AGENTS.md project size" in cap_r.text(),
              "codex docs: report_mode reports it")
        _write(os.path.join(root, "AGENTS.override.md"), "tiny")
        check(codex_project_doc_findings(cfg) == [],
              "codex docs: AGENTS.override.md beats AGENTS.md in the same directory")
        _write(os.path.join(root, "AGENTS.override.md"), "  \n")
        check(codex_project_doc_findings(cfg) == [],
              "codex docs: a blank override means the directory contributes nothing (no fall-through)")
        os.remove(os.path.join(root, "AGENTS.override.md"))
        _write(os.path.join(ch, "config.toml"), 'project_doc_fallback_filenames = ["TEAM.md"]\n')
        _write(os.path.join(root, "TEAM.md"), "y" * (cap - 1))
        got = codex_project_doc_findings(cfg)
        check(got and "project chain %d " % int(0.9 * cap + 1) in got[0],
              "codex docs: a fallback name is ignored while AGENTS.md exists")
        os.remove(os.path.join(root, "AGENTS.md"))
        got = codex_project_doc_findings(cfg)
        check(len(got) == 1 and "project chain %d " % (cap - 1) in got[0],
              "codex docs: the fallback name is used once AGENTS.md is missing")
        os.remove(os.path.join(root, "TEAM.md"))
        _write(os.path.join(ch, "config.toml"), "project_doc_max_bytes = 100\n")
        _write(os.path.join(root, "AGENTS.md"), "z" * 90)
        got = codex_project_doc_findings(cfg)
        check(len(got) == 1 and "90 of 100 bytes" in got[0], "codex docs: config project_doc_max_bytes is honored")

        # config.toml reading: every case below is checked against the real TOML meaning.
        long_str = 'prompt = """\n' + "".join("line %d\n" % i for i in range(88)) + '"""\n'
        cases = [
            ("key inside a top-level \"\"\" string is not a key",
             'prompt = """\nproject_doc_max_bytes = 100\n"""\n', (cap, [])),
            ("fallback key inside a ''' string is not a key",
             "note = '''\nproject_doc_fallback_filenames = [\"EVIL.md\"]\n'''\n", (cap, [])),
            ("a line starting with [ inside a string does not end the scan",
             'p = """\n[not a table]\n"""\nproject_doc_max_bytes = 65536\n', (65536, [])),
            ("a nested multi-line array before the key",
             'x = [\n  [1, 2],\n  [3]\n]\nproject_doc_max_bytes = 65536\n', (65536, [])),
            ("a quoted key", '"project_doc_max_bytes" = 65536\n', (65536, [])),
            ("a key after an 88-line string", long_str + "project_doc_max_bytes = 65536\n", (65536, [])),
            ("a multi-line fallback array with comments",
             'project_doc_fallback_filenames = [\n  "A.md", # one\n  \'B.md\',\n]\n', (cap, ["A.md", "B.md"])),
            ("an integer with underscores", "project_doc_max_bytes = 65_536 # bytes\n", (65536, [])),
            ("a key under a table is not top-level",
             '[profile]\nproject_doc_max_bytes = 5\n', (cap, [])),
        ]
        for name, text, want in cases:
            _write(os.path.join(ch, "config.toml"), text)
            c, fb, prob = _codex_doc_settings(ch)
            check((c, fb) == want and prob is None, "codex config: %s" % name)
        # The scanner alone, so the tomllib branch cannot hide a scanner defect.
        for name, text, want in cases:
            try:
                vals = {k: v for k, v, _ in _scan_top_level(text) if k in _CODEX_KEYS}
                got_cap = _toml_value(vals["project_doc_max_bytes"]) if "project_doc_max_bytes" in vals else cap
                got_fb = _toml_value(vals["project_doc_fallback_filenames"]) if "project_doc_fallback_filenames" in vals else []
            except ValueError:
                got_cap, got_fb = None, None
            check((got_cap, got_fb) == want, "codex config scanner: %s" % name)

        _write(os.path.join(ch, "config.toml"), "project_doc_max_bytes = 0\n")
        _write(os.path.join(root, "AGENTS.md"), "z" * (cap * 2))
        check(codex_project_doc_findings(cfg) == [], "codex docs: project_doc_max_bytes = 0 (Codex off switch) is silent")
        for text, key, shown in (('project_doc_max_bytes = "big"\n', "project_doc_max_bytes", "big"),
                                 ("project_doc_max_bytes = -5\n", "project_doc_max_bytes", "-5"),
                                 ("project_doc_max_bytes = true\n", "project_doc_max_bytes", "true"),
                                 ("project_doc_fallback_filenames = [1]\n", "project_doc_fallback_filenames", "1"),
                                 ('project_doc_fallback_filenames = "A.md"\n', "project_doc_fallback_filenames", "A.md")):
            _write(os.path.join(ch, "config.toml"), text)
            try:
                got = codex_project_doc_findings(cfg)
            except Exception as exc:  # a bad config must never raise
                got = ["RAISED %r" % exc]
            check(any("CODEX CONFIG" in f and key in f and shown.lower() in f.lower() for f in got),
                  "codex config: invalid %s is one finding naming key and value" % text.strip())
        _write(os.path.join(ch, "config.toml"), 'project_doc_max_bytes = "big"\n')
        cap_r = _Capture()
        rc = report_mode(cfg, cap_r)
        check(rc == EXIT_FINDINGS and "CODEX CONFIG" in cap_r.text() and "STORE MISSING" in cap_r.text(),
              "codex config: a bad config prints its finding AND the other findings still print")
        _write(os.path.join(ch, "config.toml"), 'x = """\nunterminated\n')
        check(any("CODEX CONFIG" in f for f in codex_project_doc_findings(cfg)),
              "codex config: an unterminated string is a finding, not a crash")
        os.remove(os.path.join(ch, "config.toml"))

        # soft: global + project over the cap while the project alone is under 90%
        _write(os.path.join(ch, "config.toml"), "project_doc_max_bytes = 1000\n")
        _write(os.path.join(root, "AGENTS.md"), "p" * 500)
        _write(os.path.join(ch, "AGENTS.md"), "g" * 600)
        got = codex_project_doc_findings(cfg)
        check(len(got) == 1 and "(soft)" in got[0], "codex docs: global + project over the cap is a labelled soft signal")
        os.remove(os.path.join(root, "AGENTS.md"))
        _write(os.path.join(ch, "AGENTS.md"), "g" * 1200)
        check(codex_project_doc_findings(cfg) == [], "codex docs: no project docs -> no soft signal from the global file alone")
        _write(os.path.join(root, "AGENTS.md"), "p" * 500)
        # unreadable project file: no exception, recorded as a blind read
        os.chmod(os.path.join(root, "AGENTS.md"), 0)
        if not os.access(os.path.join(root, "AGENTS.md"), os.R_OK):
            bb.blind_reset()
            try:
                codex_project_doc_findings(cfg)
                raised = False
            except Exception:
                raised = True
            check(not raised and len(bb.blind_reads()) >= 1, "codex docs: an unreadable AGENTS.md is a blind read, not a crash")
        os.chmod(os.path.join(root, "AGENTS.md"), 0o644)
        # parent chain: git root above the scope root counts
        import subprocess
        subprocess.run(["git", "init", "-q", os.path.join(d, "work")], check=True)
        _write(os.path.join(d, "work", "AGENTS.md"), "q" * 400)
        os.remove(os.path.join(ch, "AGENTS.md"))
        got = codex_project_doc_findings(cfg)
        check(len(got) == 1 and "900 of 1000" in got[0], "codex docs: AGENTS.md files from the git root down are summed")
        cfg["agent"]["codex_home"] = "~/no-such-dir-selftest"
        check(_codex_home_of(cfg) == os.path.expanduser("~/no-such-dir-selftest"), "codex docs: ~ in codex_home is expanded")

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
