#!/usr/bin/env python3
"""codex_session_start.py -- Codex SessionStart hook: load the memory index and RESUME.

Claude Code puts the global MEMORY.md and RESUME.md in context through CLAUDE.md
@imports. Codex has no @import (AGENTS.md is plain text, built once per run), so this
hook does the same job: on startup, resume and after compaction (Codex reports source
"compact"), it returns the Codex store's files as additionalContext.

What it loads, in order (each only if present and non-empty):
  <global store>/MEMORY.md, <global store>/RESUME.md,
  <project store>/MEMORY.md, <project store>/RESUME.md
then the live-context block (bin/live.py: what other sessions just did), which only uses
what is left of the budget after those files. The stores come from
notes_config.stores_for(cwd) in codex mode: a configured scope that holds cwd, else the cwd's git root. Nothing under ~/.claude is ever read; the live
file sits outside both agent homes (see bin/live.py).

Budget. Codex spills hook output over ~2,500 tokens (configurable per handler with
additionalContextLimit) to a temp file and shows the model only a head-and-tail
preview. A silent spill would hide the middle of the index, so the hook caps itself at
agent.codex_boot_bytes: a file that does not fit is replaced by one line naming its
path and size, telling the model to read it. A RESUME.md is the exception: it is cut to fit,
oldest units (topmost; headings and comments stay) first, with a "(N older lines not shown;
read <path>)" marker, and only dropped when no unit of its body would survive. A RESUME that
uses >= RESUME_WARN_FRACTION of its room is flagged warn=True in plan_context's entries.
install.sh sets additionalContextLimit
above that cap.

Silent when there is nothing to load (the start-empty case), and never fails the
session: any error exits 0 with no output and leaves a trace in <state_dir>/errors/.

Install: `install.sh --codex` registers it in hooks.json through the launcher it writes,
as `SESSION_NOTES_AGENT=codex <launcher> hook codex_session_start` (SessionStart, matcher
startup|resume|compact, additionalContextLimit 4000).
"""
import json
import os
import re
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))

# This hook is Codex-only, whatever the caller's environment says.
os.environ["SESSION_NOTES_AGENT"] = "codex"
import notes_config  # noqa: E402
import live  # noqa: E402

HEADER = ("session-notes memory for this Codex session (loaded at session start, from "
          "the Codex store; it does not update mid-session: run $refresh to re-read it).")

# A RESUME block that uses at least this share of the room the pack leaves it is "near the cliff":
# plan_context marks it warn=True and every tool (boot_budget, resume_guard) reads that mark.
RESUME_WARN_FRACTION = 0.9


def resume_near_cliff(block_chars, room):
    """True when a RESUME block of block_chars uses >= RESUME_WARN_FRACTION of its room."""
    return room > 0 and block_chars >= room * RESUME_WARN_FRACTION


_HEADING_RE = re.compile(r"#+(?:\s|$)")


def _resume_units(text):
    """[(lines, cuttable)] for a RESUME body. A heading (column-0 run of '#' followed by a space
    or the end of the line; `#113 ...` is not one) or an HTML comment (column-0 '<!--') is a
    one-line unit that is never cut, and so is a blank line outside an entry. Any other column-0
    non-blank line starts a cuttable unit that runs over the blank and indented lines after it,
    up to the next column-0 non-blank line. Indented text with no entry before it is its own
    cuttable unit. Every cuttable unit therefore holds at least one non-blank line."""
    units = []
    for ln in text.split("\n"):
        top = bool(ln) and not ln[0].isspace()
        if top and (_HEADING_RE.match(ln) or ln.startswith("<!--")):
            units.append(([ln], False))
        elif top:
            units.append(([ln], True))
        elif units and units[-1][1]:
            units[-1][0].append(ln)
        else:
            units.append(([ln], bool(ln.strip())))
    return units


def _truncate_resume(label, path, text, room):
    """(block, n_cut) for a RESUME that does not fit in `room`: the oldest (topmost) cuttable
    units are cut until the block plus one marker fits. None when no unit of body would survive
    (headings alone), so the caller falls back to the NOT-loaded pointer. Linear in the size of
    the text: unit lengths are summed once and the cut grows from the front."""
    units = _resume_units(text)
    idx = [i for i, u in enumerate(units) if u[1]]
    head = "## %s %s (%s)\n\n" % (label, os.path.basename(path), path)
    ulen = [sum(len(ln) + 1 for ln in u[0]) for u in units]  # each line plus its newline
    kept = sum(ulen)
    nonblank = [sum(1 for ln in u[0] if ln.strip()) for u in units]
    n = 0
    for k in range(1, len(idx)):  # k cut units; at least one cuttable unit must survive
        kept -= ulen[idx[k - 1]]
        n += nonblank[idx[k - 1]]
        marker = "(%d older lines not shown; read %s)" % (n, path)
        # joined lines minus the final newline; strip() can only shorten it
        if len(head) + kept + len(marker) + 1 - 1 <= room:
            cutset = set(idx[:k])
            lines = []
            for i, (ul, _c) in enumerate(units):
                if i == idx[0]:
                    lines.append(marker)
                if i not in cutset:
                    lines.extend(ul)
            block = head + "\n".join(lines).strip()
            if len(block) <= room:
                return block, n
    return None


