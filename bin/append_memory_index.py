#!/usr/bin/env python3
"""Atomic single-line append helper for a MEMORY.md index.

A memory store is a directory holding MEMORY.md (the index: one
`- [Title](file.md) - hook` line per memory), RESUME.md (where the last session left
off), and one .md file per memory. This helper owns exactly one operation on it:
adding a new index line.

WHY THIS HELPER: /reflect's "Update MEMORY.md" step used to be enforced only by PROSE
("re-read from disk immediately before writing and append"), a real TOCTOU window when
several /reflect sessions run concurrently. Two sibling sessions can both read the same
on-disk MEMORY.md, each append their own new line to their in-memory copy, and
whichever session's whole-file write lands LAST silently drops the other's entry. This
helper makes "re-read, append, write" a single flocked, atomic operation instead:
flock -> re-read current on-disk content INSIDE the lock -> append (idempotently) ->
write-temp-then-rename.

It handles ONLY the mechanical index-line append. It deliberately does NOT touch
RESUME.md (that stays a semantic merge: judgment, not a bare append) or any other
memory-file content.

CHOKEPOINT: this is the ONE place a new MEMORY.md index line is ever written (the
reflect skill only ever calls it, never a hand-edit), so it is where the hook-length cap
lives: not as a follow-up lint someone could forget to run, but as a mechanism this
function itself enforces before it will write anything. It imports `check_line()` from
`memory_index_lint.py` (co-located in bin/) so the cap logic has exactly one home;
`lint_hooks()`/`lint()` in that module call the SAME function for their whole-file
pass. See memory_index_lint.HookCapError.

COLD-TIER ROUTING: a new index line whose link target is a `reference_*.md` file (a
tool/vendor/service/machine lookup, not a user_/feedback_/project_ fact) is written to
`REFERENCE.md` beside the given index, not the index itself. REFERENCE.md is never
`@import`ed, so this keeps new cold-tier entries out of the boot prefix by construction
rather than relying on someone to remember to route them by hand. Pass `pin=True` (CLI:
`--pin`) for a `reference_*.md` entry that's a boot-critical trap (a NEVER/ALWAYS/HARD
RULE or a silent-false-result gotcha) and belongs hot instead; that routes it to the
index like any non-reference_ line. A non-`reference_*.md` line is unaffected by any of
this and always goes to the index. REFERENCE.md is created with a short header if it
doesn't exist yet. The hook-length cap applies identically either way (see
check_line()): routing never changes what counts as over-cap.

Usage:
  append_memory_index.py <path-to-MEMORY.md> "<the full one-line index entry>" [--pin]
  append_memory_index.py --scope NAME "<the full one-line index entry>" [--pin]
      (NAME is "global" for paths.global_store, or a [[scopes]] name from the config)
  append_memory_index.py --selftest

Exit codes: 0 = appended or already-present (both are success: idempotent), 2 = bad
usage, an unknown scope, OR a cap-refused line (see stderr). append_line() itself never
raises for the normal missing-file case (creates it); it DOES raise
memory_index_lint.HookCapError for an over-cap NEW line, and raises it before touching
disk at all.
"""
import fcntl
import os
import subprocess
import sys
import tempfile

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import memory_index_lint  # noqa: E402

# What a store's REFERENCE.md gets auto-created with on its first cold write via this
# helper. A store that already has a REFERENCE.md keeps its own header untouched.
REFERENCE_HEADER = (
    "# Cold Reference Index\n\n"
    "Tool/vendor/service/machine lookups pulled out of MEMORY.md so they no longer "
    "load at boot. NOT `@import`ed anywhere on purpose. Search this "
    "file before using an unfamiliar tool, vendor, service, or machine; read the "
    "target memory file for the how-to.\n\n"
)


def _is_reference_target(line):
    """True if `line`'s index-link target is a reference_*.md file (cold-tier by
    default). Non-index-shaped lines are never reference targets."""
    result = memory_index_lint.check_line(line)
    target = result["target"]
    return bool(target) and os.path.basename(target).startswith("reference_")


def _reference_index_path(memory_path):
    return os.path.join(os.path.dirname(os.path.abspath(memory_path)), "REFERENCE.md")


