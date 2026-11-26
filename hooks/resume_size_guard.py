#!/usr/bin/env python3
"""resume_size_guard.py -- Claude Code PreToolUse hook: refuse a RESUME.md write over its budget.

The Claude half of the write-time size guard (the Codex half is `codex_save.py commit`). Both
call the one predicate in bin/resume_guard.py, so the rule and the budget live once.

Judged tools (matcher Edit|Write|MultiEdit|Bash):
  * Edit / Write / MultiEdit whose target is a configured store's RESUME.md (the global store's
    or a scope's, shared or not): the result is simulated from the file on disk and handed to
    resume_guard.check_write. An Edit's delta is (len(new) - len(old)) x occurrences, because the
    result is built by really replacing every occurrence the edit would replace.
  * Bash: a command that WRITES such a file (redirect, tee, sed -i, cp/mv/rm, truncate, dd of=,
    inside bash -c / eval too, found with checkout_guard's own shell parser) is refused outright:
    its result cannot be simulated, so the model uses Edit or Write, where it can be judged.
Everything else exits 0 untouched, and a file not named RESUME.md never loads the config.

Fail loud: a RESUME.md target with a config that cannot be loaded is REFUSED (exit 2, naming the
config error), never waved through. An unreadable payload or an internal error exits 0 with one
line on stderr, so a hiccup cannot break unrelated writes. Exit 2 + stderr is the refusal.

The command is parsed BEFORE the config is loaded, so a broken config refuses only a command
that writes some RESUME.md, never `cat`/`grep`/`git log` on one. The old RESUME.md of a scope
that migrated to a shared store is refused ("write the shared store's RESUME.md instead").
Known limits, as with checkout_guard: a program that writes on its own (`python3 script.py`), and
a path built from `$VAR` or `"$HOME/..."` (not expanded) are not seen. Also not caught, because
the parser knows redirects, tee, sed/perl -i, cp/mv/rm/install/ln/unlink, truncate and dd of=, and not: rsync, `git checkout --`,
patch, `sort -o`, sponge, ed and `find -delete`. This hook is a net for the common slip, not a wall.

    python3 resume_size_guard.py --selftest
"""
import json
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
sys.path.insert(0, BIN)
sys.path.insert(0, HERE)
import notes_config  # noqa: E402
import resume_guard  # noqa: E402

BASH_REFUSAL = ("%s is a RESUME.md: a shell command that writes it cannot be size-checked. Use the "
                "Edit or Write tool on it instead (a write that leaves it over its budget is then "
                "refused with the instruction to move finished items to RESUME-archive.md beside "
                "it, verbatim, then retry).")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def _load_or_refuse():
    """(cfg, None) or (None, refusal text): a broken config is a refusal, never a pass."""
    try:
        return notes_config.load(), None
    except notes_config.ConfigError as exc:
        return None, ("resume_size_guard: cannot judge a RESUME.md write, the config does not "
                      "load: %s. Fix the config (the write is refused, not waved through)." % exc)


def simulate(tool, ti, old):
    """The file's text after an Edit/Write/MultiEdit, or None when the tool call will fail on
    its own (an old_string that is not there) and so changes nothing."""
    if tool == "Write":
        return ti.get("content") if isinstance(ti.get("content"), str) else None
    edits = [ti] if tool == "Edit" else (ti.get("edits") or [])
    text = old
    for e in edits:
        o, n = e.get("old_string"), e.get("new_string")
        if not isinstance(o, str) or not isinstance(n, str) or (o == "" and text):
            return None
        if o not in text:
            return None
        text = text.replace(o, n) if e.get("replace_all") else text.replace(o, n, 1)
    return text


def bash_resume_targets(cmd, cwd, depth=0):
    """Realpaths of every file named RESUME.md a shell command writes (parsed with
    checkout_guard's parser, no config needed). A `$VAR` or `"$HOME/..."` path is not expanded
    and so is not seen."""
    import checkout_guard as cg
    out = []
    for seg, writes, _sub in cg.split_segments(cg.strip_heredocs(cmd)):
        try:
            argv = shlex.split(seg)
        except ValueError:
            argv = []
        argv = cg.strip_prefix(argv) if argv else argv
        if argv and os.path.basename(argv[0]) in ("cd", "pushd") and len(argv) > 1:
            cwd = cg._literal_dir(argv[1], cwd) or cwd  # a literal `cd DIR && ...` moves the base
            continue
        for target, _g, _r in cg._write_targets(argv, writes, cwd):
            if not target or target.startswith("-") or os.path.basename(target) != "RESUME.md" \
                    or "$" in target or "`" in target:
                continue
            t = os.path.expanduser(target)
            out.append(os.path.realpath(t if os.path.isabs(t) else os.path.join(cwd, t)))
        if argv and os.path.basename(argv[0]) in cg.SHELLS and depth < 3:
            script = cg._script_of(argv)
            if script:
                out += bash_resume_targets(script, cwd, depth + 1)
        if argv and argv[0] == "eval" and depth < 3 and len(argv) > 1:
            out += bash_resume_targets(" ".join(argv[1:]), cwd, depth + 1)
    return out


