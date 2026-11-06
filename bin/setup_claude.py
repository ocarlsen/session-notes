#!/usr/bin/env python3
"""setup_claude.py -- the two edits install.sh offers to make, each only on a yes.

    setup_claude.py hooks     --settings PATH --json HOOKS_JSON [--env KEY=VALUE ...] [--yes]
    setup_claude.py claude-md --file PATH --snippet PATH [--yes]
    setup_claude.py --selftest

`hooks` merges a {"Event": [entry, ...]} block into the "hooks" object of a Claude Code
settings.json. An entry whose command is already registered for that event is left alone,
so running it twice adds nothing. Each --env KEY=VALUE goes into settings.json's "env" object,
which Claude Code sets for every session; a KEY already set to another value is left as it is
and reported. `claude-md` appends the snippet to a CLAUDE.md unless its
first line is already there.

Each one shows what it would change and asks "[y/N]" (no answer means no). --yes answers yes
for scripted installs. Before writing, the current file is copied to <file>.bak-session-notes
and the new content replaces it atomically. A settings.json that is not valid JSON is an
error that names the file: it is never overwritten. On a no, the block is printed for a
by-hand merge instead.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

BACKUP = ".bak-session-notes"


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def commands(entry):
    return [h.get("command", "") for h in entry.get("hooks", []) if isinstance(h, dict)]


def merge_hooks(settings, block):
    """(new settings, [(event, matcher, command), ...] added). Never mutates the input."""
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" in settings.json is not an object')
    added = []
    for event, entries in block.items():
        have = hooks.setdefault(event, [])
        if not isinstance(have, list):
            raise ValueError('"hooks.%s" in settings.json is not a list' % event)
        known = {(e.get("matcher", ""), c) for e in have if isinstance(e, dict) for c in commands(e)}
        for entry in entries:
            new = [c for c in commands(entry) if (entry.get("matcher", ""), c) not in known]
            if not new:
                continue
            have.append(entry)
            for c in new:
                known.add((entry.get("matcher", ""), c))
                added.append((event, entry.get("matcher", ""), c))
    return out, added


def merge_env(settings, env):
    """(new settings, [(key, value) added], [(key, current) kept because it differs])."""
    out = json.loads(json.dumps(settings))
    if not env:
        return out, [], []
    cur = out.setdefault("env", {})
    if not isinstance(cur, dict):
        raise ValueError('"env" in settings.json is not an object')
    added, kept = [], []
    for key, value in env:
        if key not in cur:
            cur[key] = value
            added.append((key, value))
        elif cur[key] != value:
            kept.append((key, cur[key]))
    return out, added, kept


def write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + BACKUP)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".setup_claude.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        # Keep the file's own mode; a new file gets the usual 0644 less the umask (mkstemp is 0600).
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        else:
            mask = os.umask(0)
            os.umask(mask)
            os.chmod(tmp, 0o644 & ~mask)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ask(question, yes):
    if yes:
        print(question + " [y/N] y")
        return True
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        print()
        return False


def cmd_hooks(args):
    block = json.loads(args.json)
    try:
        with open(args.settings) as fh:
            raw = fh.read()
        settings = json.loads(raw) if raw.strip() else {}
    except FileNotFoundError:
        settings = {}
    except ValueError as exc:
        print("setup_claude: %s is not valid JSON (%s); not touching it." % (args.settings, exc), file=sys.stderr)
        return 2
    env = [tuple(kv.split("=", 1)) for kv in args.env or []]
    try:
        merged, added = merge_hooks(settings, block)
        merged, env_added, env_kept = merge_env(merged, env)
    except ValueError as exc:
        print("setup_claude: %s: %s; not touching it." % (args.settings, exc), file=sys.stderr)
        return 2
    for key, current in env_kept:
        print("env %s is already %s in %s; leaving it." % (key, current, tilde(args.settings)))
    if not added and not env_added:
        print("Hooks: all already in %s." % tilde(args.settings))
        return 0
    print("Hooks to add to %s:" % tilde(args.settings))
    rows = [(event + ("(%s)" % matcher if matcher else ""), command) for event, matcher, command in added]
    rows += [("env", "%s=%s" % (key, tilde(value))) for key, value in env_added]
    width = max(len(label) for label, _ in rows)
    for label, text in rows:
        print("  %-*s  %s" % (width, label, text))
    if ask("Add them?", args.yes):
        write_atomic(args.settings, json.dumps(merged, indent=2) + "\n")
        what = []
        if added:
            what.append("%d hook%s" % (len(added), "" if len(added) == 1 else "s"))
        what += [key for key, _ in env_added]
        print("Added %s%s." % (" and ".join(what), " (backup: %s)" % tilde(args.settings + BACKUP)
                                if os.path.exists(args.settings + BACKUP) else ""))
    else:
        print('Skipped. To add them by hand, merge this into "hooks":')
        print(json.dumps(block, indent=2))
        if env_added:
            print('and this into "env":')
            print(json.dumps(dict(env_added), indent=2))
    return 0


def cmd_claude_md(args):
    with open(args.snippet) as fh:
        snippet = fh.read().rstrip("\n") + "\n"
    first = snippet.splitlines()[0]
    try:
        with open(args.file) as fh:
            current = fh.read()
    except FileNotFoundError:
        current = ""
    if first in current.splitlines():
        print("CLAUDE.md: %s already has %r." % (tilde(args.file), first))
        return 0
    print("Lines to add to %s, so Claude knows the commands:" % tilde(args.file))
    print("".join("  " + line + "\n" for line in snippet.splitlines()), end="")
    if ask("Add them?", args.yes):
        sep = "" if not current or current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
        write_atomic(args.file, current + sep + snippet)
        print("Added.")
    else:
        print("Skipped. Paste them into any CLAUDE.md by hand.")
    return 0


def selftest():
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    block = {"SessionStart": [{"hooks": [{"type": "command", "command": "python3 ~/sn/hooks/reg.py"}]}],
             "Notification": [{"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "python3 ~/sn/hooks/reg.py"}]}]}
    mine = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}, "model": "opus"}
    merged, added = merge_hooks(mine, block)
    check("adds each new command", len(added) == 2)
    check("keeps the user's own hooks and settings",
          merged["model"] == "opus" and merged["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "mine.sh")
    check("does not mutate its input", len(mine["hooks"]) == 1)
    again, added2 = merge_hooks(merged, block)
    check("a second run adds nothing", added2 == [] and again == merged)
    _, other = merge_hooks({"hooks": {"Notification": [{"matcher": "permission_prompt",
                                                         "hooks": [{"command": "python3 ~/sn/hooks/reg.py"}]}]}}, block)
    check("same command under another matcher still counts as new", ("Notification", "idle_prompt", "python3 ~/sn/hooks/reg.py") in other)
    e1, ea, ek = merge_env({"env": {"KEEP": "1"}}, [("SESSION_NOTES", "/x/sn")])
    check("env: a new key is added, others kept", ea == [("SESSION_NOTES", "/x/sn")] and e1["env"]["KEEP"] == "1")
    e2, ea2, ek2 = merge_env(e1, [("SESSION_NOTES", "/x/sn")])
    check("env: a second run adds nothing", ea2 == [] and ek2 == [] and e2 == e1)
    e3, ea3, ek3 = merge_env(e1, [("SESSION_NOTES", "/elsewhere")])
    check("env: a different existing value is kept and reported",
          e3["env"]["SESSION_NOTES"] == "/x/sn" and ek3 == [("SESSION_NOTES", "/x/sn")] and ea3 == [])
    try:
        merge_hooks({"hooks": []}, block)
        check("a non-object hooks is refused", False)
    except ValueError:
        check("a non-object hooks is refused", True)

    with tempfile.TemporaryDirectory() as d:
        sp = os.path.join(d, "settings.json")
        with open(sp, "w") as fh:
            json.dump(mine, fh)
        a = argparse.Namespace(settings=sp, json=json.dumps(block), env=["SESSION_NOTES=/x/sn"], yes=True)
        cmd_hooks(a)
        with open(sp) as fh:
            on_disk = json.load(fh)
        check("hooks: written on a yes, with a backup", len(on_disk["hooks"]["SessionStart"]) == 2 and os.path.exists(sp + BACKUP))
        check("hooks: env written with them", on_disk.get("env", {}).get("SESSION_NOTES") == "/x/sn")
        with open(sp, "w") as fh:
            fh.write("{broken")
        check("hooks: invalid JSON is refused, file untouched", cmd_hooks(a) == 2 and open(sp).read() == "{broken")
        sys.stdin, saved = open(os.devnull), sys.stdin
        try:
            np = os.path.join(d, "none.json")
            cmd_hooks(argparse.Namespace(settings=np, json=json.dumps(block), env=None, yes=False))
            check("hooks: no answer means no, nothing written", not os.path.exists(np))
            cp = os.path.join(d, "CLAUDE.md")
            with open(cp, "w") as fh:
                fh.write("# mine\n")
            sn = os.path.join(d, "snippet.md")
            with open(sn, "w") as fh:
                fh.write("## Session tools (session-notes)\n- a line\n")
            cmd_claude_md(argparse.Namespace(file=cp, snippet=sn, yes=False))
            check("claude-md: no answer means no", open(cp).read() == "# mine\n")
        finally:
            sys.stdin = saved
        cmd_claude_md(argparse.Namespace(file=cp, snippet=sn, yes=True))
        text = open(cp).read()
        check("claude-md: appended after a blank line", text == "# mine\n\n## Session tools (session-notes)\n- a line\n")
        cmd_claude_md(argparse.Namespace(file=cp, snippet=sn, yes=True))
        check("claude-md: a second run adds nothing", open(cp).read() == text)
        os.chmod(cp, 0o640)
        write_atomic(cp, text + "x\n")
        check("an existing file keeps its mode", (os.stat(cp).st_mode & 0o777) == 0o640)
        fresh = os.path.join(d, "fresh.md")
        write_atomic(fresh, "a\n")
        check("a new file is not left 0600", (os.stat(fresh).st_mode & 0o777) != 0o600)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


def main(argv=None):
    if argv is None and "--selftest" in sys.argv[1:]:
        return selftest()
    ap = argparse.ArgumentParser(description="Make the two edits install.sh offers, each only on a yes.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hooks")
    h.add_argument("--settings", required=True)
    h.add_argument("--json", required=True)
    h.add_argument("--env", action="append", metavar="KEY=VALUE")
    h.add_argument("--yes", action="store_true")
    c = sub.add_parser("claude-md")
    c.add_argument("--file", required=True)
    c.add_argument("--snippet", required=True)
    c.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    return cmd_hooks(args) if args.cmd == "hooks" else cmd_claude_md(args)


if __name__ == "__main__":
    sys.exit(main())