def target_path_for(path, line, pin=False):
    """Which file `line` actually lands in: `path` itself, or REFERENCE.md beside it
    when `line` is a reference_*.md entry and `pin` is False. The single routing
    decision both append_line() and any caller that wants to know in advance should
    use."""
    if not pin and _is_reference_target(line):
        return _reference_index_path(path)
    return path


def append_line(path, line, enforce_cap=True, pin=False):
    """Flocked idempotent single-line append to `path` (created if missing).

    Acquires an exclusive lock on a per-file sidecar `<path>.lock`, re-reads `path`
    from disk INSIDE the lock (so a concurrent sibling's append is never clobbered),
    appends `line` as its own line unless an identical line already exists anywhere in
    the file, and writes atomically (temp file in the same directory + os.replace, so
    a reader never observes a half-written file). Returns True if the line was
    appended, False if it was already present (no-op, still success).

    `enforce_cap` (default True): before touching disk at all, runs `line` through
    memory_index_lint.check_line() and raises memory_index_lint.HookCapError if its
    hook is over cap (110 chars for a feedback_*.md target, 80 otherwise): the
    chokepoint. A line that isn't index-shaped (doesn't match the
    `- [Title](file.md) - hook` pattern) is unaffected, same as check_line()'s own
    behavior. Pass enforce_cap=False only for a deliberate non-index append (this
    module's own contract says it handles ONLY index lines, so real callers should
    never need this).

    `pin` (default False): a reference_*.md line is routed to REFERENCE.md beside
    `path` (created with a short header if absent) unless `pin=True`, in which case it
    goes to `path` like any hot entry. A non-reference_ line always goes to `path`
    regardless of `pin` (see module docstring). The returned bool
    and idempotency semantics are unchanged; callers that want to know WHICH file was
    actually written should call `target_path_for(path, line, pin)` themselves.

    If `line` is ALREADY present in `path` (the hot index)
    itself, this is a no-op regardless of routing/`pin` -- an unpinned reference_ line
    that was previously pinned (still sitting in MEMORY.md) must never ALSO get written
    to REFERENCE.md, which would leave it indexed in both places (a hygiene finding). Only
    the hot file is checked here (the cheap, common case); the destination file's own
    idempotency check below still covers everything else, including a duplicate call
    that's already landed in REFERENCE.md.
    """
    line = line.rstrip("\n")
    if enforce_cap:
        result = memory_index_lint.check_line(line)
        if not result["ok"]:
            raise memory_index_lint.HookCapError(result["message"])

    try:
        with open(path, encoding="utf-8") as fh:
            if line in fh.read().splitlines():
                return False
    except FileNotFoundError:
        pass

    effective_path = target_path_for(path, line, pin)
    directory = os.path.dirname(os.path.abspath(effective_path)) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = effective_path + ".lock"
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                with open(effective_path, encoding="utf-8") as fh:
                    content = fh.read()
            except FileNotFoundError:
                content = REFERENCE_HEADER if effective_path != path else ""

            if line in content.splitlines():
                return False

            if content and not content.endswith("\n"):
                content += "\n"
            content += line + "\n"

            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=directory, prefix=".append_memory_index_tmp_")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_fh:
                    tmp_fh.write(content)
                os.replace(tmp_path, effective_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            return True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _usage(stream=sys.stderr):
    print("usage:", file=stream)
    print("  append_memory_index.py <path-to-MEMORY.md> \"<the full one-line index entry>\" [--pin]",
          file=stream)
    print("      A reference_*.md entry routes to REFERENCE.md beside <path> unless --pin",
          file=stream)
    print("      is given, which keeps it in <path>.", file=stream)
    print("  append_memory_index.py --scope NAME \"<entry>\" [--pin]", file=stream)
    print("      NAME is \"global\" or a [[scopes]] name from the config.", file=stream)
    print("  append_memory_index.py --selftest", file=stream)


def selftest():
    failures = 0

    def check(desc, cond):
        nonlocal failures
        failures += 0 if cond else 1
        print("%s %s" % ("PASS" if cond else "FAIL", desc))

    this_file = os.path.abspath(__file__)

    # (1) basic append to a file that doesn't exist yet
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "MEMORY.md")
        ok = append_line(p, "- [Foo](foo.md) - a thing")
        check("append_line returns True on first append (new file)", ok is True)
        check("file created with exactly the one line",
              open(p, encoding="utf-8").read() == "- [Foo](foo.md) - a thing\n")

        # (2) idempotent no-op on an identical line
        ok2 = append_line(p, "- [Foo](foo.md) - a thing")
        check("append_line returns False on an exact duplicate (no-op)", ok2 is False)
        check("file unchanged -- still exactly one copy of the line",
              open(p, encoding="utf-8").read().count("- [Foo](foo.md)") == 1)

        # (3) a second, distinct line still appends (doesn't clobber the first)
        ok3 = append_line(p, "- [Bar](bar.md) - another thing")
        check("append_line returns True for a distinct new line", ok3 is True)
        lines = open(p, encoding="utf-8").read().splitlines()
        check("both lines present, in append order",
              lines == ["- [Foo](foo.md) - a thing", "- [Bar](bar.md) - another thing"])

        # (4) appending to a file with no trailing newline still lands on its own line
        p2 = os.path.join(d, "NoTrailingNewline.md")
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write("- [Existing](x.md) - no trailing newline")
        append_line(p2, "- [New](y.md) - appended after")
        check("append after a no-trailing-newline file doesn't fuse onto the prior line",
              open(p2, encoding="utf-8").read().splitlines() ==
              ["- [Existing](x.md) - no trailing newline", "- [New](y.md) - appended after"])

        # (5) atomic write -- no leftover temp files after successful writes
        leftover = [f for f in os.listdir(d) if f.startswith(".append_memory_index_tmp_")]
        check("no leftover temp files after successful writes", leftover == [])

    # (6) THE REAL RACE -- N separate PROCESSES (mirrors N sibling /reflect
    # sessions each running the CLI once) racing to append DISTINCT lines to the SAME
    # MEMORY.md concurrently. Assert none lost, none duplicated, file not corrupted.
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "MEMORY.md")
        open(p, "w", encoding="utf-8").close()  # seed an existing (empty) file
        n = 12
        lines = ["- [Item%02d](item%02d.md) - concurrent test entry %d" % (i, i, i)
                  for i in range(n)]
        procs = [
            subprocess.Popen([sys.executable, this_file, p, line],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for line in lines
        ]
        rcs = [proc.wait(timeout=30) for proc in procs]
        check("all %d concurrent append processes exited 0" % n, all(rc == 0 for rc in rcs))

        final_lines = [ln for ln in open(p, encoding="utf-8").read().splitlines() if ln.strip()]
        check("all %d concurrently-appended lines present (none lost to a race)" % n,
              all(line in final_lines for line in lines))
        check("no duplicate lines (each appears exactly once)",
              all(final_lines.count(line) == 1 for line in lines))
        check("total line count is exactly %d (no corruption / extra lines)" % n,
              len(final_lines) == n)

        leftover = [f for f in os.listdir(d) if f.startswith(".append_memory_index_tmp_")]
        check("no leftover temp files after the concurrent race", leftover == [])

    # (7) CLI usage errors
    proc = subprocess.run([sys.executable, this_file],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("CLI with no args exits non-zero (usage error)", proc.returncode == 2)

    # (8) CHOKEPOINT -- an over-cap NEW hook line is refused before writing.
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "MEMORY.md")
        long_hook = "x" * 90  # over the 80-char non-feedback cap
        bad_line = "- [Bad](reference_bad.md) - " + long_hook

        raised = False
        try:
            append_line(p, bad_line)
        except memory_index_lint.HookCapError:
            raised = True
        check("append_line raises HookCapError for an over-cap hook line", raised)
        check("a refused append never creates the file (fails before touching disk)",
              not os.path.exists(p))

        # A well-under-cap line still appends normally (the guard isn't over-broad),
        # routed to REFERENCE.md since its target is reference_*.md (see section 9).
        ok_line = "- [Ok](reference_ok.md) - a short hook"
        appended_ok = append_line(p, ok_line)
        check("an in-cap line still appends normally after a prior refusal", appended_ok is True)

        # CLI: same refusal, surfaced as exit 2 + a stderr message naming the cap.
        proc2 = subprocess.run(
            [sys.executable, this_file, p, "- [Bad2](reference_bad2.md) - " + ("y" * 90)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        check("CLI exits 2 on an over-cap hook line", proc2.returncode == 2)
        check("CLI stderr names the cap refusal",
              b"REFUSED" in proc2.stderr and b"cap" in proc2.stderr)
        check("CLI refusal never wrote the over-cap line to the file",
              not os.path.exists(p) or "reference_bad2.md" not in open(p, encoding="utf-8").read())
        ref_p = _reference_index_path(p)
        check("CLI refusal never wrote the over-cap line to REFERENCE.md either",
              not os.path.exists(ref_p) or "reference_bad2.md" not in open(ref_p, encoding="utf-8").read())

        # enforce_cap=False deliberately bypasses the guard (documented escape hatch);
        # routing still applies independently of the cap check.
        appended_bypassed = append_line(p, bad_line, enforce_cap=False)
        check("enforce_cap=False bypasses the guard for a deliberate non-checked append",
              appended_bypassed is True)
        check("the bypassed reference_ line still routed to REFERENCE.md, not MEMORY.md",
              (not os.path.exists(p) or "reference_bad.md" not in open(p, encoding="utf-8").read())
              and "reference_bad.md" in open(ref_p, encoding="utf-8").read())

    # (9) cold-tier routing + --pin.
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "MEMORY.md")
        ref_p = _reference_index_path(p)

        # A non-reference_ line always goes to MEMORY.md, never REFERENCE.md.
        append_line(p, "- [User Fact](user_thing.md) - a durable fact")
        check("a non-reference_ line lands in MEMORY.md",
              "user_thing.md" in open(p, encoding="utf-8").read())
        check("a non-reference_ line never lands in REFERENCE.md",
              not os.path.exists(ref_p) or "user_thing.md" not in open(ref_p, encoding="utf-8").read())

        # A reference_ line with no --pin routes to REFERENCE.md, not MEMORY.md.
        append_line(p, "- [Some Tool](reference_some_tool.md) - how to use it")
        check("an unpinned reference_ line lands in REFERENCE.md",
              os.path.exists(ref_p) and "reference_some_tool.md" in open(ref_p, encoding="utf-8").read())
        check("an unpinned reference_ line never lands in MEMORY.md",
              "reference_some_tool.md" not in open(p, encoding="utf-8").read())
        check("REFERENCE.md is auto-created with a header on first cold write",
              open(ref_p, encoding="utf-8").read().startswith("# Cold Reference Index"))

        # pin=True keeps a reference_ line in MEMORY.md instead.
        append_line(p, "- [Boot Trap](reference_boot_trap.md) - NEVER do the thing", pin=True)
        check("pin=True keeps a reference_ line in MEMORY.md",
              "reference_boot_trap.md" in open(p, encoding="utf-8").read())
        check("pin=True keeps it OUT of REFERENCE.md",
              "reference_boot_trap.md" not in open(ref_p, encoding="utf-8").read())

        # target_path_for() matches append_line()'s actual routing decision.
        check("target_path_for agrees: unpinned reference_ -> REFERENCE.md",
              target_path_for(p, "- [X](reference_x.md) - y") == ref_p)
        check("target_path_for agrees: pinned reference_ -> MEMORY.md",
              target_path_for(p, "- [X](reference_x.md) - y", pin=True) == p)
        check("target_path_for agrees: non-reference_ -> MEMORY.md regardless of pin",
              target_path_for(p, "- [X](user_x.md) - y", pin=True) == p)

        # A line already sitting in the HOT index (e.g. a previously-pinned reference_
        # entry) must stay a no-op if called again WITHOUT --pin: it must never also
        # land in REFERENCE.md (which would leave it indexed in both places).
        already_hot = append_line(p, "- [Boot Trap](reference_boot_trap.md) - NEVER do the thing", pin=False)
        check("a line already present in the HOT index is a no-op even when called unpinned",
              already_hot is False)
        check("it still did NOT get written to REFERENCE.md",
              "reference_boot_trap.md" not in open(ref_p, encoding="utf-8").read())

        # Idempotency is per-destination-file: the SAME reference_ line appended twice
        # (unpinned) is still a no-op, since both calls resolve to REFERENCE.md.
        again = append_line(p, "- [Some Tool](reference_some_tool.md) - how to use it")
        check("a duplicate unpinned reference_ append is idempotent (no-op)", again is False)

        # CLI: --pin flag routes the same way as pin=True.
        p2 = os.path.join(d, "MEMORY2.md")
        ref_p2 = _reference_index_path(p2)
        proc_pin = subprocess.run(
            [sys.executable, this_file, p2, "- [Hard Rule](reference_hard_rule.md) - ALWAYS do X", "--pin"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("CLI --pin exits 0", proc_pin.returncode == 0)
        check("CLI --pin lands the line in MEMORY2.md",
              os.path.exists(p2) and "reference_hard_rule.md" in open(p2, encoding="utf-8").read())
        check("CLI --pin keeps it out of REFERENCE.md",
              not os.path.exists(ref_p2) or "reference_hard_rule.md" not in open(ref_p2, encoding="utf-8").read())

        # CLI: without --pin, a reference_ line routes to REFERENCE.md instead.
        p3 = os.path.join(d, "MEMORY3.md")
        ref_p3 = _reference_index_path(p3)
        proc_nopin = subprocess.run(
            [sys.executable, this_file, p3, "- [Lookup](reference_lookup.md) - a cold-tier fact"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("CLI without --pin exits 0", proc_nopin.returncode == 0)
        check("CLI without --pin lands the line in REFERENCE.md",
              os.path.exists(ref_p3) and "reference_lookup.md" in open(ref_p3, encoding="utf-8").read())
        check("CLI without --pin keeps MEMORY3.md clean of the reference_ line",
              not os.path.exists(p3) or "reference_lookup.md" not in open(p3, encoding="utf-8").read())

    # (10) --scope resolves the index from the config.
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "alpha-store")
        cfg_file = os.path.join(d, "config.toml")
        with open(cfg_file, "w", encoding="utf-8") as fh:
            fh.write('[paths]\nglobal_store = "%s"\n[[scopes]]\nname = "beta"\n'
                     'root = "%s"\nstore = "%s"\n'
                     % (os.path.join(d, "global"), os.path.join(d, "beta"), store))
        env = dict(os.environ, SESSION_NOTES_CONFIG=cfg_file)
        proc_scope = subprocess.run(
            [sys.executable, this_file, "--scope", "beta", "- [Fact](user_fact.md) - scoped"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        check("CLI --scope NAME exits 0", proc_scope.returncode == 0)
        check("CLI --scope NAME appends to that scope's store/MEMORY.md",
              "user_fact.md" in open(os.path.join(store, "MEMORY.md"), encoding="utf-8").read())
        proc_global = subprocess.run(
            [sys.executable, this_file, "--scope", "global", "- [G](user_g.md) - global"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        check("CLI --scope global appends to paths.global_store/MEMORY.md",
              proc_global.returncode == 0 and "user_g.md" in open(
                  os.path.join(d, "global", "MEMORY.md"), encoding="utf-8").read())
        proc_bad = subprocess.run(
            [sys.executable, this_file, "--scope", "gamma", "- [X](user_x.md) - y"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        check("CLI --scope with an unknown name exits 2", proc_bad.returncode == 2)

    print("\n%d failure(s)" % failures)
    return 1 if failures else 0


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        return selftest()
    pin = "--pin" in argv
    rest = [a for a in argv if a != "--pin"]
    scope = None
    if "--scope" in rest:
        i = rest.index("--scope")
        if i + 1 >= len(rest):
            _usage()
            return 2
        scope = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    if scope is not None:
        if len(rest) != 1:
            _usage()
            return 2
        try:
            path = memory_index_lint.index_path_for_scope(scope)
        except memory_index_lint.UnknownScopeError as e:
            print("append_memory_index: %s" % e, file=sys.stderr)
            return 2
        line = rest[0]
    else:
        if len(rest) != 2:
            _usage()
            return 2
        path, line = rest
    if not line.strip():
        print("append_memory_index: empty index line -- nothing to append", file=sys.stderr)
        return 2
    try:
        appended = append_line(path, line, pin=pin)
    except memory_index_lint.HookCapError as e:
        print(str(e), file=sys.stderr)
        return 2
    dest = target_path_for(path, line, pin=pin)
    if appended:
        print("appended to %s" % dest)
    else:
        print("already present (no-op): %s" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