def decide(payload):
    """The refusal text, or None to allow. May raise (main fails open on that)."""
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool in ("Edit", "Write", "MultiEdit"):
        path = ti.get("file_path")
        if not isinstance(path, str) or os.path.basename(path) != "RESUME.md":
            return None
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        cfg, err = _load_or_refuse()
        if err:
            return err
        legacy = resume_guard.legacy_refusal(path, cfg)
        if legacy:
            return legacy
        if not resume_guard.is_configured_resume(path, cfg):
            return None
        old = _read(path)
        new = simulate(tool, ti, old)
        if new is None:
            return None
        return resume_guard.check_write(path, old, new)
    if tool == "Bash":
        cmd = ti.get("command")
        if not isinstance(cmd, str) or "RESUME.md" not in cmd:
            return None
        targets = bash_resume_targets(cmd, cwd)
        if not targets:
            return None  # nothing it writes is a RESUME.md: no config needed, none loaded
        cfg, err = _load_or_refuse()
        if err:
            return err
        known = resume_guard.configured_resumes(cfg) | set(resume_guard.legacy_resumes(cfg))
        hit = next((t for t in targets if t in known), None)
        return BASH_REFUSAL % hit if hit else None
    return None


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        sys.stderr.write("resume_size_guard: unreadable payload: %s\n" % exc)
        return 0
    try:
        msg = decide(payload)
    except Exception as exc:
        sys.stderr.write("resume_size_guard: %s: %s\n" % (type(exc).__name__, exc))
        return 0
    if msg:
        sys.stderr.write(msg + "\n")
        return 2
    return 0


