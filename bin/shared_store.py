#!/usr/bin/env python3
"""shared_store.py -- one memory store per scope, shared by every agent (session-notes #88).

A scope that sets `shared_store = true` in its [[scopes]] entry keeps ONE store,
<paths.shared_stores_dir>/<slug> (default ~/.local/share/session-notes/stores/<slug>), for both
Claude Code and Codex: RESUME.md, MEMORY.md and the memory files. notes_config.store_for() is the
one resolver; every reader and writer reaches it through the resolved scope's `store`. This
tool moves an existing scope into it and prints the one line Claude needs:

  shared_store.py plan --scope NAME [--label FILE=claude|codex ...]
      Dry run: what would move, and whether every original entry would land exactly once.
      Writes nothing, anywhere.
  shared_store.py migrate --scope NAME [--label ...]
      Refuses unless the scope is opted in. Merges the Claude store and the Codex store into the
      shared store, checks (re-reading the files it wrote) that every original entry landed
      exactly once, and exits 1 (RED, nothing left in place) if one did not. Writes only under
      the shared stores root; the old stores are NOT touched (deleting them is the user's call).
  shared_store.py settings-line --scope NAME
      The `autoMemoryDirectory` line to add to <scope root>/.claude/settings.local.json. Claude
      Code honors it only from there (not from the checked-in settings.json), so this tool prints
      it and never writes a settings file.

What "entry" means:
  * MEMORY.md, REFERENCE.md, RESUME.md, RESUME-archive.md: each non-blank line. The Claude file
    is kept verbatim; a Codex line that is not already present (counted, not just set-wise) is
    appended after it (RESUME files get a marker line first). The check compares line multisets.
  * every other top-level *.md: one memory file. Same name in both stores with identical text
    lands once. Same name with DIFFERENT text is two harness-specific versions: the Claude copy
    keeps its name with `harness: claude`, the Codex copy lands as <stem>.codex.md with
    `harness: codex`, and the index lines are rewritten to match and marked `[claude]`/`[codex]`.
    `--label FILE=claude|codex` marks a one-sided file the same way. Unlabeled = applies to all.
  * anything else (a subfolder, a non-.md file): reported as NOT MIGRATED and the run is RED.
    Dotfiles are ignored.

The harness label is `harness: claude|codex` in a memory file's frontmatter (the index line says
`[codex]` in front of its hook). Every harness reads every file; the label tells a reader to skip
what does not apply, it hides nothing (Claude's native load cannot filter).

    python3 shared_store.py --selftest
"""
import argparse
import collections
import os
import re
import shutil
import sys

BIN = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, BIN)
import notes_config  # noqa: E402

HARNESSES = ("claude", "codex")
LINE_FILES = ("MEMORY.md", "REFERENCE.md", "RESUME.md", "RESUME-archive.md")
MARKER = "<!-- merged from the Codex store by shared_store.py -->"
MARKED_FILES = ("RESUME.md", "RESUME-archive.md")
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_INDEX_RE = re.compile(r"^(- \[[^\]]*\]\([^)]*\) - )(.*)$")


class MigrationError(Exception):
    pass


# ---------------------------------------------------------------- label helpers (shared)
def with_harness(content, harness):
    """Stamp `harness: <name>` into a memory file's frontmatter (replacing a stale label).
    No harness = unchanged. The one place frontmatter labels are written."""
    if not harness:
        return content
    lines = content.split("\n")
    if lines and lines[0].strip() == "---":
        try:
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        except StopIteration:
            raise ValueError("frontmatter is not closed (no second ---)") from None
        head = [ln for ln in lines[1:end] if not ln.startswith("harness:")]
        return "\n".join(["---"] + head + ["harness: %s" % harness] + lines[end:])
    return "---\nharness: %s\n---\n%s" % (harness, content)


def harness_of(content):
    """The `harness:` label in a memory file's frontmatter, or None (applies to all)."""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for ln in lines[1:]:
        if ln.strip() == "---":
            return None
        if ln.startswith("harness:"):
            v = ln.split(":", 1)[1].strip()
            return v if v in HARNESSES else None
    return None


def label_index_line(line, harness):
    """`- [T](f.md) - hook` -> `- [T](f.md) - [codex] hook`; unchanged for no harness or when the
    line is already marked. A line not shaped like an index line is returned unchanged."""
    m = _INDEX_RE.match(line)
    if not harness or not m or m.group(2).startswith("[%s]" % harness):
        return line
    return "%s[%s] %s" % (m.group(1), harness, m.group(2))


# ---------------------------------------------------------------- scope + stores
def find_scope(cfg, name):
    for s in cfg["scopes"]:
        if s["name"] == name:
            return s
    raise MigrationError("unknown scope %r (known: %s)" % (
        name, ", ".join(s["name"] for s in cfg["scopes"]) or "none configured"))


