#!/usr/bin/env python3
"""codex_hooks.py -- register (and refresh) this repo's hooks in Codex's hooks.json.

    codex_hooks.py show  --repo DIR                  print the hooks this repo registers
    codex_hooks.py merge --repo DIR --file hooks.json [--dry-run]

`merge` is a refresh, not a one-shot write: it replaces only session-notes entries and
leaves every other entry and event exactly as it was (a user may keep private hooks in the
same file). An entry is OURS when its command starts with SESSION_NOTES_AGENT=codex and
runs one of the basenames below from a `hooks/<basename>` path, from any checkout, so a
moved or removed checkout's entries are replaced, not duplicated. Running it twice gives a
byte-identical file, and an unchanged entry stays byte-identical, because Codex trusts the
exact hook definition and any change to it forces a re-trust.

What gets registered (every command is prefixed SESSION_NOTES_AGENT=codex):
  SessionStart      codex_session_start.py (boot load), session_pane_registry.py
  UserPromptSubmit  time_sense.py, session_bus_hook.py, session_pane_registry.py
  Stop              session_bus_hook.py, session_pane_registry.py
  PostToolUse       apply_patch: em_dash_guard.py

A file that is not valid JSON, or whose "hooks" shape is unexpected (an event that is not a
list, a group or handler that is not an object), is refused untouched with a clear error.
The file is written through a symlink (the target is replaced) and keeps its mode.
"""
import argparse
import difflib
import json
import os
import shlex
import shutil
import sys
import tempfile

OWNED = ("codex_session_start.py", "time_sense.py", "session_bus_hook.py",
         "session_pane_registry.py", "em_dash_guard.py")


PREFIX = "SESSION_NOTES_AGENT=codex"


def _cmd(repo, name):
    return "%s python3 %s" % (PREFIX, shlex.quote("%s/hooks/%s" % (repo, name)))


def desired(repo):
    """{event: [group, ...]} this repo registers."""
    def h(name, **extra):
        return dict({"type": "command", "command": _cmd(repo, name)}, **extra)
    return {
        "SessionStart": [
            {"matcher": "startup|resume|compact",
             "hooks": [h("codex_session_start.py", statusMessage="Loading session notes",
                         timeout=10, additionalContextLimit=4000)]},
            {"hooks": [h("session_pane_registry.py", timeout=10)]},
        ],
        "UserPromptSubmit": [
            {"hooks": [h("time_sense.py", timeout=10),
                       h("session_bus_hook.py", timeout=10, additionalContextLimit=6000),
                       h("session_pane_registry.py", timeout=10)]},
        ],
        "Stop": [
            # no additionalContextLimit: Stop cannot add context, and Codex warns on it
            {"hooks": [h("session_bus_hook.py", timeout=10),
                       h("session_pane_registry.py", timeout=10)]},
        ],
        "PostToolUse": [
            {"matcher": "apply_patch", "hooks": [h("em_dash_guard.py", timeout=10)]},
        ],
    }


def owned(hook):
    """True for a handler this repo registers (see the module docstring)."""
    cmd = hook.get("command") if isinstance(hook, dict) else None
    if not isinstance(cmd, str):
        return False
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    if not toks or toks[0] != PREFIX:
        return False
    return any(os.path.basename(t) in OWNED and os.path.basename(os.path.dirname(t)) == "hooks"
               for t in toks[1:])


def validate(existing):
    """Raise ValueError unless the hooks shape is one merge() can edit without guessing."""
    hooks = existing.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" is not an object')
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError('hooks.%s is not a list' % event)
        for g in groups:
            if not isinstance(g, dict):
                raise ValueError("a group under hooks.%s is not an object" % event)
            if "hooks" in g:
                if not isinstance(g["hooks"], list):
                    raise ValueError("a group's hooks under hooks.%s is not a list" % event)
                if any(not isinstance(x, dict) for x in g["hooks"]):
                    raise ValueError("a handler under hooks.%s is not an object" % event)