def _later_footprint(files, start, used, cap):
    """Chars the files after position `start` take in the pack when each loads whole where it
    fits under `cap` and becomes a NOT-loaded pointer where it does not, packed in order after
    `used`. A truncated RESUME reserves this much so later files load as they would have."""
    u = used
    for label, path, text, problem in files[start:]:
        if problem:
            continue
        block = "## %s %s (%s)\n\n%s" % (label, os.path.basename(path), path, text)
        u += len(block) + 2 if u + len(block) + 2 <= cap else len(_pointer(label, path, text)) + 2
    return u - used


def _pointer(label, path, text):
    return ("## %s %s (%s): %d bytes, over the boot budget, NOT loaded. "
            "Read it before relying on memory." % (label, os.path.basename(path),
                                                    path, len(text.encode("utf-8"))))


def _read(path):
    return _read_checked(path)[0]


def _read_checked(path):
    """(text, problem): problem is None for a file that is absent or read fine, else why an
    existing file could not be read or decoded (the text is then "")."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip(), None
    except FileNotFoundError:
        return "", None
    except (OSError, UnicodeDecodeError) as exc:
        return "", "%s: cannot be read or decoded" % type(exc).__name__


def plan_context(cwd, cfg=None):
    """(text, entries) for cwd. text is the additionalContext (what build_context returns);
    entries is one dict per candidate file in the hook's own order: path, label, chars
    (len of the text, the unit the cap counts), bytes, status ("loaded", "truncated" for a
    RESUME with older entries cut, or "dropped"), reason; a loaded or truncated RESUME entry
    also carries block_chars, room and warn (a dropped one does not). A store file that exists but cannot be read or decoded is "dropped". The live
    block is the last entry: "loaded", "truncated" when older entries were cut, or "dropped"
    when entries exist but none fit. boot_budget.py reads this to report what a
    Codex session loads; the packing lives only here."""
    cfg = cfg or notes_config.load()
    stores = notes_config.stores_for(cwd, cfg)
    cap = cfg["agent"]["codex_boot_bytes"]
    files = []  # (label, path, text, problem) for every store file that exists and is not blank
    for label, store in (("global", stores["global"]), ("project", stores["project"])):
        if not store:
            continue
        for name in ("MEMORY.md", "RESUME.md"):
            path = os.path.join(store, name)
            text, problem = _read_checked(path)
            if text or problem:
                files.append((label, path, text, problem))
    packable = [f for f in files if f[2]]
    parts = []
    entries = []
    if packable:
        parts.append(HEADER)
        if stores["project"]:
            parts.append("Project store: %s (root %s)" % (stores["project"], stores["root"]))
    used = sum(len(p) + 2 for p in parts)
    packed = []  # "label BASENAME" of each file already in the pack, for the reason text
    for pos, (label, path, text, problem) in enumerate(files):
        if problem:  # exists but unreadable: never loaded, so say so (the printed text is unchanged)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            entries.append({"path": path, "label": label, "chars": 0, "bytes": size,
                            "status": "dropped", "reason": problem})
            continue
        block = "## %s %s (%s)\n\n%s" % (label, os.path.basename(path), path, text)
        left = cap - used
        entry = {"path": path, "label": label, "chars": len(text),
                 "bytes": len(text.encode("utf-8"))}
        is_resume = os.path.basename(path) == "RESUME.md"
        room = left - 2  # what this block may use: cap minus what is packed minus the separator
        if used + len(block) + 2 <= cap:
            parts.append(block)
            used += len(block) + 2
            packed.append("%s %s" % (label, os.path.basename(path)))
            entry.update(status="loaded", reason="")
            if is_resume:
                entry.update(block_chars=len(block), room=room,
                             warn=resume_near_cliff(len(block), room))
        else:
            cut = None
            if is_resume:
                # files packed after this one keep the room they would have had: reserve their
                # footprint (a pointer for this file stands in for it in that count)
                ptr = len(_pointer(label, path, text)) + 2
                later_ptr = _later_footprint(files, pos + 1, used + ptr, cap)
                # never pack worse than the pointer plan: within the cap whenever that plan is
                limit = max(cap, used + ptr + later_ptr)
                budget = room - later_ptr
                for _ in range(8):
                    cut = _truncate_resume(label, path, text, budget)
                    if not cut:
                        break
                    # the truncated block's REAL length decides what the later files get
                    end = used + len(cut[0]) + 2
                    over = end + _later_footprint(files, pos + 1, end, cap) - limit
                    if over <= 0:
                        break
                    budget = len(cut[0]) - over  # cut further and re-check
                    cut = None
            before = ("; room already used by %s" % ", ".join(packed)) if packed and is_resume else ""
            if cut:
                tblock, ncut = cut
                parts.append(tblock)
                used += len(tblock) + 2
                packed.append("%s %s" % (label, os.path.basename(path)))
                entry.update(status="truncated",
                             reason="older entries cut to fit: %d lines, needs %d chars, "
                                    "%d left of the %d cap%s" % (ncut, len(block) + 2,
                                                                  max(left, 0), cap, before),
                             block_chars=len(tblock), room=room, warn=True)
            else:
                line = _pointer(label, path, text)
                parts.append(line)
                used += len(line) + 2
                entry.update(status="dropped",
                             reason="needs %d chars, %d left of the %d cap%s" % (
                                 len(block) + 2, max(left, 0), cap, before))
        entries.append(entry)
    try:
        block = live.render(cwd, cfg, budget=cap - used - 2)
    except Exception:
        block = ""
        _trace("live")
    if block:
        parts.append(block)
        trunc = re.search(r"\n\(\d+ older entries not shown\)$", block) is not None
        entries.append({"path": live_path(cwd, cfg), "label": "live", "chars": len(block),
                        "bytes": len(block.encode("utf-8")),
                        "status": "truncated" if trunc else "loaded",
                        "reason": "older live entries cut to fit" if trunc else ""})
    else:
        try:
            has_live = bool(live.read_entries(cwd, cfg))
        except Exception:
            has_live = False
        if has_live:
            entries.append({"path": live_path(cwd, cfg), "label": "live", "chars": 0, "bytes": 0,
                            "status": "dropped",
                            "reason": "no room left under the %d cap for even one entry" % cap})
    return "\n\n".join(parts), entries


def live_path(cwd, cfg):
    try:
        return live.scope_for(cwd, cfg)[2]
    except Exception:
        return ""


def build_context(cwd, cfg=None):
    """The additionalContext text for cwd, or "" when the stores hold nothing."""
    return plan_context(cwd, cfg)[0]


def _trace_dir():
    try:
        return os.path.join(notes_config.load()["paths"]["state_dir"], "errors")
    except Exception:
        return os.path.expanduser("~/.codex/session-notes/state/errors")


def _trace(name="main"):
    try:
        d = _trace_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "codex_session_start.txt"), "a") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()))
            fh.write("[%s] " % name + traceback.format_exc() + "\n")
    except OSError:
        pass


def main():
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}  # not Codex's payload shape: load for the process cwd
        if not isinstance(payload, dict):
            payload = {}
        cwd = payload.get("cwd") or os.getcwd()
        text = build_context(cwd)
        if text:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception:
        _trace()
    return 0


def _legacy_build_context(cwd, cfg):
    """A frozen copy of build_context as it was before plan_context existed (git 588394a). The
    selftest runs it against build_context on generated inputs: the refactor must print the
    same bytes. The equivalence holds only where no RESUME.md is truncated (RESUME truncation is a
    deliberate change; the generated inputs do not exercise one). Do not edit it to follow the hook."""
    stores = notes_config.stores_for(cwd, cfg)
    cap = cfg["agent"]["codex_boot_bytes"]
    files = []
    for label, store in (("global", stores["global"]), ("project", stores["project"])):
        if not store:
            continue
        for name in ("MEMORY.md", "RESUME.md"):
            path = os.path.join(store, name)
            text = _read(path)
            if text:
                files.append((label, path, text))
    parts = []
    if files:
        parts.append(HEADER)
        if stores["project"]:
            parts.append("Project store: %s (root %s)" % (stores["project"], stores["root"]))
    used = sum(len(p) + 2 for p in parts)
    for label, path, text in files:
        block = "## %s %s (%s)\n\n%s" % (label, os.path.basename(path), path, text)
        if used + len(block) + 2 <= cap:
            parts.append(block)
            used += len(block) + 2
        else:
            line = ("## %s %s (%s): %d bytes, over the boot budget, NOT loaded. "
                    "Read it before relying on memory." % (label, os.path.basename(path),
                                                            path, len(text.encode("utf-8"))))
            parts.append(line)
            used += len(line) + 2
    try:
        block = live.render(cwd, cfg, budget=cap - used - 2)
    except Exception:
        block = ""
    if block:
        parts.append(block)
    return "\n\n".join(parts)


def selftest():
    import subprocess
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        cfgf = os.path.join(d, "c.toml")
        with open(cfgf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                     '[live]\npath = "%s/live/live-context.md"\n'
                     '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = 2000\n'
                     '[[scopes]]\nname = "work-client"\nroot = "%s/client"\nlive = "none"\n'
                     '[[scopes]]\nname = "outer"\nroot = "%s/work"\n'
                     '[[scopes]]\nname = "inner-none"\nroot = "%s/work/client"\nlive = "none"\n'
                     % (d, d, d, d, d, d, d))
        cfg = notes_config.load(cfgf)
        repo = os.path.join(d, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        check("empty store: no context", build_context(repo, cfg) == "")

        g = cfg["paths"]["global_store"]
        p = notes_config.codex_project_store(repo, cfg["agent"]["codex_home"])
        os.makedirs(g)
        os.makedirs(p)
        with open(os.path.join(g, "MEMORY.md"), "w") as fh:
            fh.write("# Memory Index\n\n- [Prefers tabs](user_tabs.md) - tabs over spaces\n")
        with open(os.path.join(p, "RESUME.md"), "w") as fh:
            fh.write("# RESUME\n\n- parser fix shipped, tests green\n")
        text = build_context(repo, cfg)
        check("global index loaded", "Prefers tabs" in text)
        check("project RESUME loaded", "parser fix shipped" in text)
        check("names the project store", p in text)
        check("never a Claude path", os.path.join(d, "claude") not in text)

        with open(os.path.join(g, "RESUME.md"), "w") as fh:
            fh.write("x" * 5000)
        text = build_context(repo, cfg)
        check("over-budget file becomes a pointer", "over the boot budget, NOT loaded" in text
              and "xxxxx" not in text)
        check("stays under the cap", len(text) <= 2000 + 400)
        check("later files still load after a pointer", "parser fix shipped" in text)

        # live context rides along inside the cap, after the memory files
        check("no live file: no live block", "Live context" not in text)
        live.add(repo, "merged PR #3: parser fix", cfg)
        text = build_context(repo, cfg)
        check("live line present when there is room", "merged PR #3: parser fix" in text
              and text.index("parser fix shipped") < text.index("Live context"))
        for i in range(30):
            live.add(repo, "event number %d with some padding text to take room" % i, cfg)
        text = build_context(repo, cfg)
        check("large live file: total stays under the cap", len(text) <= 2000)
        check("large live file: newest kept, older counted",
              "event number 29 " in text and "older entries not shown" in text)
        check("memory files keep priority over live", "parser fix shipped" in text)
        client = os.path.join(d, "client")
        os.makedirs(client)
        ctext = build_context(client, cfg)
        check("isolated scope: nothing from the shared file",
              "event number" not in ctext and "Live context" not in ctext)
        gitc = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(gitc + ["init", "-q", client], check=True)
        subprocess.run(gitc + ["-C", client, "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        cwt = os.path.join(d, ".client-worktrees", "feat")
        subprocess.run(gitc + ["-C", client, "worktree", "add", "-q", "-b", "feat", cwt], check=True)
        # repro A: a none-scope repo nested in a shared scope, worktree under the OUTER root
        inner = os.path.join(d, "work", "client")
        os.makedirs(inner)
        subprocess.run(gitc + ["init", "-q", inner], check=True)
        subprocess.run(gitc + ["-C", inner, "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        wa = os.path.join(d, "work", ".client-worktrees", "feat")
        subprocess.run(gitc + ["-C", inner, "worktree", "add", "-q", "-b", "feat", wa], check=True)
        atext = build_context(wa, cfg)
        check("worktree under an enclosing shared scope's root: nothing from the shared file",
              "event number" not in atext and "Live context" not in atext)
        wtext = build_context(cwt, cfg)
        check("isolated scope's worktree: nothing from the shared file",
              "event number" not in wtext and "Live context" not in wtext)
    # Differential: the pre-refactor packing vs plan_context on generated inputs (different caps,
    # path lengths, file sizes, live entries). Nothing here depends on the temp dir or the TZ.
    import random
    rnd = random.Random(104)
    diffs = 0
    cases = 0
    for depth in (0, 3, 9):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            base = os.path.join(d, *["nest%d" % i for i in range(depth)])
            os.makedirs(base, exist_ok=True)
            cf = os.path.join(d, "c.toml")
            for cap in (1, 120, 400, 900, 2000, 12000):
                with open(cf, "w") as fh:
                    fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                             '[live]\npath = "%s/live/l-%d.md"\n'
                             '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = %d\n'
                             % (d, d, d, cap, d, cap))
                gcfg = notes_config.load(cf)
                grepo = os.path.join(base, "repo%d" % cap)
                os.makedirs(grepo, exist_ok=True)
                subprocess.run(["git", "init", "-q", grepo], check=True)
                gg = gcfg["paths"]["global_store"]
                gp = notes_config.codex_project_store(grepo, gcfg["agent"]["codex_home"])
                os.makedirs(gg, exist_ok=True)
                os.makedirs(gp, exist_ok=True)
                for round_ in range(6):
                    for st in (gg, gp):
                        for name in ("MEMORY.md", "RESUME.md"):
                            kind = rnd.choice(("none", "blank", "small", "big", "multibyte", "bad"))
                            f = os.path.join(st, name)
                            if kind == "none":
                                if os.path.exists(f):
                                    os.remove(f)
                                continue
                            body = {"blank": b"  \n", "small": b"# t\n- a line\n",
                                    "big": b"x" * rnd.randint(50, 3000),
                                    "multibyte": "caf\u00e9 \u4e2d\u6587 ".encode() * rnd.randint(1, 120),
                                    "bad": b"\xff\xfe bad"}[kind]
                            with open(f, "wb") as fh:
                                fh.write(body)
                    for i in range(rnd.randint(0, 12)):
                        live.add(grepo, "event %d %s" % (i, "pad" * rnd.randint(1, 30)), gcfg)
                    cases += 1
                    if build_context(grepo, gcfg) != _legacy_build_context(grepo, gcfg):
                        diffs += 1
    check("plan_context prints exactly what the pre-refactor packing printed (%d generated cases)"
          % cases, diffs == 0 and cases == 108)
    # Boundary cases: size the LAST store file, then the live entry, so the output lands exactly on
    # the cap and one and two chars either side; each must still equal the pre-refactor packing.
    bdiffs, bcases, bedges = 0, 0, 0
    for cap in (1500, 2000):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            cf = os.path.join(d, "c.toml")
            with open(cf, "w") as fh:
                fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                         '[live]\npath = "%s/live/b.md"\n'
                         '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = %d\n' % (d, d, d, d, cap))
            bcfg = notes_config.load(cf)
            brepo = os.path.join(d, "repo")
            os.makedirs(brepo)
            subprocess.run(["git", "init", "-q", brepo], check=True)
            bg = bcfg["paths"]["global_store"]
            bp = notes_config.codex_project_store(brepo, bcfg["agent"]["codex_home"])
            os.makedirs(bg)
            os.makedirs(bp)
            with open(os.path.join(bg, "MEMORY.md"), "w") as fh:
                fh.write("# Memory Index\n\n- [x](x.md) - x\n")
            last = os.path.join(bp, "RESUME.md")
            livef = live_path(brepo, bcfg)

            def edge(setter, mark, hi):
                """Largest n for which the legacy output still holds mark*n; then n-2..n+2 vs legacy."""
                lo = 0
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    setter(mid)
                    if mark * mid in _legacy_build_context(brepo, bcfg):
                        lo = mid
                    else:
                        hi = mid - 1
                got = []
                for n in range(max(lo - 2, 1), lo + 3):
                    setter(n)
                    got.append(build_context(brepo, bcfg) == _legacy_build_context(brepo, bcfg))
                return lo, got

            def set_last(n):
                with open(last, "w") as fh:
                    fh.write("y" * n)

            n0, got = edge(set_last, "y", cap)
            bcases += len(got)
            bdiffs += got.count(False)
            bedges += 1 if 0 < n0 < cap else 0
            set_last(max(n0 - 330, 1))  # leave room for the live header plus an entry (entries cap at 240)
            check("boundary: the last store file fits at some size under cap %d" % cap, 0 < n0 < cap)

            def set_live(n):
                if os.path.exists(livef):
                    os.remove(livef)
                live.add(brepo, "z" * n, bcfg)

            m0, got = edge(set_live, "z", 240)
            bcases += len(got)
            bdiffs += got.count(False)
            bedges += 1 if 0 < m0 < 240 else 0
            check("boundary: the live entry fits at some size under cap %d" % cap, 0 < m0 < 240)
    check("plan_context matches the pre-refactor packing at, and 1 and 2 chars either side of, "
          "the cap (%d boundary cases)" % bcases, bdiffs == 0 and bcases == 20 and bedges == 4)
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        cf = os.path.join(d, "c.toml")
        with open(cf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                     '[live]\npath = "%s/live/l.md"\n'
                     '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = 2000\n' % (d, d, d, d))
        gcfg = notes_config.load(cf)
        grepo = os.path.join(d, "repo")
        os.makedirs(grepo)
        subprocess.run(["git", "init", "-q", grepo], check=True)
        gg = gcfg["paths"]["global_store"]
        gp = notes_config.codex_project_store(grepo, gcfg["agent"]["codex_home"])
        os.makedirs(gg)
        os.makedirs(gp)
        for path, body in ((os.path.join(gg, "MEMORY.md"), "# Memory Index\n\n- [x](x.md) - x\n"),
                           (os.path.join(gg, "RESUME.md"), "x" * 5000),
                           (os.path.join(gp, "RESUME.md"), "# RESUME\n\n- parser fix shipped\n")):
            with open(path, "w") as fh:
                fh.write(body)
        for i in range(30):
            live.add(grepo, "event number %d with padding text to take room" % i, gcfg)
        ptext, ents = plan_context(grepo, gcfg)
        check("plan_context entries: hook order, dropped file named, live last, text matches",
              [(os.path.basename(e["path"]), e["status"]) for e in ents][:3]
              == [("MEMORY.md", "loaded"), ("RESUME.md", "dropped"), ("RESUME.md", "loaded")]
              and ents[-1]["label"] == "live" and ents[-1]["status"] == "truncated"
              and ptext == build_context(grepo, gcfg))
        drop = ents[1]
        need = int(re.search(r"needs (\d+) chars", drop["reason"]).group(1))
        left = int(re.search(r"(\d+) left", drop["reason"]).group(1))
        first = "## global MEMORY.md (%s)\n\n%s" % (ents[0]["path"], "# Memory Index\n\n- [x](x.md) - x")
        used_before = (len(HEADER) + 2 + len("Project store: %s (root %s)" % (gp, grepo)) + 2
                       + len(first) + 2)
        check("dropped reason: 'left' is cap minus what is already packed (no off-by-2)",
              left == 2000 - used_before and need > left)
        # N2: a store file that exists but cannot be decoded is reported, and the text is unchanged
        with open(os.path.join(gg, "RESUME.md"), "wb") as fh:
            fh.write(b"\xff\xfe bad")
        btext, bents = plan_context(grepo, gcfg)
        check("undecodable store file: reported dropped with a reason, text unchanged",
              any(e["path"].endswith("global/RESUME.md") and e["status"] == "dropped"
                  and "cannot be read" in e["reason"] for e in bents)
              and btext == _legacy_build_context(grepo, gcfg))
        # N3: a live entry quoting the marker is not a truncation
        os.remove(os.path.join(gg, "RESUME.md"))
        big = dict(gcfg)
        big["agent"] = dict(gcfg["agent"], codex_boot_bytes=12000)
        live.add(grepo, "talks about (3 older entries not shown) in its text", big)
        _t, lents = plan_context(grepo, big)
        check("live entry containing the marker text is not read as truncation",
              lents[-1]["label"] == "live" and lents[-1]["status"] == "loaded")
    # RESUME truncation (issue #124): a RESUME that does not fit loses its OLDEST units, not itself
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        cf = os.path.join(d, "c.toml")

        def tcfg(cap):
            with open(cf, "w") as fh:
                fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                         '[live]\npath = "%s/live/l.md"\n'
                         '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = %d\n' % (d, d, d, d, cap))
            return notes_config.load(cf)
        tc = tcfg(1000)
        trepo = os.path.join(d, "repo")
        os.makedirs(trepo)
        subprocess.run(["git", "init", "-q", trepo], check=True)
        tp = notes_config.codex_project_store(trepo, tc["agent"]["codex_home"])
        os.makedirs(tp)
        rpath = os.path.join(tp, "RESUME.md")
        assert os.path.realpath(rpath).startswith(d), "selftest path escaped the sandbox"
        with open(rpath, "w") as fh:
            fh.write("x")
        _t0, e00 = plan_context(trepo, tc)
        room00 = [e for e in e00 if e["path"] == rpath][0]["room"]
        tcap = 1000 + 900 - room00  # the room for the RESUME is 900 whatever the sandbox path length
        tc = tcfg(tcap)
        old_lines = ["- old entry %02d with some padding text" % i for i in range(40)]
        body = ("# RESUME\n\n2026-11-22 (newest entry below)\nplain paragraph text line\n\n"
                + "\n".join(old_lines) + "\n- newest entry\n  with a continuation line\n")
        with open(rpath, "w") as fh:
            fh.write(body)
        ttext, tents = plan_context(trepo, tc)
        rent = [e for e in tents if e["path"] == rpath][0]
        kept_nonblank = [ln for ln in ttext.split("\n") if ln.strip()]
        cut_n = sum(1 for ln in body.split("\n") if ln.strip() and ln not in kept_nonblank)
        mk = re.search(r"\((\d+) older lines not shown; read (.+)\)", ttext)
        check("truncated RESUME: status truncated, reason names lines and cap",
              rent["status"] == "truncated" and "older entries cut to fit" in rent["reason"]
              and "of the %d cap" % tcap in rent["reason"])
        check("truncated RESUME: heading and newest unit kept, oldest cut",
              "# RESUME" in ttext and "- newest entry\n  with a continuation line" in ttext
              and "old entry 00" not in ttext)
        check("truncated RESUME: marker N is the count of non-blank lines cut, with the path",
              mk is not None and int(mk.group(1)) == cut_n and mk.group(2) == rpath)
        check("truncated RESUME: a plain paragraph (not a bullet) is a cuttable unit",
              "2026-11-22 (newest entry below)" not in ttext and "plain paragraph text line" not in ttext)
        check("truncated RESUME: pack stays under the cap", len(ttext) <= tcap)
        check("truncated RESUME: warn flag set", rent.get("warn") is True)
        # the heading-and-marker-only case falls back to the pointer
        with open(rpath, "w") as fh:
            fh.write("# H\n" + "x" * 3000)
        _t, fents = plan_context(trepo, tc)
        fe = [e for e in fents if e["path"] == rpath][0]
        check("one huge unit (nothing of the body survives): dropped, not truncated",
              fe["status"] == "dropped" and "NOT loaded" in _t)
        with open(rpath, "w") as fh:
            fh.write("# " + "h" * 3000 + "\n- a\n- b\n")
        _t, fents = plan_context(trepo, tc)
        fe = [e for e in fents if e["path"] == rpath][0]
        check("headings alone do not fit: dropped pointer", fe["status"] == "dropped")
        # MEMORY.md keeps the drop behavior
        os.remove(rpath)
        with open(os.path.join(tp, "MEMORY.md"), "w") as fh:
            fh.write("# idx\n" + "\n".join("- [t%d](t%d.md) - hook line" % (i, i) for i in range(80)))
        _t, ments = plan_context(trepo, tc)
        me = [e for e in ments if e["label"] == "project" and e["path"].endswith("MEMORY.md")][0]
        check("an over-budget MEMORY.md is still dropped whole", me["status"] == "dropped"
              and "older lines not shown" not in _t)
        os.remove(os.path.join(tp, "MEMORY.md"))
        # 90% boundary: block of exactly 900 chars is warned at room 1000, not at room 1001
        with open(rpath, "w") as fh:
            fh.write("x")
        _t, pe = plan_context(trepo, tc)
        e0 = [e for e in pe if e["path"] == rpath][0]
        L0, room0 = e0["block_chars"], e0["room"]
        with open(rpath, "w") as fh:
            fh.write("x" * (900 - L0 + 1))
        got = {}
        for room_want in (1000, 1001, 999):
            tc2 = tcfg(tcap + room_want - room0)  # room moves 1:1 with the cap
            _t, pe = plan_context(trepo, tc2)
            e1 = [e for e in pe if e["path"] == rpath][0]
            got[room_want] = (e1["block_chars"], e1["room"], e1["status"], e1["warn"])
        check("warn at exactly 90% of the room, not below (900 chars: room 1000 warns, 1001 does not)",
              got[1000] == (900, 1000, "loaded", True) and got[1001][3] is False
              and got[999][3] is True)
        check("RESUME_WARN_FRACTION is 0.9 and resume_near_cliff reads it",
              RESUME_WARN_FRACTION == 0.9 and resume_near_cliff(90, 100) and not resume_near_cliff(89, 100))
    # Two stores (issue #124 review): a truncated global RESUME must not starve the project files
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        cf = os.path.join(d, "c.toml")
        with open(cf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                     '[live]\npath = "%s/live/l.md"\n'
                     '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = 3000\n' % (d, d, d, d))
        tc = notes_config.load(cf)
        trepo = os.path.join(d, "repo")
        os.makedirs(trepo)
        subprocess.run(["git", "init", "-q", trepo], check=True)
        gg = tc["paths"]["global_store"]
        gp = notes_config.codex_project_store(trepo, tc["agent"]["codex_home"])
        assert os.path.realpath(gg).startswith(d) and os.path.realpath(gp).startswith(d)
        os.makedirs(gg)
        os.makedirs(gp)

        def wr(path, text):
            with open(path, "w") as fh:
                fh.write(text)
        wr(os.path.join(gg, "MEMORY.md"), "# Memory Index\n" + "".join(
            "- [g%02d](g%02d.md) - global hook line\n" % (i, i) for i in range(12)))
        wr(os.path.join(gg, "RESUME.md"), "# RESUME\n" + "".join(
            "- global entry %03d with padding text to take room\n" % i for i in range(80)))
        wr(os.path.join(gp, "MEMORY.md"), "# Project Memory\n" + "".join(
            "- [p%02d](p%02d.md) - project hook line\n" % (i, i) for i in range(10)))
        small = "# RESUME\n" + "".join("- project entry %d here\n" % i for i in range(8))
        wr(os.path.join(gp, "RESUME.md"), small)
        t2, e2 = plan_context(trepo, tc)
        st = {(e["label"], os.path.basename(e["path"])): e["status"] for e in e2}
        check("two stores: global RESUME truncated, later project files still loaded",
              st[("global", "RESUME.md")] == "truncated" and st[("project", "MEMORY.md")] == "loaded"
              and st[("project", "RESUME.md")] == "loaded" and "project entry 7 here" in t2)
        check("two stores: whole pack (pointers included) stays within the cap", len(t2) <= 3000)
        saved = {k: os.environ.get(k) for k in ("SESSION_NOTES_CONFIG", "HOME")}
        os.environ["SESSION_NOTES_CONFIG"] = cf
        try:
            import resume_guard
            prp = os.path.join(gp, "RESUME.md")
            more = small + "- one more project entry\n"
            check("two stores: appending a line to the small project RESUME is not refused",
                  resume_guard.codex_boot_drop(prp, more, tc, trepo) is None)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    # unit rules: `#113 ...` is an entry, not a heading; a blank line is never the surviving body
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        cf = os.path.join(d, "c.toml")
        with open(cf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\n'
                     '[live]\npath = "%s/live/l.md"\n'
                     '[agent]\ncodex_home = "%s/codex"\ncodex_boot_bytes = 1000\n' % (d, d, d, d))
        tc = notes_config.load(cf)
        trepo = os.path.join(d, "repo")
        os.makedirs(trepo)
        subprocess.run(["git", "init", "-q", trepo], check=True)
        gp = notes_config.codex_project_store(trepo, tc["agent"]["codex_home"])
        os.makedirs(gp)
        rpath = os.path.join(gp, "RESUME.md")
        assert os.path.realpath(rpath).startswith(d)
        with open(rpath, "w") as fh:
            fh.write("x")
        _t0, e00 = plan_context(trepo, tc)
        tc = notes_config.load(cf)  # reload below with a cap that leaves 900 chars of room
        room00 = [e for e in e00 if e["path"] == rpath][0]["room"]
        with open(cf) as fh:
            ctxt = fh.read().replace("codex_boot_bytes = 1000", "codex_boot_bytes = %d" % (1000 + 900 - room00))
        with open(cf, "w") as fh:
            fh.write(ctxt)
        tc = notes_config.load(cf)
        with open(rpath, "w") as fh:
            fh.write("# RESUME\n\n" + "".join("#%d MERGED some padding text for this entry\n" % i
                                              for i in range(100, 130)) + "- newest\n")
        t3, e3 = plan_context(trepo, tc)
        check("`#113 ...` style lines are cuttable entries, not headings",
              "#100 MERGED" not in t3 and "- newest" in t3 and "# RESUME" in t3
              and [e for e in e3 if e["path"] == rpath][0]["status"] == "truncated")
        check("unit rules: a '#' run then a space is a heading, '#113' is not, '<!--' is kept",
              [u[1] for u in _resume_units("# a\n## b\n#\n#113 x\n<!-- c -->\n- e\n\n  more")]
              == [False, False, False, True, False, True])
        with open(rpath, "w") as fh:
            fh.write("# H\n\n- " + "x" * 3000 + "\n")
        t3, e3 = plan_context(trepo, tc)
        check("a heading, a blank line and one huge entry: dropped, not a blank-bodied 'truncated'",
              [e for e in e3 if e["path"] == rpath][0]["status"] == "dropped")
        # linear time: a ~1.3M-char RESUME packs fast
        with open(rpath, "w") as fh:
            fh.write("# RESUME\n" + "".join("- entry %07d padding padding padding padding\n" % i
                                            for i in range(28000)))
        big = os.path.getsize(rpath)
        t0 = time.time()
        t3, e3 = plan_context(trepo, tc)
        took = time.time() - t0
        check("a %d-char RESUME packs in well under 1s (%.2fs), newest kept" % (big, took),
              big > 1300000 and took < 1.0 and "entry 0027999" in t3
              and [e for e in e3 if e["path"] == rpath][0]["status"] == "truncated")
    # Follow-up nits on #125: short mocked paths, so a truncated block can come out shorter than
    # the pointer it replaces; the pack must still stay under the cap and name what used the room
    real_stores, real_read = notes_config.stores_for, _read_checked
    texts = {"/g/MEMORY.md": "# m\n" + "\n".join("- [t%d](t%d.md) hook" % (i, i) for i in range(6)),
             "/g/RESUME.md": "# R\n" + "\n".join("- g entry %03d padding" % i for i in range(40)),
             "/p/MEMORY.md": "# pm\n- [a](a.md) hook",
             "/p/RESUME.md": "# R\n" + "\n".join("- p entry %03d padding" % i for i in range(40))}
    import shutil
    tdir = tempfile.mkdtemp()  # the mock's trace dir and live file: nothing may reach the real HOME
    mcfg = {"agent": {"codex_boot_bytes": 0}, "scopes": [], "live": {"path": os.path.join(tdir, "live.md")}}
    real_tdir = _trace_dir
    notes_config.stores_for = lambda cwd, cfg: {"global": "/g", "project": "/p", "root": "/r"}
    globals()["_read_checked"] = lambda path: (texts.get(path, ""), None)
    globals()["_trace_dir"] = lambda: os.path.join(tdir, "errors")
    try:
        mcfg["agent"]["codex_boot_bytes"] = 1
        floor = len(plan_context("/r", mcfg)[0])  # every file a pointer: the smallest pack
        over, saw_reason = [], False
        for cap in range(floor, 2600):
            mcfg["agent"]["codex_boot_bytes"] = cap
            mt, me = plan_context("/r", mcfg)
            if len(mt) > cap and any(e["status"] == "truncated" and e["label"] != "live" for e in me):
                over.append(cap)
            for e in me:
                if e["label"] == "project" and e["path"] == "/p/RESUME.md" and e["status"] != "loaded" \
                        and "global RESUME.md" in e["reason"]:
                    saw_reason = True
        check("mocked short paths: a pack with a truncated RESUME stays under the cap at every cap above the pointer floor",
              not over)
        check("a dropped or truncated project RESUME names the earlier file that used the room",
              saw_reason)
        # the reviewer's trigger: the truncated global block comes out SHORTER than its pointer, so
        # a reserve that counted a pointer let the project MEMORY load whole and pushed the
        # project RESUME out, over the cap. The later reserve now follows the block's real length.
        texts.pop("/g/MEMORY.md")
        texts.update({"/g/RESUME.md": "# R\n" + "\n".join("- g%03d" % i for i in range(70)),
                      "/p/MEMORY.md": "m" * 158, "/p/RESUME.md": "r" * 40})
        mcfg["agent"]["codex_boot_bytes"] = 495
        mt, me = plan_context("/r", mcfg)
        st = {(e["label"], os.path.basename(e["path"])): e["status"] for e in me if e["label"] != "live"}
        check("truncated block shorter than its pointer: pack within the cap and project RESUME not dropped",
              len(mt) <= 495 and st[("project", "RESUME.md")] != "dropped")
        # seeded fuzz over short-path layouts: never worse than the whole-or-pointer plan, and a
        # file that loads whole when its earlier RESUME is a pointer is never dropped
        import random
        rnd = random.Random(126)
        bad, n_trunc = [], 0
        for _ in range(3000):
            ls = [rnd.randint(1, 12) for _ in range(rnd.randint(3, 60))]
            texts.clear()
            texts["/g/RESUME.md"] = "# R\n" + "\n".join("-" + "x" * k for k in ls)
            texts["/p/MEMORY.md"] = "m" * rnd.randint(60, 320)
            texts["/p/RESUME.md"] = "r" * rnd.randint(10, 120)
            if rnd.random() < .5:
                texts["/g/MEMORY.md"] = "g" * rnd.randint(10, 100)
            cap = rnd.randint(250, 700)
            mcfg["agent"]["codex_boot_bytes"] = cap
            mt, me = plan_context("/r", mcfg)
            mine = {e["path"]: e for e in me if e["label"] != "live"}
            n_trunc += any(e["status"] == "truncated" for e in mine.values())
            # base plan: every file whole where it fits, else a pointer
            u = len(HEADER) + 2 + len("Project store: /p (root /r)") + 2
            plan = {}
            for pth in ("/g/MEMORY.md", "/g/RESUME.md", "/p/MEMORY.md", "/p/RESUME.md"):
                if pth not in texts:
                    continue
                lab = "global" if pth.startswith("/g") else "project"
                blk = "## %s %s (%s)\n\n%s" % (lab, os.path.basename(pth), pth, texts[pth])
                if u + len(blk) + 2 <= cap:
                    plan[pth] = "loaded"
                    u += len(blk) + 2
                else:
                    plan[pth] = "dropped"
                    u += len(_pointer(lab, pth, texts[pth])) + 2
            if u <= cap and len(mt) > cap:
                bad.append(("over", cap, len(mt)))
            if any(st == "loaded" and mine[pth]["status"] == "dropped" for pth, st in plan.items()):
                bad.append(("lost", cap))
        check("fuzz: %d truncated layouts; never over a cap the base plan fits, never loses a file the "
              "base plan loads %s" % (n_trunc, bad[:3]), not bad and n_trunc > 100)
        check("the mocked block wrote no trace file, real or temp",
              not os.path.exists(os.path.join(tdir, "errors")))
    finally:
        globals()["_trace_dir"] = real_tdir
        shutil.rmtree(tdir, ignore_errors=True)
        notes_config.stores_for = real_stores
        globals()["_read_checked"] = real_read
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