def legacy_stores(cfg, scope):
    """{agent: the per-harness store the scope had before it opted in}, from the one resolver."""
    plain = {"root": scope["root"], "shared_store": False}
    return {a: notes_config.store_for(plain, a, cfg) for a in notes_config.AGENTS}


def _entries(store):
    """({name: text}, [problem]) for a store's top-level files; a missing store is empty."""
    files, problems = {}, []
    if not os.path.isdir(store):
        return files, problems
    for name in sorted(os.listdir(store)):
        if name.startswith("."):
            continue
        p = os.path.join(store, name)
        if os.path.isdir(p) or not name.endswith(".md"):
            problems.append("%s: not a top-level .md file (NOT MIGRATED; move it by hand)" % p)
            continue
        with open(p, encoding="utf-8") as fh:
            files[name] = fh.read()
    return files, problems


# ---------------------------------------------------------------- the merge
def _relabel_map(files, labels):
    """{agent: {name: (new_name, harness)}} for conflicting and explicitly labeled files."""
    c, x = files["claude"], files["codex"]
    out = {"claude": {}, "codex": {}}
    for name in sorted(set(c) & set(x)):
        if name in LINE_FILES or c[name] == x[name]:
            continue
        stem = name[:-3]
        out["claude"][name] = (name, "claude")
        out["codex"][name] = ("%s.codex.md" % stem, "codex")
    for name, h in labels.items():
        if name in LINE_FILES:
            raise MigrationError("--label %s: an index or RESUME file cannot carry a label" % name)
        if name in c and name in x:
            raise MigrationError("--label %s: the file is in both stores (labeled automatically "
                                 "when the two copies differ)" % name)
        for agent in HARNESSES:
            if name in files[agent]:
                out[agent][name] = (name, h)
    return out


def _transform_line(line, agent, relabel):
    m = _LINK_RE.search(line)
    if not m or m.group(1) not in relabel[agent]:
        return line
    new, h = relabel[agent][m.group(1)]
    line = line[:m.start(1)] + new + line[m.end(1):]
    return label_index_line(line, h)


def _nonblank(text):
    return [ln for ln in text.split("\n") if ln.strip()]


def _merge_lines(name, c_text, x_text, relabel):
    """Merged text of a line file: the Claude text, then the Codex lines it lacks."""
    c_lines = [_transform_line(ln, "claude", relabel) for ln in (c_text or "").split("\n")] if c_text is not None else None
    x_lines = [_transform_line(ln, "codex", relabel) for ln in (x_text or "").split("\n")] if x_text is not None else None
    if c_lines is None:
        return "\n".join(x_lines)
    if x_lines is None:
        return "\n".join(c_lines)
    have = collections.Counter(ln for ln in c_lines if ln.strip())
    extra = []
    for ln in x_lines:
        if not ln.strip():
            continue
        if have[ln] > 0:
            have[ln] -= 1
        else:
            extra.append(ln)
    out = list(c_lines)
    if extra:
        while out and not out[-1].strip():
            out.pop()
        out.append("")
        if name in MARKED_FILES:
            out.append(MARKER)
        out.extend(extra)
        out.append("")
    return "\n".join(out)


def build(cfg, scope, labels=None):
    """(outputs {name: text}, report lines, problems [str]). Pure: reads the two legacy stores,
    writes nothing."""
    labels = labels or {}
    stores = legacy_stores(cfg, scope)
    files, problems = {}, []
    for a in HARNESSES:
        files[a], pr = _entries(stores[a])
        problems += pr
    relabel = _relabel_map(files, labels)
    for name in labels:
        if not any(name in files[a] for a in HARNESSES):
            problems.append("--label %s: no such file in either store" % name)
    outputs, report = {}, []
    for name in sorted(set(files["claude"]) | set(files["codex"])):
        c, x = files["claude"].get(name), files["codex"].get(name)
        if name in LINE_FILES:
            outputs[name] = _merge_lines(name, c, x, relabel)
            report.append("merge  %s (claude %s lines, codex %s lines)" % (
                name, len(_nonblank(c)) if c is not None else "-", len(_nonblank(x)) if x is not None else "-"))
            continue
        for agent, text in (("claude", c), ("codex", x)):
            if text is None:
                continue
            if agent == "codex" and c is not None and c == x:
                report.append("same   %s (identical in both stores: lands once)" % name)
                continue
            new, h = relabel[agent].get(name, (name, None))
            outputs[new] = with_harness(text, h)
            report.append("copy   %s -> %s%s (from %s)" % (name, new, " [harness: %s]" % h if h else "", agent))
    return outputs, report, problems