def merge(existing, repo):
    """The hooks.json dict after a refresh. `existing` is not modified."""
    validate(existing)
    out = json.loads(json.dumps(existing))
    hooks = out.setdefault("hooks", {})
    want = desired(repo)
    for event in list(hooks):
        groups = hooks[event]
        slot = None
        kept = []
        for g in groups:
            hs = g.get("hooks")
            if not hs or not any(owned(x) for x in hs):
                kept.append(g)
                continue
            if slot is None:
                slot = len(kept)
            rest = [x for x in hs if not owned(x)]
            if rest:
                kept.append(dict(g, hooks=rest))
        if slot is not None:
            hooks[event] = kept[:slot] + want.pop(event, []) + kept[slot:]
        elif kept != groups:
            hooks[event] = kept
        if slot is not None and not hooks[event]:
            del hooks[event]  # an event of ours emptied by the refresh, not re-registered
    for event, groups in want.items():
        hooks[event] = list(hooks.get(event, [])) + groups
    return out


def render(data):
    return json.dumps(data, indent=2) + "\n"


def refresh(path, repo, dry_run=False):
    """Merge into `path`. Returns (changed, diff_text). Writes unless dry_run, keeping a
    .bak-session-notes copy of an existing file. A symlink is written through (its target
    is replaced) and the file keeps its mode."""
    path = os.path.realpath(path)
    text, existing = "", {}
    if os.path.exists(path):
        with open(path) as fh:
            text = fh.read()
        if text.strip():
            existing = json.loads(text)
            if not isinstance(existing, dict):
                raise ValueError("top level is not an object")
    new = render(merge(existing, repo))
    if text.strip() and json.loads(new) == existing:
        return False, ""
    diff = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True),
                                        path, path + " (after)"))
    if not dry_run:
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        mode = os.stat(path).st_mode & 0o7777 if os.path.exists(path) else None
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak-session-notes")
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".hooks.")
        with os.fdopen(fd, "w") as fh:
            fh.write(new)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    return True, diff


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("show", "merge"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        if name == "merge":
            p.add_argument("--file", required=True)
            p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    if a.cmd == "show":
        sys.stdout.write(render({"hooks": desired(repo)}))
        return 0
    try:
        changed, diff = refresh(a.file, repo, dry_run=a.dry_run)
    except (ValueError, json.JSONDecodeError) as e:
        print("codex_hooks: %s is not usable (%s); left untouched" % (a.file, e), file=sys.stderr)
        return 1
    if not changed:
        print("%s already up to date" % a.file)
    else:
        sys.stdout.write(diff)
        print(("would update %s" if a.dry_run else "updated %s") % a.file)
    return 0


def selftest():
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    repo = "/opt/session-notes"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "codex", "hooks.json")

        def load():
            with open(path) as fh:
                return json.load(fh)

        def cmds(data, event):
            return [x["command"] for g in data["hooks"].get(event, []) for x in g["hooks"]]

        # fresh file
        changed, _ = refresh(path, repo)
        d = load()
        check("fresh: file written", changed and os.path.exists(path))
        check("fresh: SessionStart keeps the exact boot command", d["hooks"]["SessionStart"][0] == {
            "matcher": "startup|resume|compact",
            "hooks": [{"type": "command",
                       "command": "SESSION_NOTES_AGENT=codex python3 /opt/session-notes/hooks/codex_session_start.py",
                       "statusMessage": "Loading session notes", "timeout": 10,
                       "additionalContextLimit": 4000}]})
        check("fresh: prompt hooks", [c.rsplit("/", 1)[1] for c in cmds(d, "UserPromptSubmit")]
              == ["time_sense.py", "session_bus_hook.py", "session_pane_registry.py"])
        check("fresh: stop hooks", [c.rsplit("/", 1)[1] for c in cmds(d, "Stop")]
              == ["session_bus_hook.py", "session_pane_registry.py"])
        check("fresh: em dash guard on apply_patch",
              d["hooks"]["PostToolUse"][0]["matcher"] == "apply_patch")
        check("fresh: every command carries the agent prefix", all(
            c.startswith("SESSION_NOTES_AGENT=codex ")
            for e in d["hooks"] for c in cmds(d, e)))

        # idempotence: byte-identical, no rewrite, no new backup
        before = open(path, "rb").read()
        changed, _ = refresh(path, repo)
        check("rerun: reports unchanged", not changed)
        check("rerun: byte-identical", open(path, "rb").read() == before)

        # foreign entries preserved, ours refreshed in place
        foreign = {"type": "command", "command": "python3 /opt/other/private_hook.py"}
        d = load()
        d["hooks"]["Stop"].append({"hooks": [foreign]})
        d["hooks"]["UserPromptSubmit"][0]["hooks"].insert(0, foreign)
        d["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [foreign]}]
        d["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 99  # a stale edit of ours
        d["model"] = "keep-me"
        with open(path, "w") as fh:
            fh.write(render(d))
        changed, diff = refresh(path, repo, dry_run=True)
        check("dry run: shows a diff and writes nothing", changed and "timeout" in diff
              and load()["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 99)
        refresh(path, repo)
        n = load()
        check("foreign: other events and keys untouched", n["hooks"]["PreToolUse"] == d["hooks"]["PreToolUse"]
              and n["model"] == "keep-me")
        check("foreign: Stop keeps the private hook", foreign["command"] in cmds(n, "Stop"))
        check("foreign: mixed group keeps the private hook", foreign["command"] in cmds(n, "UserPromptSubmit"))
        check("foreign: our hooks restored once, not duplicated", sum(
            "session_bus_hook" in c for c in cmds(n, "UserPromptSubmit")) == 1
            and n["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 10)
        check("backup kept", os.path.exists(path + ".bak-session-notes"))
        again = open(path, "rb").read()
        refresh(path, repo)
        check("foreign: rerun byte-identical", open(path, "rb").read() == again)

        # ownership: prefix + our basename + hooks/<name> path; a moved checkout is cleaned up
        pre = "SESSION_NOTES_AGENT=codex python3 "
        old = load()
        old["hooks"]["Stop"] = [
            {"hooks": [{"type": "command", "command": pre + "/old/worktree/hooks/session_bus_hook.py"}]},
            {"hooks": [{"type": "command", "command": "python3 /opt/session-notes/hooks/session_bus_hook.py"}]},
            {"hooks": [{"type": "command", "command": "python3 /backup/x/hooks/foo.py"}]},
            {"hooks": [{"type": "command", "command": pre + "/p/hooks/origin_sense.py"},
                       {"type": "command", "command": pre + "/p/hooks/uploads_sense.py"},
                       {"type": "command", "command": pre + "/p/hooks/uploads_claim.py"}]},
            {"hooks": [{"type": "command", "command": pre + "/p/nothooks/time_sense.py"}]}]
        with open(path, "w") as fh:
            fh.write(render(old))
        refresh(path, repo)
        stop = cmds(load(), "Stop")
        check("old: a stale entry from another checkout is replaced",
              pre + "/old/worktree/hooks/session_bus_hook.py" not in stop
              and pre + "/opt/session-notes/hooks/session_bus_hook.py" in stop)
        check("old: no prefix means not ours", "python3 /opt/session-notes/hooks/session_bus_hook.py" in stop
              and "python3 /backup/x/hooks/foo.py" in stop)
        check("old: private hooks with the prefix are kept", all(
            pre + "/p/hooks/" + n in stop for n in ("origin_sense.py", "uploads_sense.py", "uploads_claim.py")))
        check("old: path-component boundary, not substring", pre + "/p/nothooks/time_sense.py" in stop)
        bus_cmd = pre + "/opt/session-notes/hooks/session_bus_hook.py"
        check("bus hook carries additionalContextLimit 6000 on UserPromptSubmit", any(
            x.get("additionalContextLimit") == 6000 for g in load()["hooks"]["UserPromptSubmit"]
            for x in g["hooks"] if x["command"] == bus_cmd))
        check("bus hook on Stop has no additionalContextLimit", all(
            "additionalContextLimit" not in x for g in load()["hooks"]["Stop"]
            for x in g["hooks"] if x["command"] == bus_cmd))

        # foreign empty events survive; unexpected shapes are refused untouched
        e = load()
        e["hooks"]["PreToolUse"] = []
        with open(path, "w") as fh:
            fh.write(render(e))
        refresh(path, repo)
        check("foreign empty event preserved", load()["hooks"].get("PreToolUse") == [])
        for bad in ({"hooks": {"Stop": {"a": 1}}}, {"hooks": {"Stop": ["x"]}},
                    {"hooks": {"Stop": [{"hooks": ["x"]}]}}, {"hooks": []}):
            with open(path, "w") as fh:
                json.dump(bad, fh)
            raw = open(path).read()
            check("refused untouched: %s" % json.dumps(bad),
                  main(["merge", "--repo", repo, "--file", path]) == 1 and open(path).read() == raw)

        # symlink target written through, mode kept
        real = os.path.join(tmp, "real.json")
        link = os.path.join(tmp, "link.json")
        with open(real, "w") as fh:
            fh.write("{}")
        os.chmod(real, 0o600)
        os.symlink(real, link)
        refresh(link, repo)
        check("symlink: still a link, target updated", os.path.islink(link)
              and "SessionStart" in json.load(open(real))["hooks"])
        check("mode preserved", os.stat(real).st_mode & 0o777 == 0o600)

        # install.sh --codex: the closing text must not EXECUTE the backticked `codex` it quotes,
        # and a corrupt hooks.json skips only the hooks step. A stub codex on PATH logs any call.
        import subprocess
        repo_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        stubdir = os.path.join(tmp, "stub")
        os.makedirs(stubdir)
        calls_log = os.path.join(tmp, "codex-called")
        with open(os.path.join(stubdir, "codex"), "w") as fh:
            fh.write('#!/bin/sh\necho "$@" >> "%s"\nexit 9\n' % calls_log)
        os.chmod(os.path.join(stubdir, "codex"), 0o755)

        def run_install(*extra, home_hooks=None):
            ch = os.path.join(tmp, "ch_home")
            os.makedirs(ch, exist_ok=True)
            hj = os.path.join(ch, "hooks.json")
            if os.path.exists(hj):
                os.remove(hj)
            if home_hooks is not None:
                with open(hj, "w") as fh:
                    fh.write(home_hooks)
            env = dict(os.environ, CODEX_HOME=ch, PATH=stubdir + os.pathsep + os.environ["PATH"])
            r = subprocess.run(["sh", os.path.join(repo_root, "install.sh"), "--codex"] + list(extra),
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
                               timeout=120)
            return r, hj
        r, hj = run_install()
        check("install --codex: the stub codex is never invoked", not os.path.exists(calls_log))
        check("install --codex: closing text prints the backticked command", "`codex --no-daemon`" in r.stdout
              and "daemon_auto_start" in r.stdout and r.returncode == 0)
        r, hj = run_install("--dry-run", home_hooks="{broken")
        check("install --codex: corrupt hooks.json skips the hooks step and continues",
              "Hooks step skipped" in r.stdout and "Trust new or changed hooks" in r.stdout
              and open(hj).read() == "{broken" and not os.path.exists(calls_log))

        # an empty or unusable file
        with open(path, "w") as fh:
            fh.write("")
        check("empty file treated as fresh", refresh(path, repo)[0] and "SessionStart" in load()["hooks"])
        with open(path, "w") as fh:
            fh.write("{broken")
        check("corrupt file refused untouched", main(["merge", "--repo", repo, "--file", path]) == 1
              and open(path).read() == "{broken")
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