def selftest():
    import tempfile
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        home = os.path.join(d, "home")
        os.makedirs(home)
        cfgf = os.path.join(d, "c.toml")
        store = os.path.join(d, "store")
        os.makedirs(store)
        gstore = os.path.join(d, "g")
        os.makedirs(gstore)
        with open(cfgf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\nglobal_store = "%s"\n'
                     '[budgets]\nresume_bytes = 100\n'
                     '[[scopes]]\nname = "a"\nroot = "%s/a"\nstore = "%s"\n'
                     '[[scopes]]\nname = "m"\nroot = "%s/m"\nshared_store = true\n' % (d, gstore, d, store, d))
        env = {k: v for k, v in os.environ.items() if not k.startswith("SESSION_NOTES_")}
        env.update(HOME=home, SESSION_NOTES_CONFIG=cfgf)
        resume = os.path.join(store, "RESUME.md")

        def run(payload, e=None):
            r = subprocess.run([sys.executable, os.path.abspath(__file__)], input=json.dumps(payload),
                               capture_output=True, text=True, env=e or env, cwd=d)
            return r.returncode, r.stderr

        def w(path, text):
            with open(path, "w") as fh:
                fh.write(text)

        w(resume, "a\n" * 20)  # 40 chars
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": resume, "content": "x" * 200}})
        check("Write that crosses the budget is refused (exit 2) with the archive text",
              rc == 2 and "RESUME-archive.md" in err and "verbatim" in err, (rc, err))
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": resume, "content": "x" * 99}})
        check("Write under the budget passes", rc == 0 and err == "", (rc, err))
        w(resume, "a" * 60 + "\nb\n")
        # Edit delta = (len(new)-len(old)) x occurrences: 3 occurrences x +20 crosses 100
        w(resume, "q q q " + "z" * 40)
        rc, err = run({"tool_name": "Edit", "tool_input": {"file_path": resume, "old_string": "q",
                                                           "new_string": "q" * 21, "replace_all": True}})
        check("replace_all Edit is judged on its result, delta x occurrences", rc == 2, (rc, err))
        rc, err = run({"tool_name": "Edit", "tool_input": {"file_path": resume, "old_string": "q",
                                                           "new_string": "q" * 21}})
        check("single Edit of the same text passes (1 occurrence)", rc == 0, (rc, err))
        rc, err = run({"tool_name": "Edit", "tool_input": {"file_path": resume, "old_string": "nope",
                                                           "new_string": "x" * 500}})
        check("an Edit whose old_string is absent changes nothing: passes", rc == 0, (rc, err))
        lines = "".join("entry number %02d\n" % i for i in range(30))  # 480 chars, over the 100 budget
        w(resume, lines)
        rc, err = run({"tool_name": "Edit", "tool_input": {"file_path": resume, "old_string": "entry number 05\n",
                                                           "new_string": ""}})
        check("an Edit that only removes a line of an over-budget file passes", rc == 0, (rc, err))
        rc, err = run({"tool_name": "Edit", "tool_input": {"file_path": resume, "old_string": "entry number 05\n",
                                                           "new_string": "entry number 5\n"}})
        check("the 1-char swap (shorter, but a new line) is refused while over budget", rc == 2
              and "only remove lines" in err, (rc, err))
        rc, err = run({"tool_name": "Edit", "tool_input": {"file_path": resume, "old_string": "entry number 05",
                                                           "new_string": "entry number 055"}})
        check("a growing Edit of an over-budget file is refused", rc == 2, (rc, err))
        rc, err = run({"tool_name": "MultiEdit", "tool_input": {"file_path": resume, "edits": [
            {"old_string": "entry number 05\n", "new_string": ""}, {"old_string": "entry number 06\n", "new_string": ""}]}})
        check("MultiEdit is judged on the final result (two removals pass)", rc == 0, (rc, err))
        rc, err = run({"tool_name": "MultiEdit", "tool_input": {"file_path": resume, "edits": [
            {"old_string": "entry number 05\n", "new_string": ""}, {"old_string": "entry number 06", "new_string": "entry number 66"}]}})
        check("MultiEdit that removes one line and changes another is refused", rc == 2, (rc, err))
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": resume, "content": "short\n"}})
        check("a rewrite that lands under budget passes", rc == 0, (rc, err))
        w(resume, "a\n" * 20)
        import shared_store
        mcfg = notes_config.load(cfgf, env={})
        mscope = [x for x in mcfg["scopes"] if x["name"] == "m"][0]
        legacy = os.path.join(shared_store.legacy_stores(mcfg, mscope)["claude"], "RESUME.md")
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": legacy, "content": "x"}})
        check("the old RESUME.md of a migrated scope is refused: write the shared store",
              rc == 2 and "migrated to a shared store" in err and os.sep + "stores" + os.sep in err, (rc, err))
        rc, err = run({"tool_name": "Bash", "tool_input": {"command": "echo x >> %s" % legacy}})
        check("a Bash write to the old RESUME.md is refused too", rc == 2, (rc, err))
        other = os.path.join(store, "feedback_x.md")
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": other, "content": "x" * 5000}})
        check("a non-RESUME file in the store is ignored", rc == 0 and err == "", (rc, err))
        stray = os.path.join(d, "elsewhere", "RESUME.md")
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": stray, "content": "x" * 5000}})
        check("a RESUME.md outside every configured store is ignored", rc == 0, (rc, err))
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": os.path.join(gstore, "RESUME.md"),
                                                            "content": "x" * 500}})
        check("the global store's RESUME.md is guarded too", rc == 2, (rc, err))
        for cmd in ('echo hi >> %s' % resume, 'tee %s < /dev/null' % resume, "sed -i '' s/a/b/ %s" % resume,
                    'bash -c "cat x > %s"' % resume, 'cd %s && mv x RESUME.md' % store, 'rm %s' % resume):
            rc, err = run({"tool_name": "Bash", "tool_input": {"command": cmd}})
            check("Bash write refused outright: %s" % cmd[:40],
                  rc == 2 and "Edit or Write" in err and "RESUME-archive.md" in err, (rc, err))
        for cmd in ('cat %s' % resume, 'grep -n a %s | head' % resume, 'wc -c %s' % resume,
                    'echo RESUME.md > /tmp/x-not-resume'):
            rc, err = run({"tool_name": "Bash", "tool_input": {"command": cmd}})
            check("Bash read of RESUME.md passes: %s" % cmd[:30], rc == 0, (rc, err))
        # missing / broken config fails loud for a RESUME target, and stays out of the way otherwise
        bad = dict(env, SESSION_NOTES_CONFIG=os.path.join(d, "missing.toml"))
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": resume, "content": "x"}}, bad)
        check("missing config: a RESUME write is refused loudly, not passed",
              rc == 2 and "config does not load" in err and "missing.toml" in err, (rc, err))
        rc, err = run({"tool_name": "Write", "tool_input": {"file_path": other, "content": "x"}}, bad)
        check("missing config: a non-RESUME write is untouched", rc == 0 and err == "", (rc, err))
        rc, err = run({"tool_name": "Bash", "tool_input": {"command": "echo x > %s" % resume}}, bad)
        check("missing config: a Bash RESUME write is refused loudly", rc == 2, (rc, err))
        for cmd in ('cat %s' % resume, 'grep -n a docs/RESUME.md', 'git log -- RESUME.md', 'wc -c RESUME.md'):
            rc, err = run({"tool_name": "Bash", "tool_input": {"command": cmd}}, bad)
            check("broken config: a command that writes no RESUME.md is not refused: %s" % cmd[:24], rc == 0 and err == "", (rc, err))
        rc, err = run({"tool_name": "Bash", "tool_input": {"command": "echo x > docs/RESUME.md"}}, bad)
        check("broken config: a command that DOES write a RESUME.md is refused loudly", rc == 2 and "missing.toml" in err, (rc, err))
        rc, err = run({"tool_name": "Bash", "tool_input": {"command": 'echo x > "$HOME/RESUME.md"'}}, bad)
        check("known limit: a $HOME path is not expanded, so it is not seen", rc == 0, (rc, err))
        rc, err = subprocess.run([sys.executable, os.path.abspath(__file__)], input="not json",
                                 capture_output=True, text=True, env=env).returncode, ""
        check("an unreadable payload fails open", rc == 0)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