# ---------------------------------------------------------------- the check
def verify(cfg, scope, dest, labels=None):
    """Re-read `dest` and the two legacy stores. Returns [finding] (empty = every original entry
    landed exactly once). Does not use build()'s output text: it checks the files on disk."""
    labels = labels or {}
    stores = legacy_stores(cfg, scope)
    files = {a: _entries(stores[a])[0] for a in HARNESSES}
    relabel = _relabel_map(files, labels)
    got, _ = _entries(dest)
    got = {n: t for n, t in got.items()}
    bad, expected_names = [], set()
    for name in sorted(set(files["claude"]) | set(files["codex"])):
        c, x = files["claude"].get(name), files["codex"].get(name)
        if name in LINE_FILES:
            expected_names.add(name)
            want = collections.Counter()
            have = collections.Counter()
            for agent, text in (("claude", c), ("codex", x)):
                if text is None:
                    continue
                mine = collections.Counter(_transform_line(ln, agent, relabel) for ln in _nonblank(text))
                if agent == "claude":
                    have = mine
                    want = collections.Counter(mine)
                else:
                    for ln, n in mine.items():
                        want[ln] += max(0, n - have.get(ln, 0))
            out = collections.Counter(ln for ln in _nonblank(got.get(name, "")) if ln != MARKER)
            if name not in got:
                bad.append("%s: missing from the shared store" % name)
            elif out != want:
                miss = sorted((want - out).elements())[:3]
                dup = sorted((out - want).elements())[:3]
                bad.append("%s: entries differ (dropped or duplicated): missing %r, extra %r" % (name, miss, dup))
            continue
        for agent, text in (("claude", c), ("codex", x)):
            if text is None or (agent == "codex" and c is not None and c == x):
                continue
            new, h = relabel[agent].get(name, (name, None))
            expected_names.add(new)
            if new not in got:
                bad.append("%s: missing from the shared store (expected %s)" % (name, new))
                continue
            added = len(with_harness(text, h).split("\n")) - len(text.split("\n")) if h else 0
            if len(got[new].split("\n")) != len(text.split("\n")) + added:
                bad.append("%s: line count %d != source %d (+%d label)" % (
                    new, len(got[new].split("\n")), len(text.split("\n")), added))
            elif h and harness_of(got[new]) != h:
                bad.append("%s: harness label %r missing" % (new, h))
            elif not h and got[new] != text:
                bad.append("%s: text differs from its source" % new)
    for name in sorted(set(got) - expected_names):
        bad.append("%s: in the shared store but in neither source (appears from nowhere)" % name)
    return bad


# ---------------------------------------------------------------- commands
def _scope_args(cfg_path_env=None, name=None):
    cfg = notes_config.load()
    return cfg, find_scope(cfg, name)


def cmd_plan(args):
    cfg, scope = _scope_args(name=args.scope)
    labels = _parse_labels(args.label)
    outputs, report, problems = build(cfg, scope, labels)
    st = legacy_stores(cfg, scope)
    print("scope %s: %s -> %s" % (scope["name"], " + ".join("%s (%s)" % (st[a], a) for a in HARNESSES), scope["store"]))
    if not scope.get("shared_store"):
        print("NOTE: the scope is not opted in (set `shared_store = true`); migrate would refuse.")
    for ln in report:
        print("  " + ln)
    for p in problems:
        print("  PROBLEM " + p)
    r = outputs.get("RESUME.md")
    if r is not None:
        import resume_guard
        msg = resume_guard.check_write(os.path.join(scope["store"], "RESUME.md"), "", r)
        if msg:
            print("  WARN merged RESUME.md is over its budget: " + msg)
    print("plan only: nothing was written." if not problems else "RED: fix the PROBLEM lines first.")
    return 1 if problems else 0


