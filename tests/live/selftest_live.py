#!/usr/bin/env python3
"""selftest_live.py -- run session-notes in REAL Claude Code and Codex CLI sessions, in a sandbox.

`make check` runs unit --selftests; nothing there starts a real CLI. This does: it clones the
code under test (this checkout's committed HEAD) into a throwaway sandbox, installs it the way
a new user would (install.sh, then install.sh --codex), launches real sessions with a sandboxed
HOME / CLAUDE_CONFIG_DIR / CODEX_HOME / tmux server, and prints a pass/fail table
(feature x CLI). It catches what unit tests cannot: a hooks.json naming scripts in a deleted
worktree ("Blocked by hook"), a hook that never reaches the model, a pane that cannot be typed
into.

    make selftest-live
    python3 tests/live/selftest_live.py [--only claude|codex] [--feature boot,bus,...] [--keep]
    python3 tests/live/selftest_live.py --selftest      # pure logic, no CLI, no network

Cost and auth. Claude panes run `--model haiku`, Codex `-m gpt-6.2-sol` at low effort, with tiny
prompts and a timeout per pane. Both authenticate with API keys read from the macOS Keychain
(never the seat login):
    SELFTEST_CLAUDE_KEYCHAIN_SERVICE   default session-notes-selftest
    SELFTEST_OPENAI_KEYCHAIN_SERVICE   default session-notes-selftest-openai
    SELFTEST_CLAUDE_BIN / SELFTEST_CODEX_BIN   the real binaries (default: first non-script on PATH)
    SELFTEST_CLAUDE_MODEL (haiku) / SELFTEST_CODEX_MODEL (gpt-6.2-sol)
A CLI whose key is missing has its rows reported SKIP. Codex runs with
--dangerously-bypass-hook-trust (a fresh CODEX_HOME silently skips untrusted hooks), so this
suite does not cover Codex hook trust.

Safety. Every child gets an explicit environment (env -i style): HOME, CLAUDE_CONFIG_DIR,
CODEX_HOME, TMUX_TMPDIR and SESSION_NOTES_CONFIG all point into the sandbox. tmux is addressed
only through the sandbox TMUX_TMPDIR (the tools under test call a bare `tmux`, so a -L name
would not reach them); the socket path is asserted to sit inside the sandbox before any tool
that enumerates panes runs, and the run aborts otherwise. launchctl and gh are stubs on the
sandbox PATH. The "real dirs untouched" row hashes the real install targets before and after
and fails if any file written during the run mentions the sandbox. API keys are redacted from
every captured log, and the sandbox Codex auth.json is wiped at teardown.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.realpath(__file__))
SRC_REPO = os.path.dirname(os.path.dirname(HERE))

KEY_RE = re.compile(r"sk-[A-Za-z0-9_*-]{20,}")
_SECRETS = []  # exact key values this run fetched; redacted wherever they appear
EM = chr(0x2014)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# feature id, title, scope. "both" rows are CLI-independent and run once.
FEATURES = [
    ("boot", "boot context + clock line", "cli"),
    ("bus", "bus send, delivered next prompt", "cli"),
    ("peers", "peers lists the panes", "cli"),
    ("bcast", "bcast types into panes", "cli"),
    ("handoff", "handoff child pane runs", "cli"),
    ("refresh", "refresh reads the store", "cli"),
    ("reflect", "reflect writes the store", "cli"),
    ("emdash", "em dash guard fires", "cli"),
    ("checkout", "checkout guard refuses main-branch edit", "cli"),
    ("wt", "wt refuses merge without APPROVE", "both"),
    ("hookpath", "registered hook paths exist", "both"),
    ("review", "review (bin/review.py)", "both"),
    ("live", "live-context (bin/live.py)", "both"),
    ("env", "env (bin/environment_render.py)", "both"),
    ("realdirs", "real dirs untouched", "both"),
]


def redact(s):
    s = KEY_RE.sub("sk-REDACTED", s or "")
    for secret in _SECRETS:
        if secret and len(secret) >= 8:
            s = s.replace(secret, "REDACTED")
    return s


# ---------------------------------------------------------------------------- pure logic

def hook_commands(settings=None, hooks_json=None):
    """Every hook command string registered in a Claude settings.json and/or Codex hooks.json."""
    out = []
    for doc in (settings, hooks_json):
        hooks = (doc or {}).get("hooks") or {}
        for groups in hooks.values():
            for g in groups or []:
                for h in (g or {}).get("hooks") or []:
                    if isinstance(h, dict) and h.get("type", "command") == "command" and h.get("command"):
                        out.append(h["command"])
    return out


def check_hook_paths(commands, home, run_launcher=None):
    """Problems (list of strings) with registered hook commands: every script path a command
    names, and every launcher it goes through, must exist. `run_launcher(path)` returns the
    checkout the launcher points at (`session-notes repo`) or None; with it, the hooks/<name>.py
    that a `<launcher> hook <name>` runs is checked too."""
    problems = []
    for cmd in commands:
        try:
            toks = shlex.split(cmd)
        except ValueError:
            problems.append("cannot resolve hook path: %s" % cmd)
            continue
        resolved = False
        for i, t in enumerate(toks):
            if t.startswith("~/"):
                t = os.path.join(home, t[2:])
            elif t.startswith("$HOME/"):
                t = os.path.join(home, t[6:])
            if not t.startswith("/"):
                continue
            if t.endswith(".py"):
                resolved = True
                if not os.path.isfile(t):
                    problems.append("missing script %s (in: %s)" % (t, cmd))
            elif i + 2 < len(toks) and toks[i + 1] == "hook" and os.path.basename(t) == "session-notes":
                resolved = True
                if not os.path.isfile(t):
                    problems.append("missing launcher %s (in: %s)" % (t, cmd))
                elif run_launcher:
                    checkout = run_launcher(t)
                    name = toks[i + 2]
                    if not checkout:
                        problems.append("launcher %s did not name a checkout" % t)
                    elif not os.path.isfile(os.path.join(checkout, "hooks", name + ".py")):
                        problems.append("launcher %s -> %s has no hooks/%s.py" % (t, checkout, name))
        if not resolved:
            problems.append("cannot resolve hook path: %s" % cmd)
    return problems


def assistant_strings(obj, in_assistant=False):
    """Strings inside assistant messages of one transcript line (Claude and Codex shapes)."""
    out = []
    if isinstance(obj, dict):
        is_a = in_assistant or obj.get("role") == "assistant" or obj.get("type") == "assistant"
        for v in obj.values():
            out.extend(assistant_strings(v, is_a))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(assistant_strings(v, in_assistant))
    elif isinstance(obj, str) and in_assistant:
        out.append(obj)
    return out


def transcript_text(root, assistant_only=True):
    """Concatenated text of every *.jsonl under root (assistant messages only by default)."""
    chunks = []
    for f in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    if not assistant_only:
                        chunks.append(ln)
                        continue
                    try:
                        chunks.extend(assistant_strings(json.loads(ln)))
                    except ValueError:
                        pass
        except OSError:
            pass
    return "\n".join(chunks)


def clock_ok(text, now=None, slack_min=5):
    """True when `text` has NOW=<...YYYY-MM-DD HH:MM...> within `slack_min` minutes of `now`
    (the stamp time_sense.py emits: [now: Sun 2026-11-22 14:51 PST]). A bare date is not enough."""
    m = re.search(r"NOW=[^\n]*?(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})", text or "")
    if not m:
        return False
    try:
        stamp = datetime.strptime("%s %s:%s" % m.groups(), "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return abs((stamp - (now or datetime.now())).total_seconds()) <= slack_min * 60


def _strings(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)
    elif isinstance(o, str):
        yield o


def _strip_heredocs(text):
    """Drop heredoc bodies, so a stage call whose body mentions a command line is not that command."""
    out, end = [], None
    for ln in text.splitlines():
        if end is not None:
            if ln.strip() == end:
                end = None
            continue
        out.append(ln)
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", ln)
        if m:
            end = m.group(1)
    return "\n".join(out)


def call_commands(item):
    """Command lines a Codex call item runs. Handles function_call (`arguments` JSON: a `command`
    argv list or a `cmd` string) and custom_tool_call (`input`: JS calling tools.exec_command with
    a `cmd:` string). Unknown shapes yield nothing."""
    cmds = []
    args = item.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    if isinstance(args, dict):
        c = args.get("command", args.get("cmd"))
        if isinstance(c, list) and all(isinstance(x, str) for x in c):
            cmds.append(shlex.join(c))
        elif isinstance(c, str):
            cmds.append(c)
    src = item.get("input")
    if isinstance(src, str):
        for m in re.finditer(r"cmd\s*:\s*(\"(?:[^\"\\\\]|\\\\.)*\"|`[^`]*`)", src):
            lit = m.group(1)
            if lit.startswith('"'):
                try:
                    cmds.append(json.loads(lit))
                except ValueError:
                    pass
            else:
                cmds.append(lit[1:-1])
    return cmds


def is_codex_save_commit(cmd, _depth=0):
    """True when a command line IS `python3 <path>/bin/codex_save.py commit`: argv of exactly that
    shape (env assignments and a /bin/sh -c wrapper allowed), last argument exactly `commit`."""
    for ln in _strip_heredocs(cmd).splitlines():
        try:
            toks = shlex.split(ln)
        except ValueError:
            continue
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]
        if len(toks) >= 3 and os.path.basename(toks[0]) in ("zsh", "bash", "sh") and toks[1] in ("-c", "-lc") and _depth < 2:
            if is_codex_save_commit(toks[2], _depth + 1):
                return True
            continue
        if (len(toks) == 3 and os.path.basename(toks[0]).startswith("python3")
                and toks[1].endswith("bin/codex_save.py") and toks[2] == "commit"):
            return True
    return False


def codex_commit_proven(root):
    """True when a rollout under root holds a CALL item that runs `codex_save.py commit` AND the
    paired output item (same call_id) printed codex_save's release line. Outputs (including a skill
    file that merely quotes the command) and message text never count."""
    calls, outputs = set(), {}
    for f in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    it = o.get("payload") if isinstance(o, dict) and isinstance(o.get("payload"), dict) else o
                    if not isinstance(it, dict) or not it.get("call_id"):
                        continue
                    t = str(it.get("type", ""))
                    if t.endswith("_output"):
                        outputs.setdefault(it["call_id"], []).extend(_strings(it.get("output")))
                    elif "call" in t and any(is_codex_save_commit(c) for c in call_commands(it)):
                        calls.add(it["call_id"])
        except OSError:
            pass
    return any("released the reflect lock" in "\n".join(outputs.get(c, [])) for c in calls)


def seen(token, text):
    """True when `token` is in `text`, ignoring whitespace (a model may split a token across a space)."""
    return token in re.sub(r"\s+", "", text or "")


def render_table(results):
    """results: dicts with feature/title/cli (claude|codex|both)/status/detail. -> table text."""
    by = {}
    for r in results:
        by.setdefault(r["feature"], {})[r["cli"]] = r
    rows = [("feature", "claude", "codex")]
    for fid, title, _scope in FEATURES:
        if fid not in by:
            continue
        cells = []
        for cli in ("claude", "codex"):
            r = by[fid].get(cli) or by[fid].get("both")
            if not r:
                cells.append("-")
                continue
            s = r["status"]
            if s == SKIP and r.get("detail"):
                s = "SKIP (%s)" % r["detail"]
            cells.append(s + (" [both]" if r["cli"] == "both" else ""))
        rows.append((title, cells[0], cells[1]))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = []
    for n, r in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def fail_lines(results):
    return ["FAIL %s/%s: %s%s" % (r["feature"], r["cli"], r.get("detail", ""),
                                  ("  (evidence: %s)" % r["evidence"]) if r.get("evidence") else "")
            for r in results if r["status"] == FAIL]


def real_targets(real_home):
    """Real install targets: files are hashed, directories listed, before and after a run."""
    j = os.path.join
    files = [j(real_home, ".claude", "settings.json"), j(real_home, ".claude", "CLAUDE.md"),
             j(real_home, ".codex", "hooks.json"), j(real_home, ".codex", "config.toml"),
             j(real_home, ".codex", "AGENTS.md"), j(real_home, ".local", "bin", "session-notes")]
    dirs = [j(real_home, ".claude", "skills"), j(real_home, ".codex", "skills"),
            j(real_home, "Library", "LaunchAgents"), j(real_home, ".config", "session-notes")]
    return files, dirs


def snapshot(real_home):
    files, dirs = real_targets(real_home)
    snap = {}
    for f in files:
        try:
            with open(f, "rb") as fh:
                snap[f] = hashlib.sha256(fh.read()).hexdigest()
        except FileNotFoundError:
            snap[f] = None
        except OSError:
            snap[f] = "unreadable"
    for d in dirs:
        try:
            snap[d] = sorted(os.listdir(d))
        except OSError:
            snap[d] = None
    return snap


def compare_snapshots(before, after):
    return ["%s changed" % k for k in before if before[k] != after.get(k)]


def fatal_diffs(before, after, needles):
    """compare_snapshots, minus hashed FILES whose new content does not mention the sandbox or run
    token: a real settings.json churns under other live sessions, while an install that leaked
    into it would name the sandbox. Only a file present and readable both times can be benign; an
    appearing, disappearing or unreadable one, and any directory listing change, is fatal.
    -> (fatal, benign)."""
    fatal, benign = [], []
    for k in before:
        b, a = before[k], after.get(k)
        if b == a:
            continue
        if isinstance(b, list) or isinstance(a, list):
            fatal.append("%s changed" % k)
        elif b is None or a is None:
            fatal.append("%s %s" % (k, "appeared" if b is None else "disappeared"))
        elif b == "unreadable" or a == "unreadable":
            fatal.append("%s unreadable, cannot be judged" % k)
        else:
            try:
                with open(k, "rb") as fh:
                    data = fh.read()
            except OSError:
                fatal.append("%s unreadable, cannot be judged" % k)
                continue
            (fatal if any(n.encode() in data for n in needles if n) else benign).append("%s changed" % k)
    return fatal, benign


def scan_roots(real_home):
    """Where a leak from the sandbox would land: install targets, the session-notes state and
    stores (the bus and bcast ledgers are .jsonl), memory stores, skills. Whole transcript trees
    (~/.claude/projects/*/*.jsonl, ~/.codex/sessions) are left out because the parent session's own
    transcript legitimately prints sandbox paths; the memory stores under projects/ are in."""
    j = os.path.join
    roots = [j(real_home, ".claude", n) for n in ("settings.json", "CLAUDE.md", "skills", "session-notes",
                                                    "state", "skill-feedback")]
    roots += glob.glob(j(real_home, ".claude", "projects", "*", "memory"))
    roots += [j(real_home, ".codex", n) for n in ("hooks.json", "config.toml", "AGENTS.md", "rules",
                                                    "skills", "session-notes")]
    roots += [j(real_home, ".config", "session-notes"), j(real_home, ".local", "bin"),
              j(real_home, "Library", "LaunchAgents")]
    return roots


def scan_for_sandbox(real_home, needles, since, max_bytes=20_000_000):
    """Files under scan_roots, modified at or after `since`, that mention any of `needles`
    (.jsonl included: the bus and bcast ledgers are the likeliest leak targets)."""
    if isinstance(needles, str):
        needles = [needles]
    pats = [n.encode() for n in needles if n]
    hits = []
    for root in scan_roots(real_home):
        files = [root] if os.path.isfile(root) else [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs]
        for p in files:
            try:
                st = os.stat(p)
                if st.st_mtime < since or st.st_size > max_bytes or not os.path.isfile(p):
                    continue
                with open(p, "rb") as fh:
                    data = fh.read()
                if any(x in data for x in pats):
                    hits.append(p)
            except OSError:
                pass
    return hits


def emdash_flagged(hook_stdout):
    """True when em_dash_guard's output is the flag (systemMessage + additionalContext)."""
    try:
        d = json.loads(hook_stdout)
    except ValueError:
        return False
    ctx = (d.get("hookSpecificOutput") or {}).get("additionalContext", "")
    return "em-dash guard" in (d.get("systemMessage") or "") and "EM-DASH GUARD" in ctx


# ---------------------------------------------------------------------------- wt row (no CLI)

GH_STUB = r'''#!/usr/bin/env python3
# gh stub for the live selftest: records every call, answers from scenario.json. No network.
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "gh-calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
try:
    sc = json.load(open(os.path.join(here, "scenario.json")))
except OSError:
    sc = {}
if args[:2] == ["auth", "token"]:
    print("faketoken")
elif args[:2] == ["pr", "list"]:
    print(json.dumps(sc.get("pr_list", [])))
elif args[:2] == ["pr", "create"]:
    print(sc.get("pr_url", "https://example.invalid/o/r/pull/7"))
elif args[:2] == ["pr", "view"]:
    print(json.dumps(sc.get("view", {})))
elif args[:2] == ["api", "user"]:
    print(sc.get("login", "tester"))
elif args[:1] == ["api"]:
    print(json.dumps(sc.get("comments", [])))
elif args[:2] == ["pr", "merge"]:
    print("merged (stub)")
else:
    sys.stderr.write("gh stub: unhandled %r\n" % (args,))
    sys.exit(1)
'''


def git_env(home):
    return {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "LANG": "en_US.UTF-8"}


def sh(args, cwd, env, check=True, timeout=60):
    r = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError("%s failed rc=%d: %s" % (" ".join(args[:4]), r.returncode, (r.stderr or r.stdout)[-300:]))
    return r


def make_origin_repo(parent, name, env):
    """A local bare origin with one tracked file, plus a main checkout cloned from it."""
    bare, seed, main = (os.path.join(parent, n) for n in (name + "-origin.git", name + "-seed", name))
    sh(["git", "init", "-q", "--bare", "-b", "main", bare], parent, env)
    sh(["git", "clone", "-q", bare, seed], parent, env)
    with open(os.path.join(seed, "tracked.txt"), "w") as fh:
        fh.write("original\n")
    sh(["git", "add", "."], seed, env)
    sh(["git", "commit", "-q", "-m", "seed", "--no-verify"], seed, env)
    sh(["git", "push", "-q", "origin", "HEAD:main"], seed, env)
    sh(["git", "clone", "-q", bare, main], parent, env)
    return main


def wt_row(wt_py, tmp):
    """wt new / pr / merge against a local bare origin with a stubbed gh. (status, detail)."""
    home = os.path.join(tmp, "wthome")
    os.makedirs(home, exist_ok=True)
    stubdir = os.path.join(tmp, "ghstub")
    os.makedirs(stubdir, exist_ok=True)
    gh = os.path.join(stubdir, "gh")
    with open(gh, "w") as fh:
        fh.write(GH_STUB)
    os.chmod(gh, 0o755)
    cfg = os.path.join(tmp, "wt.toml")
    with open(cfg, "w") as fh:
        fh.write('[wt]\ntoken_command = "echo faketoken"\nroot = "%s/wts"\n' % tmp)
    env = git_env(home)
    env.update(SESSION_NOTES_CONFIG=cfg, WT_GH=gh)
    try:
        main = make_origin_repo(tmp, "proj", env)
        wt = [sys.executable, wt_py]
        r = sh(wt + ["new", "feat"], main, env)
        path = r.stdout.strip().splitlines()[-1]
        with open(os.path.join(path, "tracked.txt"), "a") as fh:
            fh.write("v2\n")
        sh(["git", "commit", "-qam", "change", "--no-verify"], path, env)
        r = sh(wt + ["pr", "--title", "t"], path, env, check=False)
        if r.returncode != 0:
            return FAIL, "wt pr failed rc=%d: %s" % (r.returncode, (r.stderr or r.stdout)[-200:])
        head = sh(["git", "rev-parse", "HEAD"], path, env).stdout.strip()
        view = {"number": 7, "headRefOid": head, "state": "OPEN", "url": "https://example.invalid/o/r/pull/7"}

        def scenario(comments):
            with open(os.path.join(stubdir, "scenario.json"), "w") as fh:
                json.dump({"view": view, "login": "tester", "comments": comments}, fh)

        def comment(verdict, sha):
            return {"id": 1, "body": "Review verdict: %s %s\n\nfindings" % (verdict, sha),
                    "user": {"login": "tester"}, "created_at": "2026-01-01T00:00:01Z",
                    "updated_at": "2026-01-01T00:00:01Z"}

        scenario([])
        r1 = sh(wt + ["merge", "feat"], path, env, check=False)
        if r1.returncode != 6:  # stop here: a merge that went through may have removed the worktree
            return FAIL, "merge with no verdict: rc=%d (want 6): %s" % (r1.returncode, (r1.stderr or r1.stdout)[-160:])
        scenario([comment("APPROVE", "0" * 40)])
        r2 = sh(wt + ["merge", "feat"], path, env, check=False)
        if r2.returncode != 6:
            return FAIL, "merge with a stale-sha verdict: rc=%d (want 6)" % r2.returncode
        scenario([comment("APPROVE", head)])
        r3 = sh(wt + ["merge", "feat", "--dry-run"], path, env, check=False)
        with open(os.path.join(stubdir, "gh-calls.jsonl")) as fh:
            calls = [json.loads(x) for x in fh.read().splitlines()]
        merged = [c for c in calls if c[:2] == ["pr", "merge"]]
        if r1.returncode != 6:
            return FAIL, "merge with no verdict: rc=%d (want 6): %s" % (r1.returncode, (r1.stderr or r1.stdout)[-160:])
        if r2.returncode != 6:
            return FAIL, "merge with a stale-sha verdict: rc=%d (want 6)" % r2.returncode
        if r3.returncode != 0 or "verdict gate passed" not in r3.stdout:
            return FAIL, "positive control (APPROVE at head) did not pass the gate: rc=%d %s" % (
                r3.returncode, (r3.stderr or r3.stdout)[-160:])
        if merged:
            return FAIL, "gh pr merge was called during a refused or dry-run merge: %r" % merged
        if not any(c[:2] == ["pr", "create"] for c in calls):
            return FAIL, "wt pr never called gh pr create"
        return PASS, "no verdict rc6, stale sha rc6, APPROVE at head passes the gate, no pr merge call"
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return FAIL, "wt row error: %s" % exc


# ---------------------------------------------------------------------------- selftest

def selftest():
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + detail))
        if not cond:
            fails.append(name)

    longkey = "sk-" + "ant-api03-abcDEF_123-456*xyzXYZ789"
    check("redact: key-shaped string", longkey not in redact("key %s here" % longkey) and "sk-REDACTED" in redact(longkey))
    check("redact: task-list and ask-me survive", redact("task-list ask-me risk-free") == "task-list ask-me risk-free")
    _SECRETS.append("opaqueSecretValue99")
    check("redact: the exact fetched key value", "opaqueSecretValue99" not in redact("x opaqueSecretValue99 y"))
    _SECRETS.pop()

    # boot: the clock must be a real, current [now: ...] stamp
    nowdt = datetime(2026, 11, 22, 14, 51)
    check("clock: current stamp passes", clock_ok("RULES=x NOW=now: Sun 2026-11-22 14:53 PST", nowdt))
    check("clock: NEGATIVE control, bare date fails", not clock_ok("NOW=Sun 2026-11-22", nowdt))
    check("clock: NEGATIVE control, stale time fails", not clock_ok("NOW=Sun 2026-11-22 13:00 PST", nowdt))
    check("clock: NEGATIVE control, no NOW fails", not clock_ok("2026-11-22 14:51", nowdt))

    # reflect (codex): only an executed commit CALL paired with its release output counts
    def rollout(d, name, *items):
        with open(os.path.join(d, name), "w") as fh:
            for it in items:
                fh.write(json.dumps({"type": "response_item", "payload": it}) + "\n")

    commit_js = 'text(await tools.exec_command({cmd:"python3 /x/repo/bin/codex_save.py commit"}))\n'
    stage_js = ("const r=await tools.exec_command({cmd:`SESSION_NOTES_AGENT=codex python3 /x/repo/bin/codex_save.py "
                "stage --store global --file f.md <<'EOF'\nit was committed\nEOF`})\n")
    out_text = lambda t: [{"type": "input_text", "text": t}]
    release = '{"output":"wrote f.md\\nreleased the reflect lock\\n"}'
    adapter = "run: python3 $SESSION_NOTES/bin/codex_save.py commit"
    with tempfile.TemporaryDirectory() as d:
        rollout(d, "a.jsonl",
                {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "text(await tools.exec_command({cmd:\"cat ADAPTER.md\"}))\n"},
                {"type": "custom_tool_call_output", "call_id": "c1", "output": out_text(adapter + " released the reflect lock")})
        check("commit proof: NEGATIVE control (a), a skill file's output quoting the command is not execution",
              not codex_commit_proven(d))
    with tempfile.TemporaryDirectory() as d:
        rollout(d, "b.jsonl",
                {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": stage_js},
                {"type": "custom_tool_call_output", "call_id": "c1", "output": out_text("staged " + release)})
        check("commit proof: NEGATIVE control (b), a stage call whose heredoc says committed does not match",
              not codex_commit_proven(d))
    with tempfile.TemporaryDirectory() as d:
        rollout(d, "c.jsonl",
                {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": commit_js},
                {"type": "custom_tool_call_output", "call_id": "c1", "output": out_text("python3: warning only")})
        check("commit proof: NEGATIVE control (c), a commit call without the release line fails",
              not codex_commit_proven(d))
    with tempfile.TemporaryDirectory() as d:
        rollout(d, "d.jsonl",
                {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": commit_js},
                {"type": "custom_tool_call_output", "call_id": "c1", "output": out_text(release)})
        check("commit proof (d): custom_tool_call commit + paired release output passes", codex_commit_proven(d))
    with tempfile.TemporaryDirectory() as d:
        rollout(d, "e.jsonl",
                {"type": "function_call", "call_id": "c2", "name": "shell",
                 "arguments": json.dumps({"command": ["/bin/zsh", "-lc", "python3 /x/repo/bin/codex_save.py commit"]})},
                {"type": "function_call_output", "call_id": "c2", "output": release})
        check("commit proof (d): function_call commit + paired release output passes", codex_commit_proven(d))
    with tempfile.TemporaryDirectory() as d:
        rollout(d, "f.jsonl",
                {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": commit_js},
                {"type": "custom_tool_call_output", "call_id": "other", "output": out_text(release)})
        check("commit proof: NEGATIVE control, a release line paired with a different call_id fails",
              not codex_commit_proven(d))

    # hook-path check: positive control, and NEGATIVE control (a copy naming a missing file goes red)
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "repo", "hooks"))
        with open(os.path.join(d, "repo", "hooks", "a.py"), "w") as fh:
            fh.write("")

        def doc(cmd):
            return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}}

        check("hookpath: present script is clean",
              check_hook_paths(hook_commands(doc("python3 %s/repo/hooks/a.py" % d)), d) == [])
        pb = check_hook_paths(hook_commands(doc("python3 %s/deleted-worktree/hooks/a.py" % d)), d)
        check("hookpath: NEGATIVE control, missing script reported", len(pb) == 1 and "deleted-worktree" in pb[0], str(pb))
        check("hookpath: ~ expands against home", len(check_hook_paths(["python3 ~/nothere/x.py"], d)) == 1)
        launcher = os.path.join(d, "session-notes")
        with open(launcher, "w") as fh:
            fh.write("#!/bin/sh\n")
        cx = lambda name: ["SESSION_NOTES_AGENT=codex %s hook %s" % (launcher, name)]
        check("hookpath: launcher + existing hook is clean",
              check_hook_paths(cx("a"), d, lambda p: os.path.join(d, "repo")) == [])
        check("hookpath: launcher naming a missing hook is reported",
              len(check_hook_paths(cx("zzz"), d, lambda p: os.path.join(d, "repo"))) == 1)
        check("hookpath: launcher pointing at a deleted checkout is reported",
              len(check_hook_paths(cx("a"), d, lambda p: os.path.join(d, "gone"))) == 1)
        check("hookpath: NEGATIVE control, unparseable command is a problem",
              check_hook_paths(["python3 'unterminated"], d) == ["cannot resolve hook path: python3 'unterminated"])
        check("hookpath: NEGATIVE control, a command naming no path is a problem",
              len(check_hook_paths(["echo hi"], d)) == 1)
        check("hookpath: missing launcher is reported",
              len(check_hook_paths(["SESSION_NOTES_AGENT=codex %s/nope/session-notes hook a" % d], d)) == 1)

    # transcripts: assistant text only
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "a.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "USERONLY"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "ASSISTANTTOK"}]}}) + "\n")
            fh.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text", "text": "CODEXTOK"}]}}) + "\n")
        t = transcript_text(d)
        check("transcript: assistant text found (claude + codex shapes)", "ASSISTANTTOK" in t and "CODEXTOK" in t)
        check("transcript: user text excluded", "USERONLY" not in t)

    # em dash guard: red and green through the real hook
    guard = os.path.join(SRC_REPO, "hooks", "em_dash_guard.py")
    if os.path.isfile(guard):
        def run_guard(text):
            p = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x/note.md", "content": text}}
            return subprocess.run([sys.executable, guard], input=json.dumps(p), capture_output=True, text=True).stdout
        check("emdash: dash flagged", emdash_flagged(run_guard("a %s b" % EM)))
        check("emdash: NEGATIVE control, clean text is silent", not emdash_flagged(run_guard("a - b")))
        check("emdash: empty output is not a flag", not emdash_flagged(""))

    # real-dirs guard: snapshot/compare and the leak scan go red on a change / a mention
    with tempfile.TemporaryDirectory() as h:
        os.makedirs(os.path.join(h, ".claude", "skills"))
        with open(os.path.join(h, ".claude", "settings.json"), "w") as fh:
            fh.write("{}")
        t0 = time.time() - 1
        s0 = snapshot(h)
        check("realdirs: unchanged -> no diff", compare_snapshots(s0, snapshot(h)) == [])
        needles = ["sn-live-ZZZ", "TOK12345"]
        check("realdirs: no mention -> clean", scan_for_sandbox(h, needles, t0) == [])
        with open(os.path.join(h, ".claude", "settings.json"), "w") as fh:
            fh.write('{"x":1}')
        os.makedirs(os.path.join(h, ".claude", "skills", "new"))
        os.makedirs(os.path.join(h, ".codex", "skills", "newx"))
        check("realdirs: NEGATIVE control, changed file and new skills (claude, codex) reported",
              len(compare_snapshots(s0, snapshot(h))) == 3)
        fat, ben = fatal_diffs(s0, snapshot(h), needles)
        check("realdirs: a changed file without a sandbox mention is benign, new skill dirs are fatal",
              len(fat) == 2 and len(ben) == 1, str((fat, ben)))
        with open(os.path.join(h, ".claude", "settings.json"), "w") as fh:
            fh.write('{"x":"/private/tmp/sn-live-ZZZ/home"}')
        fat, ben = fatal_diffs(s0, snapshot(h), needles)
        check("realdirs: NEGATIVE control, a changed file that names the sandbox is fatal",
              len(fat) == 3 and not ben, str((fat, ben)))
        with open(os.path.join(h, ".claude", "settings.json"), "w") as fh:
            fh.write('{"x":2}')
        bus = os.path.join(h, ".claude", "session-notes", "state", "session-bus")
        os.makedirs(bus)
        with open(os.path.join(bus, "bus.jsonl"), "w") as fh:
            fh.write(json.dumps({"from": "x", "text": "sent TOK12345"}) + "\n")
        check("realdirs: NEGATIVE control, a planted bus.jsonl line with the run token is found",
              scan_for_sandbox(h, needles, t0) == [os.path.join(bus, "bus.jsonl")])
        mem = os.path.join(h, ".claude", "projects", "-x", "memory")
        os.makedirs(mem)
        with open(os.path.join(mem, "m.md"), "w") as fh:
            fh.write("/private/tmp/sn-live-ZZZ/home")
        check("realdirs: NEGATIVE control, a memory-store file naming the sandbox is found",
              len(scan_for_sandbox(h, needles, t0)) == 2)
        tr = os.path.join(h, ".claude", "projects", "-x")
        with open(os.path.join(tr, "t.jsonl"), "w") as fh:
            fh.write("TOK12345 in a parent transcript")
        check("realdirs: a transcript outside the stores is not scanned", len(scan_for_sandbox(h, needles, t0)) == 2)
        check("realdirs: files older than the run are ignored", scan_for_sandbox(h, needles, time.time() + 100) == [])

    # real-dirs: presence flips and unreadable files are fatal, never benign
    with tempfile.TemporaryDirectory() as h:
        os.makedirs(os.path.join(h, ".claude"))
        sj = os.path.join(h, ".claude", "settings.json")
        with open(sj, "w") as fh:
            fh.write("{}")
        before = snapshot(h)
        os.remove(sj)
        fat, ben = fatal_diffs(before, snapshot(h), ["sn-live-ZZZ"])
        check("realdirs: NEGATIVE control, a deleted target is fatal", len(fat) == 1 and not ben and "disappeared" in fat[0], str((fat, ben)))
        fat, ben = fatal_diffs(snapshot(h), {**snapshot(h), sj: "x" * 64}, ["sn-live-ZZZ"])
        check("realdirs: NEGATIVE control, a created target is fatal", len(fat) == 1 and not ben and "appeared" in fat[0], str((fat, ben)))
        with open(sj, "w") as fh:
            fh.write("{}")
        before = snapshot(h)
        with open(sj, "w") as fh:
            fh.write('{"changed":1}')
        os.chmod(sj, 0)
        try:
            after = snapshot(h)
            if os.access(sj, os.R_OK):
                # Root (e.g. a CI container) reads mode-0 files, so chmod cannot make one
                # unreadable there; feed fatal_diffs the snapshot an unreadable file gives.
                after[sj] = "unreadable"
            fat, ben = fatal_diffs(before, after, ["sn-live-ZZZ"])
            check("realdirs: NEGATIVE control, an unreadable target is fatal", len(fat) == 1 and not ben and "unreadable" in fat[0], str((fat, ben)))
        finally:
            os.chmod(sj, 0o600)
        fat, ben = fatal_diffs(before, snapshot(h), ["sn-live-ZZZ"])
        check("realdirs: a readable changed file with no needle is benign", not fat and len(ben) == 1, str((fat, ben)))

    # a rejected Codex login is a FAIL on every Codex row, not a SKIP
    live = Live(argparse.Namespace(only=None, feature=None, keep=False, selftest=False))
    try:
        live.have["codex"], live.login_failed = False, True
        live.do("boot", "codex", lambda cli: (PASS, "", None), "codex")
        r0 = live.results[0] if live.results else {}
        check("codex login failed: row is FAIL naming it", r0.get("status") == FAIL and r0.get("detail") == "codex login failed", str(r0))
        live.results.clear()
        live.login_failed = False
        live.why["codex"] = "keychain item missing"
        live.do("boot", "codex", lambda cli: (PASS, "", None), "codex")
        check("missing keychain item: row is SKIP", live.results[0]["status"] == SKIP, str(live.results))
    finally:
        shutil.rmtree(live.root, ignore_errors=True)

    # teardown always runs: interrupted go() must still wipe the Codex auth.json
    ns = argparse.Namespace(only=None, feature=None, keep=False, selftest=False)
    live = Live(ns)
    try:
        auth = os.path.join(live.codex_dir, "auth.json")
        with open(auth, "w") as fh:
            fh.write("{}")

        def boom():
            raise KeyboardInterrupt()

        live.go = boom
        root = live.root
        _res, _lines, _kept = execute(live)
        check("teardown: auth.json removed when go() raises KeyboardInterrupt", not os.path.exists(auth))
        check("teardown: sandbox removed after an interrupt", not os.path.exists(root) or _kept == root)
        check("teardown: an interrupted run is reported FAIL", any(r["status"] == FAIL for r in _res))
    finally:
        shutil.rmtree(live.root, ignore_errors=True)

    # table
    res = [{"feature": "boot", "title": "boot", "cli": "claude", "status": PASS, "detail": ""},
           {"feature": "boot", "title": "boot", "cli": "codex", "status": FAIL, "detail": "no marker", "evidence": "/x"},
           {"feature": "wt", "title": "wt", "cli": "both", "status": SKIP, "detail": "not landed"}]
    tbl = render_table(res)
    check("table: renders statuses", "PASS" in tbl and "FAIL" in tbl and "SKIP (not landed)" in tbl, tbl)
    check("table: one FAIL line with evidence", fail_lines(res) == ["FAIL boot/codex: no marker  (evidence: /x)"])

    # wt row: the real wt.py, a local bare origin, a stub gh
    wt_py = os.path.join(SRC_REPO, "bin", "wt.py")
    with tempfile.TemporaryDirectory() as d:
        st, detail = wt_row(wt_py, os.path.realpath(d))
        check("wt: refuses without APPROVE, passes with it", st == PASS, detail)
        # red path: a wt.py whose gate always passes must make the row FAIL
        broken = os.path.join(d, "wt_broken.py")
        with open(wt_py) as fh:
            src = fh.read()
        sentinel = "    ordered = sorted(comments"
        check("wt: sentinel for the red-path mutation exists once", src.count(sentinel) == 1)
        with open(broken, "w") as fh:
            fh.write(src.replace(sentinel, "    return True, 'mutated'\n" + sentinel, 1))
        sys.path_importer_cache.clear()
        shutil.copytree(os.path.join(SRC_REPO, "bin"), os.path.join(d, "binc"))
        shutil.copy(broken, os.path.join(d, "binc", "wt.py"))
        r2 = os.path.join(d, "r2")
        os.makedirs(r2)
        st2, detail2 = wt_row(os.path.join(d, "binc", "wt.py"), os.path.realpath(r2))
        check("wt: NEGATIVE control, a gate that always passes is reported FAIL at the merge gate",
              st2 == FAIL and "merge with no verdict" in detail2 and "rc=0" in detail2, detail2)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


# ---------------------------------------------------------------------------- the live run

def find_real_binary(name, override):
    """The real CLI binary (not a shell-script wrapper that switches accounts)."""
    cands = [override] if override else []
    cands += [os.path.join(d, name) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for c in cands:
        if not c or not os.path.isfile(c) or not os.access(c, os.X_OK):
            continue
        real = os.path.realpath(c)
        try:
            with open(real, "rb") as fh:
                if fh.read(2) == b"#!":
                    continue
        except OSError:
            continue
        return real
    return None


def keychain_key(service, keychain, timeout=20):
    """(key, why). why is "" on success, else "keychain item missing" or "keychain locked or
    unreachable" (so a locked keychain is not mistaken for an unconfigured one)."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w", keychain],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None, "keychain locked or unreachable"
    key = r.stdout.strip()
    if r.returncode == 0 and key:
        _SECRETS.append(key)
        return key, ""
    if r.returncode == 44:
        return None, "keychain item missing"
    return None, "keychain locked or unreachable (security rc=%d)" % r.returncode


class Abort(Exception):
    pass


class Live:
    CLAUDE_TOOLS = ["Bash", "Write", "Edit", "Read", "Skill"]

    def __init__(self, a):
        self.a = a
        self.real_home = os.path.expanduser("~")
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="sn-live-", dir="/tmp"))
        j = lambda *p: os.path.join(self.root, *p)
        self.home, self.bin, self.tmuxd, self.repo, self.work, self.logs = (
            j("home"), j("bin"), j("tmux"), j("repo"), j("work"), j("logs"))
        self.claude_dir = os.path.join(self.home, ".claude")
        self.codex_dir = os.path.join(self.home, ".codex")
        self.cfg = j("config.toml")
        for d in (self.home, self.claude_dir, self.codex_dir, self.bin, self.tmuxd, self.work, self.logs):
            os.makedirs(d, exist_ok=True)
        with open(self.cfg, "w") as fh:  # defaults, except where the Claude store lives
            fh.write('[paths]\nglobal_store = "%s/store-claude"\n' % self.root)
        self.results = []
        self.panes = set()
        self.t0 = time.time()
        self.claude_bin = find_real_binary("claude", os.environ.get("SELFTEST_CLAUDE_BIN"))
        self.codex_bin = find_real_binary("codex", os.environ.get("SELFTEST_CODEX_BIN"))
        self.claude_model = os.environ.get("SELFTEST_CLAUDE_MODEL", "haiku")
        self.codex_model = os.environ.get("SELFTEST_CODEX_MODEL", "gpt-6.2-sol")
        self.keychain = os.path.join(self.real_home, "Library", "Keychains", "login.keychain-db")
        self.tok = hashlib.sha256(os.urandom(8)).hexdigest()[:8].upper()
        self.have = {"claude": False, "codex": False}
        self.why = {}
        self.installed = False
        self.before = None
        self.login_failed = False  # a key was found but Codex rejected it: FAIL, not SKIP

    # -- environment ----------------------------------------------------------------------
    def env(self, **extra):
        dirs = [self.bin]
        for t in ("tmux", "git", "python3"):
            p = shutil.which(t)
            if p and os.path.dirname(p) not in dirs:
                dirs.append(os.path.dirname(p))
        dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        e = {"HOME": self.home, "PATH": os.pathsep.join(dirs), "TERM": "xterm-256color",
             "LANG": "en_US.UTF-8", "USER": os.environ.get("USER", "selftest"), "SHELL": "/bin/zsh",
             "TMUX_TMPDIR": self.tmuxd, "CLAUDE_CONFIG_DIR": self.claude_dir, "CODEX_HOME": self.codex_dir,
             "SESSION_NOTES_CONFIG": self.cfg, "DISABLE_AUTOUPDATER": "1",
             "GIT_CONFIG_GLOBAL": os.path.join(self.root, "gitconfig"),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
             "WT_GH": os.path.join(self.bin, "gh")}
        e.update(extra)
        return e

    def run(self, args, log=None, env=None, cwd=None, timeout=120, stdin=None, check=False):
        try:
            r = subprocess.run(args, env=env or self.env(), cwd=cwd or self.work, capture_output=True,
                               text=True, timeout=timeout, input=stdin if stdin is not None else "")
        except subprocess.TimeoutExpired as exc:
            if log:
                self.log(log, "$ %s\nTIMEOUT after %ss\n%s\n" % (" ".join(shlex.quote(x) for x in args), timeout,
                                                                 (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")))
            return subprocess.CompletedProcess(args, 124, "", "timeout")
        if log:
            self.log(log, "$ %s\nrc=%d\n--- stdout\n%s\n--- stderr\n%s\n" % (
                " ".join(shlex.quote(x) for x in args), r.returncode, r.stdout, r.stderr))
        if check and r.returncode != 0:
            raise Abort("%s failed rc=%d (see logs/%s)" % (args[0], r.returncode, log))
        return r

    def log(self, name, text):
        with open(os.path.join(self.logs, name), "a") as fh:
            fh.write(redact(text))

    def record(self, feature, cli, status, detail="", evidence=None):
        title = next(t for f, t, _ in FEATURES if f == feature)
        self.results.append({"feature": feature, "title": title, "cli": cli, "status": status,
                             "detail": detail, "evidence": evidence})

    # -- tmux -----------------------------------------------------------------------------
    def tmux(self, *args, timeout=30):
        return subprocess.run(["tmux"] + list(args), env=self.env(), capture_output=True, text=True,
                              timeout=timeout)

    def assert_sandbox_tmux(self):
        r = self.tmux("display", "-p", "#{socket_path}")
        sock = os.path.realpath(r.stdout.strip()) if r.stdout.strip() else ""
        if r.returncode != 0 or not sock.startswith(self.root + os.sep):
            raise Abort("tmux socket %r is not inside the sandbox %s; refusing to touch it" % (sock, self.root))

    def start_tmux(self):
        r = self.tmux("new-session", "-d", "-s", "keeper", "-x", "180", "-y", "50", "--", "sleep", "86400")
        if r.returncode != 0:
            raise Abort("could not start the sandbox tmux server: %s" % r.stderr.strip())
        self.assert_sandbox_tmux()

    def stop_tmux(self):
        try:
            sock = self.tmux("display", "-p", "#{socket_path}").stdout.strip()
            if sock and os.path.realpath(sock).startswith(self.root + os.sep):
                self.tmux("kill-server")
        except (OSError, subprocess.TimeoutExpired):
            pass

    def launch(self, name, argv, cwd=None, out=None):
        """A pane in the sandbox server under `env -i` with an explicit env (TMUX/TMUX_PANE kept).
        `out` redirects the command's output to a file (for a one-shot command run from a pane)."""
        e = self.env()
        parts = ["exec", "env", "-i"] + [shlex.quote("%s=%s" % kv) for kv in sorted(e.items())]
        parts += ['TMUX="$TMUX"', 'TMUX_PANE="$TMUX_PANE"'] + [shlex.quote(x) for x in argv]
        if out:
            parts += [">", shlex.quote(out), "2>&1"]
        r = self.tmux("new-session", "-d", "-s", name, "-x", "180", "-y", "50", "-c", cwd or self.work,
                      "--", "sh", "-c", " ".join(parts))
        if r.returncode != 0:
            raise Abort("launch %s failed: %s" % (name, r.stderr.strip()))
        self.panes.add(name)

    def pane_run(self, name, argv, logname, timeout=60):
        """Run one command from inside its own sandbox pane (some tools need $TMUX_PANE to know
        who is calling). Returns its redacted output, or None on timeout."""
        out = os.path.join(self.logs, logname)
        self.launch(name, argv, out=out)
        gone = self.wait_for(lambda: self.tmux("has-session", "-t", "=" + name).returncode != 0, timeout, 0.5)
        try:
            with open(out) as fh:
                text = redact(fh.read())
        except OSError:
            text = ""
        return text if gone else None

    def screen(self, name, lines=60):
        r = self.tmux("capture-pane", "-p", "-t", "=%s:" % name, "-S", "-%d" % lines)
        return r.stdout if r.returncode == 0 else ""

    def send(self, name, text):
        """Type a line into a sandbox pane this run created, then Enter."""
        if name not in self.panes:
            raise Abort("refusing to type into pane %r: not created by this run" % name)
        self.tmux("send-keys", "-t", "=%s:" % name, "-l", text)
        time.sleep(0.8)
        self.tmux("send-keys", "-t", "=%s:" % name, "Enter")

    @staticmethod
    def wait_for(cond, timeout, step=1.5):
        end = time.time() + timeout
        while time.time() < end:
            v = cond()
            if v:
                return v
            time.sleep(step)
        return None

    def idle(self, name):
        """True when the pane shows an input box and is not mid-turn."""
        s = self.screen(name, 30)
        if not s.strip() or "interrupt" in s:
            return False
        tail = "\n".join([ln for ln in s.splitlines() if ln.strip()][-8:])
        return "❯" in tail or "›" in tail

    def wait_idle(self, name, timeout=90):
        time.sleep(3)
        return self.wait_for(lambda: self.idle(name), timeout)

    # -- setup ----------------------------------------------------------------------------
    def store_dirs(self):
        # The Claude store sits outside .claude/ (config paths.global_store): Claude treats writes
        # under .claude/ as sensitive and would prompt, which a headless run cannot answer.
        return (os.path.join(self.root, "store-claude"),
                os.path.join(self.codex_dir, "session-notes", "global"))

    def stub_bins(self):
        with open(os.path.join(self.bin, "launchctl"), "w") as fh:
            fh.write('#!/bin/sh\necho "$@" >> "%s/launchctl-calls.txt"\nexit 0\n' % self.logs)
        with open(os.path.join(self.bin, "gh"), "w") as fh:
            fh.write(GH_STUB)
        for n in ("launchctl", "gh"):
            os.chmod(os.path.join(self.bin, n), 0o755)
        for n, real in (("claude", self.claude_bin), ("codex", self.codex_bin)):
            if real:
                os.symlink(real, os.path.join(self.bin, n))

    def clone_and_install(self):
        head = subprocess.run(["git", "-C", SRC_REPO, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.run(["git", "clone", "-q", "--no-hardlinks", SRC_REPO, self.repo], "clone.txt", check=True)
        self.run(["git", "-C", self.repo, "checkout", "-q", head], "clone.txt", check=True)
        # No remote: nothing in the sandbox clone can push or fetch anywhere.
        self.run(["git", "-C", self.repo, "remote", "remove", "origin"], "clone.txt", check=True)
        self.installed = True  # from here on a stray write could reach the real dirs: always check
        self.run(["sh", "install.sh", "--all", "--yes", "--bin-dir", self.bin], "install-claude.txt",
                 cwd=self.repo, check=True)
        self.run(["sh", "install.sh", "--codex", "--yes"], "install-codex.txt", cwd=self.repo, check=True)

    def seed_claude(self):
        sp = os.path.join(self.claude_dir, "settings.json")
        try:
            with open(sp) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            doc = {}
        doc["apiKeyHelper"] = "security find-generic-password -s %s -w %s" % (
            shlex.quote(os.environ.get("SELFTEST_CLAUDE_KEYCHAIN_SERVICE", "session-notes-selftest")),
            shlex.quote(self.keychain))
        perm = doc.setdefault("permissions", {})
        perm["deny"] = list(perm.get("deny", [])) + [
            "Read(%s/.config/secrets/**)" % self.real_home, "Bash(security:*)", "Bash(/usr/bin/security:*)"]
        doc.setdefault("env", {})["DISABLE_AUTOUPDATER"] = "1"
        with open(sp, "w") as fh:
            json.dump(doc, fh, indent=2)
        env = git_env(self.home)
        self.guard_repo = make_origin_repo(self.root, "guardproj", env)
        trusted = set()
        for p in (self.work, self.repo, self.guard_repo):
            trusted |= {p, os.path.realpath(p)}
        with open(os.path.join(self.claude_dir, ".claude.json"), "w") as fh:
            json.dump({"hasCompletedOnboarding": True, "theme": "dark",
                       "projects": {p: {"hasTrustDialogAccepted": True, "allowedTools": []} for p in trusted}},
                      fh, indent=2)
        gstore, _ = self.store_dirs()
        os.makedirs(gstore, exist_ok=True)
        with open(os.path.join(gstore, "MEMORY.md"), "w") as fh:
            fh.write("# Memory Index\n\n- [Marker](marker.md) - the selftest marker word is CLAUDEMARK%s\n" % self.tok)
        # No store @import is written here: what reaches a Claude session at boot is judged only
        # through what install.sh wired (the standing-rules import and the hooks).

    def seed_codex(self):
        _, gstore = self.store_dirs()
        os.makedirs(gstore, exist_ok=True)
        with open(os.path.join(gstore, "MEMORY.md"), "w") as fh:
            fh.write("# Memory Index\n\n- [Marker](marker.md) - the selftest marker word is CODEXMARK%s\n" % self.tok)
        with open(os.path.join(self.codex_dir, "config.toml"), "a") as fh:
            for p in sorted({self.work, os.path.realpath(self.work)}):
                fh.write('\n[projects."%s"]\ntrust_level = "trusted"\n' % p)

    def codex_login(self, key):
        r = subprocess.run([self.codex_bin, "--no-daemon", "login", "--with-api-key"], env=self.env(),
                           input=key + "\n", capture_output=True, text=True, timeout=60)
        self.log("codex-login.txt", "rc=%d\n%s\n%s" % (r.returncode, r.stdout, r.stderr))
        return r.returncode == 0 and os.path.isfile(os.path.join(self.codex_dir, "auth.json"))

    # -- CLI invocations --------------------------------------------------------------------
    def claude_argv(self, prompt, name=None, headless=False):
        a = [self.claude_bin]
        if headless:
            a += ["-p", "--max-budget-usd", "0.60"]
        if name:
            a += ["-n", name]
        return a + ["--model", self.claude_model, "--allowedTools"] + self.CLAUDE_TOOLS + ["--", prompt]

    def codex_headless(self, prompt, log, sandbox="workspace-write", cwd=None, timeout=240):
        argv = [self.codex_bin, "--no-daemon", "exec", "--dangerously-bypass-hook-trust",
                "-m", self.codex_model, "-c", "model_reasoning_effort=low", "-s", sandbox,
                "--skip-git-repo-check", prompt]
        return self.run(argv, log, env=self.env(), cwd=cwd, timeout=timeout)

    def codex_argv(self, prompt):
        return [self.codex_bin, "--no-daemon", "--dangerously-bypass-hook-trust", "-m", self.codex_model,
                "-c", "model_reasoning_effort=low", "-a", "never", "-s", "workspace-write", prompt]

    def ctext(self, cli, assistant_only=True):
        root = os.path.join(self.claude_dir, "projects") if cli == "claude" else os.path.join(self.codex_dir, "sessions")
        return transcript_text(root, assistant_only)

    def scoped(self, cli, tok):
        """A temp dir of symlinks to only those transcripts that contain `tok` (this row's own
        sessions: the token is in their prompt). Caller removes it."""
        root = os.path.join(self.claude_dir, "projects") if cli == "claude" else os.path.join(self.codex_dir, "sessions")
        d = tempfile.mkdtemp(dir=self.root)
        for n, f in enumerate(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    if tok in fh.read():
                        os.symlink(f, os.path.join(d, "t%d.jsonl" % n))
            except OSError:
                pass
        return d

    def hook_injected(self, cli, tok):
        root = os.path.join(self.claude_dir, "projects") if cli == "claude" else os.path.join(self.codex_dir, "sessions")
        for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    if any(tok in ln and "VERBATIM PEER TEXT" in ln for ln in fh):
                        return True
            except OSError:
                pass
        return False

    def count_arrivals(self, cli, tok):
        root = os.path.join(self.claude_dir, "projects") if cli == "claude" else os.path.join(self.codex_dir, "sessions")
        n = 0
        for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    if any(tok in ln and "via bcast" in ln for ln in fh):
                        n += 1
            except OSError:
                pass
        return n

    def evid(self, name):
        return os.path.join(self.logs, name)

    # -- rows -------------------------------------------------------------------------------
    def row_boot(self, cli):
        now_part = ("NOW=<the text inside the square brackets of the line in your context that starts with "
                    "[now: (copy its weekday, date, time and zone)>")
        if cli == "claude":
            # What reaches Claude through session-notes: install.sh's standing-rules @import in
            # CLAUDE.md (heading below) and the time_sense hook. No runner-written import is judged.
            p = ("Reply with exactly one line of the form RULES=<heading> %s. <heading> is the text of the "
                 "first heading of the standing rules in your instructions." % now_part)
            r = self.run(self.claude_argv(p, headless=True), "boot-claude.txt", timeout=180)
            text = r.stdout
            if r.returncode != 0:
                return FAIL, "claude -p rc=%d" % r.returncode, "boot-claude.txt"
            if "Standing rules (session-notes)" not in text:
                return FAIL, "the install-wired standing rules did not reach the model", "boot-claude.txt"
        else:
            p = ("Reply with exactly one line of the form MARK=<word> %s. <word> is the selftest marker word "
                 "from your session memory." % now_part)
            r = self.codex_headless(p, "boot-codex.txt")
            text, mark = r.stdout, "CODEXMARK" + self.tok
            if r.returncode != 0:
                return FAIL, "codex exec rc=%d" % r.returncode, "boot-codex.txt"
            if mark not in text:
                return FAIL, "marker %s not in the reply (the SessionStart hook did not deliver it)" % mark, "boot-codex.txt"
        if not clock_ok(text):
            return FAIL, "no [now: ...] stamp within 5 minutes of the wall clock in the reply (time_sense hook dead or stale): %r" % text.strip()[-120:], "boot-%s.txt" % cli
        return PASS, "%s and a [now: ...] stamp within 5 minutes of the wall clock" % (
            "install-wired standing rules" if cli == "claude" else "SessionStart marker"), None

    @staticmethod
    def pane_pair(cli):
        return ("cA", "cB") if cli == "claude" else ("xA", "xB")

    def row_peers(self, cli):
        a, b = self.pane_pair(cli)
        self.assert_sandbox_tmux()
        r = self.run([sys.executable, os.path.join(self.repo, "bin", "session_peers.py"), "--all", "--json"],
                     "peers-%s.txt" % cli)
        try:
            roster = json.loads(r.stdout)
        except ValueError:
            return FAIL, "session_peers --json did not print JSON", "peers-%s.txt" % cli
        found = {p.get("tmux_session"): p for p in roster}
        bad = [n for n in (a, b) if not (found.get(n) or {}).get("is_agent") or (found[n].get("harness") != cli)]
        if bad:
            return FAIL, "panes %s not listed as %s agents (roster: %s)" % (
                bad, cli, sorted((k, v.get("harness")) for k, v in found.items())), "peers-%s.txt" % cli
        return PASS, "both sandbox panes listed as %s agents" % cli, None

    def row_bcast(self, cli):
        a, b = self.pane_pair(cli)
        self.assert_sandbox_tmux()
        tok = "BCAST%s%s" % (self.tok, cli[:2].upper())
        line = "Reply with only the word %s" % tok
        for n in (a, b):
            self.wait_idle(n)
        r = self.run([sys.executable, os.path.join(self.repo, "bin", "bcast.py"), "--to", "%s,%s" % (a, b), line],
                     "bcast-%s.txt" % cli, timeout=60)
        # Proof is arrival: the attributed line shows up as a submitted user message in each pane's
        # own transcript. A model may rightly decline to act on a broadcast, so obedience is not the test.
        if not self.wait_for(lambda: self.count_arrivals(cli, tok) >= 2, 120):
            return FAIL, "the broadcast line reached fewer than 2 panes' transcripts (bcast: %s)" % (
                (r.stdout + r.stderr).strip()[-150:]), "bcast-%s.txt" % cli
        return PASS, "attributed line typed into and submitted in both panes", None

    def row_bus(self, cli):
        a, _ = self.pane_pair(cli)
        tok = "BUS%s%s" % (self.tok, cli[:2].upper())
        self.wait_idle(a)
        # session_bus send needs $TMUX_PANE (the sender's address), so it runs from its own pane.
        out = self.pane_run("busSend" + cli[:2], [sys.executable, os.path.join(self.repo, "bin", "session_bus.py"),
                            "send", "--to", a, "Include the word %s in your next reply." % tok], "bus-%s.txt" % cli)
        if out is None or "cannot send" in out or "error" in out.lower():
            return FAIL, "session_bus send failed: %s" % (out or "timeout").strip()[-160:], "bus-%s.txt" % cli
        time.sleep(2)
        self.send(a, "What is 2+2? Answer in one short line.")  # neutral: the token can only come via the hook
        # Proof is the hook's own injection in the recipient's transcript (the fenced peer-text frame
        # carrying the token). The typed prompt is neutral and never mentions mail; a model may
        # rightly decline to act on peer text, so obedience is not the test.
        if not self.wait_for(lambda: self.hook_injected(cli, tok), 120):
            return FAIL, "the bus hook never put %s into the recipient's context on its next prompt" % tok, "bus-%s.txt" % cli
        return PASS, "mail injected into the recipient's context by the hook on its next prompt", None

    def row_handoff(self, cli):
        if cli == "codex":
            return SKIP, "handoff_session launches Claude only", None
        tok = "CHILD%s" % self.tok
        self.assert_sandbox_tmux()
        line = "Reply with only the word %s" % tok
        r = self.run([sys.executable, os.path.join(self.repo, "bin", "handoff_session.py"), "--name", "child",
                      "--prompt", line, "--cwd", self.work, "--claude-bin", self.claude_bin,
                      "--model", self.claude_model], "handoff.txt", timeout=60)
        if r.returncode != 0:
            return FAIL, "handoff_session rc=%d: %s" % (r.returncode, (r.stderr or r.stdout).strip()[-200:]), "handoff.txt"
        if not self.wait_for(lambda: seen(tok, self.ctext("claude")), 120) or "child" not in self.tmux("ls").stdout:
            return FAIL, "child session never answered %s (or its pane is gone)" % tok, "handoff.txt"
        return PASS, "child pane launched in the sandbox server and ran its prompt", None

    def row_refresh(self, cli):
        a, _ = self.pane_pair(cli)
        tok = "REFRESH%s%s" % (self.tok, cli[:2].upper())
        g = self.store_dirs()[0 if cli == "claude" else 1]
        with open(os.path.join(g, "RESUME.md"), "w") as fh:
            fh.write("# RESUME\n\n- the refresh token is %s\n" % tok)
        self.wait_idle(a)
        self.send(a, ("/refresh" if cli == "claude" else "$refresh") + " then reply with only the refresh token from RESUME")
        if not self.wait_for(lambda: seen(tok, self.ctext(cli)), 180):
            return FAIL, "refresh never surfaced %s (seeded after the pane booted)" % tok, "refresh-%s.txt" % cli
        return PASS, "store seeded after boot, surfaced by the refresh skill", None

    def row_reflect(self, cli):
        tok = "REFLECT%s%s" % (self.tok, cli[:2].upper())
        g = self.store_dirs()[0 if cli == "claude" else 1]
        ask = ("Remember this durable fact for future sessions: the selftest codename is %s. Save only that, "
               "in the GLOBAL session-notes store (the path `python3 $SESSION_NOTES/bin/notes_config.py "
               "--stores` prints as global, not your built-in per-project memory directory). Skip every "
               "other step and do not ask questions." % tok)
        if cli == "claude":
            r = self.run(self.claude_argv("/reflect  " + ask, headless=True), "reflect-claude.txt", timeout=420)
        else:
            r = self.codex_headless("$reflect  " + ask, "reflect-codex.txt", timeout=420)
        if r.returncode != 0:
            return FAIL, "%s rc=%d" % (cli, r.returncode), "reflect-%s.txt" % cli
        hit = None
        for f in glob.glob(os.path.join(g, "*.md")):
            try:
                with open(f) as fh:
                    if tok in fh.read():
                        hit = f
            except OSError:
                pass
        if not hit:
            return FAIL, "no store file under %s holds %s" % (g, tok), "reflect-%s.txt" % cli
        if cli == "codex":
            d = self.scoped("codex", tok)
            try:
                proven = codex_commit_proven(d)
            finally:
                shutil.rmtree(d, ignore_errors=True)
            if not proven:
                return FAIL, "store gained the fact but the rollout has no executed `codex_save.py commit` call with its release output", "reflect-codex.txt"
        return PASS, "store file %s gained the fact%s" % (os.path.basename(hit),
                                                         "; codex_save.py commit was executed and released the lock" if cli == "codex" else ""), None

    def row_emdash(self, cli):
        tok = "EMD%s%s" % (self.tok, cli[:2].upper())
        note = os.path.join(self.work, "note-%s.md" % tok)
        tail = ("containing two lines: the word %s, and a second line made of the word one, the Unicode "
                "character U+2014 (decimal 8212), and the word two. Then reply DONE." % tok)
        if cli == "claude":
            r = self.run(self.claude_argv("Use the Write tool to create the file %s %s" % (note, tail), headless=True),
                         "emdash-claude.txt", timeout=180)
        else:
            r = self.codex_headless("Use apply_patch to add a file %s %s" % (note, tail), "emdash-codex.txt")
        if r.returncode != 0:
            return FAIL, "%s rc=%d" % (cli, r.returncode), "emdash-%s.txt" % cli
        try:
            with open(note, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = None
        if body is None or EM not in body:
            return FAIL, "the probe file has no U+2014 (cannot judge the guard)", "emdash-%s.txt" % cli
        d = self.scoped(cli, tok)
        try:
            raw = transcript_text(d, assistant_only=False)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if "EM-DASH GUARD" not in raw:
            return FAIL, "an em dash was written but the guard's message never reached this session", "emdash-%s.txt" % cli
        return PASS, "write flagged and the guard's text reached this session (advisory: the file stays)", None

    def row_checkout(self, cli):
        if cli == "codex":
            return SKIP, "checkout_guard is not registered for Codex", None
        tok = "CHK%s" % self.tok
        tracked = os.path.join(self.guard_repo, "tracked.txt")
        r = self.run(self.claude_argv("Use the Edit tool to replace the text 'original' with 'changed-%s' in %s. "
                                      "Make exactly that one Edit call on exactly that path: this is a test of the edit guard, so do not "
                                      "create a worktree, do not run wt, do not edit any other path. If the "
                                      "call is refused, reply REFUSED and stop." % (tok, tracked),
                                      headless=True), "checkout-claude.txt", cwd=self.guard_repo, timeout=180)
        if r.returncode != 0:
            return FAIL, "claude -p rc=%d (the session did not run)" % r.returncode, "checkout-claude.txt"
        with open(tracked) as fh:
            if fh.read() != "original\n":
                return FAIL, "the tracked file on the default branch was edited", "checkout-claude.txt"
        d = self.scoped("claude", tok)
        try:
            raw = transcript_text(d, assistant_only=False)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if not re.search(r"checkout_guard: \S*tracked\.txt is tracked on", raw):
            return FAIL, "file untouched but this session never received the guard's own refusal message", "checkout-claude.txt"
        return PASS, "edit refused with the guard's message, file unchanged", None

    def row_wave2(self, feature, rel):
        path = os.path.join(self.repo, rel)
        if not os.path.isfile(path):
            return SKIP, "not landed", None
        r = self.run([sys.executable, path, "--selftest"], "%s.txt" % feature, timeout=120)
        if r.returncode != 0:
            return FAIL, "%s --selftest rc=%d" % (rel, r.returncode), "%s.txt" % feature
        return PASS, "landed; only its --selftest is exercised here", None

    def row_hookpath(self):
        with open(os.path.join(self.claude_dir, "settings.json")) as fh:
            sj = json.load(fh)
        hj_path = os.path.join(self.codex_dir, "hooks.json")
        hj = None
        if os.path.isfile(hj_path):
            with open(hj_path) as fh:
                hj = json.load(fh)
        ccmds, xcmds = hook_commands(sj, None), hook_commands(None, hj)
        if not ccmds:
            return FAIL, "no hook commands in the Claude settings.json", None
        if not xcmds:
            return FAIL, "no hook commands in the Codex hooks.json", None

        def launcher_repo(p):
            r = subprocess.run([p, "repo"], env=self.env(), capture_output=True, text=True, timeout=20)
            return r.stdout.strip() if r.returncode == 0 else None

        problems = check_hook_paths(ccmds + xcmds, self.home, launcher_repo)
        # negative controls: a copy of the INSTALLED commands with one path broken must go red
        ghost = os.path.join(self.root, "deleted-worktree")
        c1 = [c.replace(self.repo, ghost) for c in ccmds if self.repo in c][:1]
        c2 = [c.replace(os.path.join(self.home, ".local", "bin"), os.path.join(ghost, "bin")) for c in xcmds
              if os.path.join(self.home, ".local", "bin") in c][:1]
        if not c1 or not check_hook_paths(c1, self.home, launcher_repo):
            return FAIL, "negative control: a Claude hook naming a deleted worktree was NOT reported", None
        if not c2 or not check_hook_paths(c2, self.home, launcher_repo):
            return FAIL, "negative control: a Codex hook through a missing launcher was NOT reported", None
        if problems:
            return FAIL, "; ".join(problems[:3]), None
        return PASS, "%d Claude + %d Codex hook commands resolve; both negative controls reported red" % (
            len(ccmds), len(xcmds)), None

    def row_realdirs(self):
        after = snapshot(self.real_home)
        needles = [os.path.basename(self.root), self.tok]
        diffs, benign = fatal_diffs(self.before, after, needles)
        hits = scan_for_sandbox(self.real_home, needles, self.t0)
        if diffs or hits:
            return FAIL, "; ".join(diffs + ["mentions the sandbox or run token: %s" % h for h in hits])[:300], None
        note = "" if not benign else " (%d file(s) changed by other sessions, none mention the sandbox)" % len(benign)
        return PASS, "%d install targets checked; nothing written to the real dirs mentions the sandbox or run token%s" % (len(self.before), note), None

    # -- orchestration ----------------------------------------------------------------------
    def want(self, fid):
        return self.a.feature is None or fid in self.a.feature

    def do(self, fid, cli, fn, *args):
        if not self.want(fid):
            return
        if cli == "codex" and self.login_failed:
            self.record(fid, cli, FAIL, "codex login failed", self.evid("codex-login.txt"))
            return
        if cli in ("claude", "codex") and not self.have[cli]:
            self.record(fid, cli, SKIP, self.why.get(cli, "cli unavailable"))
            return
        try:
            status, detail, ev = fn(*args)
        except Abort:
            raise
        except Exception as exc:  # a crashing row is a FAIL, never a silent skip
            status, detail, ev = FAIL, "row crashed: %r" % (exc,), None
        self.record(fid, cli, status, detail, self.evid(ev) if ev else None)

    def go(self):
        self.before = snapshot(self.real_home)
        ck, ck_why = keychain_key(os.environ.get("SELFTEST_CLAUDE_KEYCHAIN_SERVICE", "session-notes-selftest"), self.keychain)
        ok, ok_why = keychain_key(os.environ.get("SELFTEST_OPENAI_KEYCHAIN_SERVICE", "session-notes-selftest-openai"), self.keychain)
        for cli, key, why, binp in (("claude", ck, ck_why, self.claude_bin), ("codex", ok, ok_why, self.codex_bin)):
            if self.a.only and self.a.only != cli:
                self.why[cli] = "--only %s" % self.a.only
            elif not binp:
                self.why[cli] = "no %s binary" % cli
            elif not key:
                self.why[cli] = why
            else:
                self.have[cli] = True
        if not any(self.have.values()) and any(self.want(f) for f, _, sc in FEATURES if sc == "cli"):
            raise Abort("nothing live ran: no CLI was usable (%s)" % "; ".join(
                "%s: %s" % kv for kv in sorted(self.why.items())))
        self.stub_bins()
        self.clone_and_install()
        self.seed_claude()
        self.seed_codex()
        if self.have["codex"] and not self.codex_login(ok):
            self.have["codex"], self.login_failed = False, True
        ok = None
        self.start_tmux()
        if any(self.want(f) for f in ("peers", "bcast", "bus", "refresh")):
            if self.have["claude"]:
                for n in ("cA", "cB"):
                    self.launch(n, self.claude_argv("Reply with only the word READY", name=n))
            if self.have["codex"]:
                for n in ("xA", "xB"):
                    self.launch(n, self.codex_argv("Reply with only the word READY"))
            for n in sorted(self.panes):
                self.wait_idle(n, 120)
        order = [("boot", self.row_boot), ("peers", self.row_peers), ("bcast", self.row_bcast),
                 ("bus", self.row_bus), ("handoff", self.row_handoff), ("refresh", self.row_refresh),
                 ("reflect", self.row_reflect), ("emdash", self.row_emdash), ("checkout", self.row_checkout)]
        for fid, fn in order:
            for cli in ("claude", "codex"):
                self.do(fid, cli, fn, cli)
        if self.want("wt"):
            with tempfile.TemporaryDirectory(dir=self.root) as d:
                st, detail = wt_row(os.path.join(self.repo, "bin", "wt.py"), os.path.realpath(d))
            self.record("wt", "both", st, detail)
        self.do("hookpath", "both", self.row_hookpath)
        for fid, rel in (("review", "bin/review.py"), ("live", "bin/live.py"), ("env", "bin/environment_render.py")):
            self.do(fid, "both", self.row_wave2, fid, rel)
        self.stop_tmux()

    def finish_realdirs(self):
        """The real-dirs row runs whenever install.sh ran, under any --feature filter and even
        when go() aborted or was interrupted: a leak is likeliest exactly then."""
        if not self.installed or self.before is None:
            return
        self.stop_tmux()
        try:
            status, detail, ev = self.row_realdirs()
        except Exception as exc:
            status, detail = FAIL, "real-dirs check crashed: %r" % (exc,)
        self.record("realdirs", "both", status, detail)

    def teardown(self, failed):
        try:
            os.remove(os.path.join(self.codex_dir, "auth.json"))
        except OSError:
            pass
        try:
            self.stop_tmux()
        except Exception:
            pass
        if self.a.keep or failed:
            return self.root
        shutil.rmtree(self.root, ignore_errors=True)
        return None


def main():
    ap = argparse.ArgumentParser(description="Live selftest: real Claude Code and Codex sessions in a sandbox")
    ap.add_argument("--only", choices=("claude", "codex"))
    ap.add_argument("--feature", type=lambda s: set(s.split(",")), help="comma list of: " + ",".join(f for f, _, _ in FEATURES))
    ap.add_argument("--keep", action="store_true", help="keep the sandbox and print its path")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    known = {f for f, _, _ in FEATURES}
    if a.feature and not a.feature <= known:
        print("unknown feature(s): %s" % ", ".join(sorted(a.feature - known)), file=sys.stderr)
        return 2
    live = Live(a)
    results, lines, kept = execute(live)
    out = [render_table(results)] + [redact(ln) for ln in lines]
    if kept:
        out.append("sandbox kept: %s" % kept)
    try:
        print("\n".join(out))
        sys.stdout.flush()
    except OSError:  # BrokenPipe: the verdict is still the exit code
        pass
    return 1 if lines else 0


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt("signal %d" % signum)


def execute(live):
    """Run the suite. Whatever happens (an exception, Ctrl-C, SIGTERM, SIGHUP), teardown runs:
    the sandbox Codex auth.json is wiped and the sandbox tmux server is killed."""
    old = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            old[sig] = signal.signal(sig, _raise_interrupt)
        except (ValueError, OSError):
            pass
    err, lines, kept = None, [], None
    try:
        try:
            live.go()
        except Abort as exc:
            err = str(exc)
        except KeyboardInterrupt:
            err = "interrupted"
        except Exception as exc:  # setup crashed: report it, never hide it
            err = "run crashed: %r" % (exc,)
        try:
            live.finish_realdirs()
        except KeyboardInterrupt:
            err = err or "interrupted"
        if err:
            live.results.append({"feature": "realdirs", "title": dict((f, t) for f, t, _ in FEATURES)["realdirs"],
                                 "cli": "both", "status": FAIL, "detail": "run aborted: " + err, "evidence": None})
        lines = fail_lines(live.results)
    finally:
        kept = live.teardown(bool(lines) or bool(err))
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass
    return live.results, lines, kept


if __name__ == "__main__":
    sys.exit(main())