def cmd_migrate(args):
    cfg, scope = _scope_args(name=args.scope)
    if not scope.get("shared_store"):
        print("shared_store: scope %s is not opted in; set `shared_store = true` in its [[scopes]] "
              "entry first" % scope["name"], file=sys.stderr)
        return 2
    labels = _parse_labels(args.label)
    outputs, report, problems = build(cfg, scope, labels)
    if problems:
        for p in problems:
            print("PROBLEM " + p, file=sys.stderr)
        print("RED: nothing written.", file=sys.stderr)
        return 1
    dest = scope["store"]
    root = cfg["paths"]["shared_stores_dir"]
    if not notes_config._under(dest, root):
        raise MigrationError("destination %s is outside the shared stores root %s" % (dest, root))
    if os.path.isdir(dest) and os.listdir(dest):
        have, _ = _entries(dest)
        if have != outputs:
            print("shared_store: %s already has different content; refusing to overwrite it" % dest, file=sys.stderr)
            return 2
        print("already migrated: %s matches the merge of the old stores." % dest)
        return 0
    tmp = os.path.join(root, ".migrating-%s" % os.path.basename(dest))
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        for name, text in outputs.items():
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        st = legacy_stores(cfg, scope)
        with open(os.path.join(tmp, "MIGRATED-FROM.txt"), "w", encoding="utf-8") as fh:
            fh.write("Merged by shared_store.py migrate from:\n  claude: %s\n  codex:  %s\n"
                     "Those stores were left untouched; delete them when you are satisfied.\n"
                     % (st["claude"], st["codex"]))
        bad = verify_dir(cfg, scope, tmp, labels)
        if bad:
            for b in bad:
                print("RED " + b, file=sys.stderr)
            print("RED: an original entry did not land exactly once; nothing was moved into place.", file=sys.stderr)
            return 1
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isdir(dest):
            os.rmdir(dest)  # empty (checked above)
        os.rename(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    bad = verify(cfg, scope, dest, labels)
    for ln in report:
        print("  " + ln)
    if bad:
        for b in bad:
            print("RED " + b, file=sys.stderr)
        return 1
    print("migrated %d files into %s; every original entry landed exactly once." % (len(outputs), dest))
    print("The old stores were not touched. Next: shared_store.py settings-line --scope %s" % scope["name"])
    return 0


def verify_dir(cfg, scope, directory, labels):
    """verify() against a staging directory (the MIGRATED-FROM.txt file is not an entry)."""
    return [b for b in verify(cfg, scope, directory, labels)]


def cmd_settings_line(args):
    cfg, scope = _scope_args(name=args.scope)
    if not scope.get("shared_store"):
        print("shared_store: scope %s is not opted in; nothing to point Claude at" % scope["name"], file=sys.stderr)
        return 2
    store = scope["store"]
    home = os.path.expanduser("~")
    shown = "~" + store[len(home):] if store.startswith(home.rstrip("/") + "/") else store
    print("Add to %s (this file only: Claude Code ignores the key in the checked-in settings.json):"
          % os.path.join(scope["root"], ".claude", "settings.local.json"))
    print('  "autoMemoryDirectory": "%s"' % shown)
    return 0


def _parse_labels(items):
    out = {}
    for it in items or []:
        name, _, h = it.partition("=")
        if h not in HARNESSES or not name.endswith(".md") or "/" in name:
            raise MigrationError("--label wants FILE.md=claude|codex, got %r" % it)
        out[name] = h
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    for n in ("plan", "migrate", "settings-line"):
        p = sub.add_parser(n)
        p.add_argument("--scope", required=True)
        if n != "settings-line":
            p.add_argument("--label", action="append", metavar="FILE=claude|codex")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            return cmd_plan(args)
        if args.cmd == "migrate":
            return cmd_migrate(args)
        if args.cmd == "settings-line":
            return cmd_settings_line(args)
    except (MigrationError, notes_config.ConfigError) as exc:
        print("shared_store: %s" % exc, file=sys.stderr)
        return 2
    ap.print_help()
    return 2


def selftest():
    import hashlib
    import json
    import subprocess
    import tempfile
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
        if not cond:
            fails.append(name)

    def snap(*dirs):
        """{relative path: sha} of every file under the dirs (missing dir = empty)."""
        out = {}
        for top in dirs:
            for cur, _d, fs in os.walk(top):
                for f in fs:
                    p = os.path.join(cur, f)
                    with open(p, "rb") as fh:
                        out[p] = hashlib.sha256(fh.read()).hexdigest()
        return out

    def w(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def r(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    saved = {k: os.environ.get(k) for k in ("SESSION_NOTES_CONFIG", "SESSION_NOTES_AGENT", "HOME")}
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        home = os.path.join(d, "home")
        os.makedirs(home)
        pilot, plain, iso = (os.path.join(d, n) for n in ("pilot", "plain", "iso"))
        for x in (pilot, plain, iso):
            os.makedirs(os.path.join(x, ".claude"))
        shared_root = os.path.join(d, "stores")
        cfgf = os.path.join(d, "c.toml")
        base_cfg = ('[paths]\nclaude_home = "%s/claude"\nstate_dir = "%s/state"\nglobal_store = "%s/gstore"\n'
                    'shared_stores_dir = "%s"\n[live]\npath = "%s/live.md"\n'
                    '[agent]\ncodex_home = "%s/codex"\n[budgets]\nresume_bytes = 300\n'
                    % (d, d, d, shared_root, d, d))
        scopes = ('[[scopes]]\nname = "pilot"\nroot = "%s"\nshared_store = true\n'
                  '[[scopes]]\nname = "plain"\nroot = "%s"\n' % (pilot, plain))
        w(cfgf, base_cfg + scopes)
        env = {k: v for k, v in os.environ.items() if not k.startswith("SESSION_NOTES_")}
        env.update(HOME=home, SESSION_NOTES_CONFIG=cfgf)
        os.environ["HOME"] = home
        os.environ["SESSION_NOTES_CONFIG"] = cfgf
        os.environ.pop("SESSION_NOTES_AGENT", None)

        def load(kind, path=cfgf):
            return notes_config.load(path, env={"SESSION_NOTES_AGENT": kind})

        def sub(args, stdin="", e=None, cwd=None):
            p = subprocess.run(args, input=stdin, capture_output=True, text=True, env=e or env, cwd=cwd or d)
            return p.returncode, p.stdout, p.stderr

        slug = notes_config.encode_project_dir(pilot)
        shared = os.path.join(shared_root, slug)

        # --- the resolver: one path for both agents; non-opted scopes keep today's stores
        cc, cx = load("claude"), load("codex")
        sc = {k: {s["name"]: s for s in c["scopes"]} for k, c in (("claude", cc), ("codex", cx))}
        check("opted-in scope: ONE store for both agents, under the shared root",
              sc["claude"]["pilot"]["store"] == sc["codex"]["pilot"]["store"] == shared)
        plain_slug = notes_config.encode_project_dir(plain)
        check("non-opted scope: today's per-harness stores, byte for byte the old formula",
              sc["claude"]["plain"]["store"] == os.path.join(d, "claude", "projects", plain_slug, "memory")
              and sc["codex"]["plain"]["store"] == os.path.join(d, "codex", "session-notes", "projects", plain_slug))
        check("store_for(shared, either agent) agrees with the resolved scope",
              notes_config.store_for(sc["claude"]["pilot"], "codex", cc) == shared
              and notes_config.store_for({"root": plain, "shared_store": False}, "claude", cc)
              == sc["claude"]["plain"]["store"])
        for tag, extra in (("live = none", '[[scopes]]\\nname = "x"\\nroot = "%s"\\nshared_store = true\\nlive = "none"\\n' % iso),
                           ("live = own", '[[scopes]]\\nname = "x"\\nroot = "%s"\\nshared_store = true\\nlive = "own"\\nlive_file = "%s/x-live.md"\\n' % (iso, d)),
                           ("store set", '[[scopes]]\\nname = "x"\\nroot = "%s"\\nshared_store = true\\nstore = "%s/s"\\n' % (iso, d)),
                           ("not a bool", '[[scopes]]\\nname = "x"\\nroot = "%s"\\nshared_store = "yes"\\n' % iso)):
            bad = os.path.join(d, "bad.toml")
            w(bad, base_cfg + extra.replace("\\\\n", "\\n"))
            try:
                load("claude", bad)
                got = "loaded"
            except notes_config.ConfigError as exc:
                got = str(exc)
            check("config load refuses shared_store with %s" % tag,
                  got != "loaded" and "shared_store" in got, got)

        # --- writers and readers, both directions, through the real tools
        queue = os.path.join(d, "queue")
        penv = dict(env, CODEX_THREAD_ID="thr-1")
        codex_save_py = """import sys
sys.path.insert(0, %r)
import codex_save as c
c.queue_root = lambda: %r
sys.exit(c.main(sys.argv[1:]))""" % (BIN, queue)

        def cs(args, stdin=""):
            return sub([sys.executable, "-c", codex_save_py] + args, stdin, penv, cwd=pilot)

        plain_before = snap(os.path.join(d, "claude", "projects", plain_slug), os.path.join(d, "codex", "session-notes", "projects", plain_slug))
        # Codex writes a fact; Claude's side (claude-mode store) reads it from the same files
        rc, out, err = cs(["stage", "--store", "project", "--file", "project_from_codex.md"],
                          "---\nname: from codex\ndescription: d\nmetadata:\n  type: project\n---\ncodex fact\n")
        check("codex_save stages into the shared store", rc == 0 and shared in out, (rc, out, err))
        cs(["stage", "--store", "project", "--index-line", "- [From Codex](project_from_codex.md) - learned in Codex"])
        rc, out, err = cs(["commit"])
        check("codex_save commit writes the shared store", rc == 0 and os.path.isfile(os.path.join(shared, "project_from_codex.md")), (rc, out, err))
        claude_store = sc["claude"]["pilot"]["store"]
        check("Claude's resolved store sees the Codex-written fact and index line",
              "codex fact" in r(os.path.join(claude_store, "project_from_codex.md"))
              and "learned in Codex" in r(os.path.join(claude_store, "MEMORY.md")))
        # Claude writes a fact (its writers: the Write tool and append_memory_index); Codex boot reads it
        import append_memory_index
        w(os.path.join(claude_store, "feedback_from_claude.md"), "---\nname: fc\ndescription: d\nmetadata:\n  type: feedback\n---\nclaude fact\n")
        append_memory_index.append_line(os.path.join(claude_store, "MEMORY.md"), "- [From Claude](feedback_from_claude.md) - learned in Claude")
        w(os.path.join(claude_store, "RESUME.md"), "# RESUME\n\n- claude was here\n")
        start = ("import sys\nsys.path.insert(0, %r)\nsys.path.insert(0, %r)\nimport codex_session_start as c\n"
                 "print(c.plan_context(sys.argv[1])[0])" % (os.path.join(os.path.dirname(BIN), "hooks"), BIN))
        rc, out, err = sub([sys.executable, "-c", start, pilot])
        check("Codex session start loads the Claude-written index line and RESUME",
              rc == 0 and "learned in Claude" in out and "claude was here" in out and "learned in Codex" in out, (rc, out[-300:], err))
        check("no per-harness store was created for the opted-in scope",
              not os.path.exists(os.path.join(d, "claude", "projects", slug))
              and not os.path.exists(os.path.join(d, "codex", "session-notes", "projects", slug)))
        check("non-opted scope's stores are byte-identical after both writers ran",
              plain_before == snap(os.path.join(d, "claude", "projects", plain_slug), os.path.join(d, "codex", "session-notes", "projects", plain_slug)))

        # --- harness label: present for both, marked in the index
        rc, _, err = cs(["stage", "--store", "project", "--file", "feedback_codex_quirk.md", "--harness", "codex"],
                        "---\nname: q\ndescription: d\nmetadata:\n  type: feedback\n---\ncodex-only quirk\n")
        cs(["stage", "--store", "project", "--index-line", "- [Quirk](feedback_codex_quirk.md) - a Codex tool gotcha", "--harness", "codex"])
        rc2, out2, _ = cs(["commit"])
        labeled = r(os.path.join(shared, "feedback_codex_quirk.md"))
        check("labeled file carries `harness: codex` in its frontmatter",
              rc == 0 and rc2 == 0 and harness_of(labeled) == "codex" and labeled.startswith("---\nname: q"), (rc, err, labeled))
        idx = r(os.path.join(shared, "MEMORY.md"))
        check("the index marks the harness-specific line", "- [Quirk](feedback_codex_quirk.md) - [codex] a Codex tool gotcha" in idx, idx)
        rc, out, err = sub([sys.executable, "-c", start, pilot])
        check("Codex boot shows the labeled line; Claude's resolved store has the same file",
              "[codex] a Codex tool gotcha" in out and os.path.isfile(os.path.join(claude_store, "feedback_codex_quirk.md")))
        check("--harness without a file/index line is refused",
              cs(["stage", "--store", "project", "--resume", "--harness", "codex"], "x")[0] == 2)

        # --- size guard: both writers, on the shared RESUME
        rc, base, _ = cs(["hash", "--store", "project"])
        rc, out, err = cs(["stage", "--store", "project", "--resume", "--base", base.strip()], "r" * 400)
        check("codex_save refuses an over-budget RESUME at stage time (300 budget)",
              rc == 2 and "RESUME-archive.md" in err, (rc, err))
        rc, out, err = cs(["stage", "--store", "project", "--resume", "--base", base.strip()], "# RESUME\n\n- short\n")
        rc2, out2, err2 = cs(["commit"])
        check("a write under budget passes and replaces RESUME", rc == 0 and rc2 == 0 and "- short" in r(os.path.join(shared, "RESUME.md")), (rc, err, rc2, out2))
        hook = [sys.executable, os.path.join(os.path.dirname(BIN), "hooks", "resume_size_guard.py")]
        rc, out, err = sub(hook, json.dumps({"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(shared, "RESUME.md"), "content": "r" * 400}}))
        check("the Claude hook refuses the same write on the shared RESUME", rc == 2 and "RESUME-archive.md" in err, (rc, err))
        # a staged item that went stale between stage and commit is judged again at commit
        w(os.path.join(queue, "thr-1", "000-over.json"), json.dumps(
            {"store": shared, "kind": "resume", "base": hashlib.sha256(open(os.path.join(shared, "RESUME.md"), "rb").read()).hexdigest(),
             "content": "r" * 400, "ts": 0}))
        rc, out, err = cs(["commit"])
        check("commit judges the budget again (FAILED, file unchanged)", rc == 1 and "FAILED resume" in out and "- short" in r(os.path.join(shared, "RESUME.md")), (rc, out))
        w(os.path.join(queue, "thr-1", "000-bad.json"), json.dumps({"store": os.path.join(d, "elsewhere"), "kind": "file", "name": "a.md", "content": "x", "ts": 0}))
        rc, out, err = cs(["commit"])
        check("a staged store outside both roots is still refused", rc == 1 and "outside" in out and not os.path.exists(os.path.join(d, "elsewhere")), (rc, out))

        # --- boot totals include the shared files (Claude boot_budget + Codex boot report)
        w(os.path.join(pilot, "CLAUDE.md"), "# pilot\n@%s\n" % os.path.join(shared, "RESUME.md"))
        w(os.path.join(home, ".claude", "settings.json"), "{}")
        bb = [sys.executable, os.path.join(BIN, "boot_budget.py")]
        benv = dict(env, CLAUDE_CONFIG_DIR=os.path.join(d, "claude"))
        w(os.path.join(d, "claude", "settings.json"), "{}")
        rc, out, err = sub(bb + ["check"], e=benv)
        check("claude check: a shared scope without autoMemoryDirectory is a loud finding",
              rc == 1 and "SHARED STORE pilot" in out and "autoMemoryDirectory" in out and "settings-line --scope pilot" in out, (rc, out))
        rc, line, err = sub([sys.executable, os.path.join(BIN, "shared_store.py"), "settings-line", "--scope", "pilot"])
        check("settings-line prints the autoMemoryDirectory line for settings.local.json",
              rc == 0 and '"autoMemoryDirectory": "%s"' % shared in line and "settings.local.json" in line, (rc, line, err))
        rc, out, err = sub([sys.executable, os.path.join(BIN, "shared_store.py"), "settings-line", "--scope", "plain"])
        check("settings-line refuses a scope that is not opted in", rc == 2, (rc, out, err))
        w(os.path.join(pilot, ".claude", "settings.local.json"), json.dumps({"autoMemoryDirectory": shared}))
        rc, out, err = sub(bb + ["check"], e=benv)
        check("claude check is silent once the setting points at the shared store", rc == 0 and out == "", (rc, out))
        rc, out, err = sub(bb + ["report"], e=benv)
        mem_chars = len(r(os.path.join(shared, "MEMORY.md")))
        check("claude report counts the shared MEMORY.md (native) and RESUME.md (import) in the scope total",
              rc == 0 and os.path.join(shared, "RESUME.md") in out.replace("~", home) + out and "loaded natively" in out
              and os.path.join(shared, "MEMORY.md") in out and ("%s" % format(mem_chars, ",")) in out, (rc, out))
        rc, out, err = sub(bb + ["--agent", "codex", "report"], e=benv)
        check("codex report lists the shared RESUME.md and MEMORY.md in its boot set",
              rc == 0 and shared in out.replace("~", home) and "MEMORY.md" in out and "RESUME.md" in out, (rc, out))

        canary = [sys.executable, os.path.join(BIN, "memory_hygiene_canary.py"), "--report"]
        rc, out, err = sub(canary, e=benv)
        check("the memory canary reads the shared store and does not flag the harness label",
              "3 memory files on disk, 3 distinct index entries" in out and "No findings for this s" in out
              and "feedback_codex_quirk.md" not in out and "harness" not in out, (rc, out))

        # --- migration
        old_c = sc["claude"]["pilot"]
        legacy = legacy_stores(cc, old_c)
        check("legacy stores come from the same resolver as the plain scope's",
              legacy["claude"] == os.path.join(d, "claude", "projects", slug, "memory")
              and legacy["codex"] == os.path.join(d, "codex", "session-notes", "projects", slug))
        shutil.rmtree(shared)
        cdir, xdir = legacy["claude"], legacy["codex"]
        w(os.path.join(cdir, "MEMORY.md"), "# Memory Index\n\n- [A](a.md) - claude a\n- [Both](both.md) - same\n- [Dup](dup.md) - d\n- [Dup](dup.md) - d\n")
        w(os.path.join(xdir, "MEMORY.md"), "# Memory Index\n\n- [X](x.md) - codex x\n- [Both](both.md) - same\n- [Dup](dup.md) - d\n")
        w(os.path.join(cdir, "RESUME.md"), "# RESUME\n\n- c1\n- c2\n")
        w(os.path.join(xdir, "RESUME.md"), "# RESUME\n\n- c1\n- x1\n")
        for n, t in (("a.md", "---\nname: a\ndescription: d\n---\nA\n"), ("both.md", "---\nname: b\ndescription: d\n---\nsame\n"),
                     ("dup.md", "claude dup\n")):
            w(os.path.join(cdir, n), t)
        w(os.path.join(xdir, "both.md"), "---\nname: b\ndescription: d\n---\nsame\n")
        w(os.path.join(xdir, "x.md"), "---\nname: x\ndescription: d\n---\nX\n")
        w(os.path.join(xdir, "dup.md"), "codex dup\n")
        before_old = snap(cdir, xdir)
        before_all = snap(d)
        sstore = [sys.executable, os.path.join(BIN, "shared_store.py")]
        rc, out, err = sub(sstore + ["plan", "--scope", "pilot"])
        check("plan prints what would move and writes NOTHING anywhere",
              rc == 0 and "merge  MEMORY.md" in out and "dup.codex.md" in out and "same   both.md" in out
              and "plan only" in out and snap(d) == before_all and not os.path.exists(shared), (rc, out, err))
        rc, out, err = sub(sstore + ["migrate", "--scope", "plain"])
        check("migrate refuses a scope that is not opted in", rc == 2 and "not opted in" in err and snap(d) == before_all, (rc, err))
        # RED: an entry is dropped between the merge and the write
        global build
        real_build = build

        def dropping(cfg, scope, labels=None):
            outs, rep, pr = real_build(cfg, scope, labels)
            outs["MEMORY.md"] = outs["MEMORY.md"].replace("- [A](a.md) - claude a\n", "")
            return outs, rep, pr
        build = dropping
        try:
            os.environ["SESSION_NOTES_CONFIG"] = cfgf
            rc = main(["migrate", "--scope", "pilot"])
        finally:
            build = real_build
        check("migrate fails RED when an index entry is dropped, and moves nothing into place",
              rc == 1 and not os.path.exists(shared) and snap(cdir, xdir) == before_old)

        def dropping_file(cfg, scope, labels=None):
            outs, rep, pr = real_build(cfg, scope, labels)
            outs.pop("x.md")
            return outs, rep, pr
        build = dropping_file
        try:
            rc = main(["migrate", "--scope", "pilot"])
        finally:
            build = real_build
        check("migrate fails RED when a memory file is dropped", rc == 1 and not os.path.exists(shared))
        rc, out, err = sub(sstore + ["migrate", "--scope", "pilot"])
        check("migrate succeeds on the real merge and says every entry landed once",
              rc == 0 and "landed exactly once" in out, (rc, out, err))
        check("old stores untouched (byte-identical)", snap(cdir, xdir) == before_old)
        mem = r(os.path.join(shared, "MEMORY.md"))
        check("MEMORY merged: claude's duplicate lines kept (counted, not collapsed), codex-only line appended",
              mem.count("- [Dup](dup.md) - [claude] d") == 2 and mem.count("- [Dup](dup.codex.md)") == 1
              and mem.index("claude a") < mem.index("- [X](x.md) - codex x"), mem)
        check("both-store identical file lands once, codex-only file lands",
              r(os.path.join(shared, "both.md")).count("same") == 1 and os.path.isfile(os.path.join(shared, "x.md")))
        check("conflicting file: both versions kept, each labeled for its harness",
              harness_of(r(os.path.join(shared, "dup.md"))) == "claude" and harness_of(r(os.path.join(shared, "dup.codex.md"))) == "codex"
              and "claude dup" in r(os.path.join(shared, "dup.md")) and "codex dup" in r(os.path.join(shared, "dup.codex.md")))
        check("the conflicting codex index line points at the renamed file and is marked",
              "- [Dup](dup.codex.md) - [codex] d" in mem and "- [Dup](dup.md) - [claude] d" in mem, mem)
        res = r(os.path.join(shared, "RESUME.md"))
        check("RESUME merged by entry: c1 once, c2 kept, x1 appended after the marker",
              res.count("- c1") == 1 and "- c2" in res and res.index(MARKER) < res.index("- x1"), res)
        check("verify() is green on the migrated store and the pointer file names the old stores",
              verify(cc, old_c, shared) == [] and "claude:" in r(os.path.join(shared, "MIGRATED-FROM.txt")))
        os.unlink(os.path.join(shared, "a.md"))
        check("verify() goes red when a file is later missing", any("a.md" in b for b in verify(cc, old_c, shared)))
        rc, out, err = sub(sstore + ["migrate", "--scope", "pilot"])
        check("migrate over a store that now differs refuses to overwrite", rc == 2 and "different content" in err, (rc, err))
        w(os.path.join(xdir, "sub", "n.md"), "x")
        rc, out, err = sub(sstore + ["plan", "--scope", "pilot"])
        check("a subfolder in an old store is a PROBLEM (RED), never silently skipped", rc == 1 and "NOT MIGRATED" in out, (rc, out))

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
