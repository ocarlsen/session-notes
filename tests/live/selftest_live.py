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
    make selftest-watch                                 # the same run, built to be watched and filmed
    python3 tests/live/selftest_live.py [--only claude|codex] [--feature boot,bus,...] [--keep]
    python3 tests/live/selftest_live.py --watch [--pace X] [--step] [--leak-terms FILE]
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

Watch mode (--watch). One tmux session `watch` in the sandbox server: a 3-row status pane on top,
the Claude session under test on the left and the Codex one on the right (each a read-only nested
attach of the real session), in a window fixed at 180x50. Attach with `tmux -L sn-watch attach`
(a symlink named `sn-watch` in the default tmux socket directory points at the sandbox socket and
is removed at teardown). Every step shows a chapter title, holds (--pace), can wait for Enter
(--step), and writes a status JSON a film storyboard can follow. Because the run is public, every
visible pane is scanned at each step for the real home path, the username, the hostname, key-shaped
strings and (with --leak-terms FILE) each term in that file; a hit blanks the panes and aborts.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import select
import shlex
import signal
import shutil
import socket
import stat
import subprocess
import pwd
import sys
import tempfile
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.realpath(__file__))
SOCKET_LABEL = "sn-watch"  # `tmux -L sn-watch attach` reaches the sandbox server through this link
SRC_REPO = os.path.dirname(os.path.dirname(HERE))

KEY_RE = re.compile(r"sk-[A-Za-z0-9_*-]{20,}")
_SECRETS = []  # exact key values this run fetched; redacted wherever they appear
EM = chr(0x2014)
FAKE_HOME = "/" + "Users" + "/zed"   # a planted home path for the screen guard's controls

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# feature id, title, scope. "both" rows are CLI-independent and run once.
# Watch mode's `ls`: a long listing (-l, -g, -o in any short-option cluster) gets -n, numeric
# owner and group, so no real user name reaches a filmed pane.
LS_SHIM = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    --) break ;;
    --*) ;;
    -*[lgo]*) exec /bin/ls -n "$@" ;;
  esac
done
exec /bin/ls "$@"
"""
REQUIRE_TMUX_ENV = "SESSION_NOTES_REQUIRE_TMUX"  # set: the selftest fails, not skips, without tmux
RUN_TITLE = "run finished"  # the row an aborted run is reported on
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
    ("review", "review (bin/review.py)", "cli"),
    ("live", "live-context (bin/live.py)", "cli"),
    ("env", "env (bin/environment_render.py)", "cli"),
    ("realdirs", "real dirs untouched", "both"),
]

# Viewer-facing chapter title per feature (watch mode shows it, the status file carries it). No
# dashes of any kind, at most 8 words, no internal jargon. A storyboard times its cards off these.
CHAPTERS = {
    "boot": "Boot: fresh sessions load the rules",
    "bus": "Mail: a note arrives on the next prompt",
    "peers": "Peers: each session can see the others",
    "bcast": "Broadcast: one line reaches every pane",
    "handoff": "Handoff: a new session takes over",
    "refresh": "Refresh: a session rereads its memory",
    "reflect": "Reflect: a session saves what it learned",
    "emdash": "Style guard: a writing rule enforced",
    "checkout": "Branch guard: no edits on main",
    "wt": "Merge gate: no merge without approval",
    "hookpath": "Wiring: every registered path exists",
    "review": "Review: a fresh reviewer checks the change",
    "live": "Live notes: sessions share recent news",
    "env": "Environment: one file guides approvals",
    "realdirs": "Isolation: the real home stays untouched",
}
# Rows that run as headless one-shots in CI mode and as interactive panes in watch mode.
PANE_ROWS = ("boot", "reflect", "emdash", "checkout")
WATCH_W, WATCH_H, STATUS_H, INNER_W, INNER_H = 180, 50, 3, 89, 46
TITLE_HOLD, RESULT_HOLD = 2.0, 4.0   # seconds at --pace 1
DASHES = "".join(chr(c) for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0x2E3A, 0x2E3B)) + "-"


def title_problems(title):
    """Why a chapter title is not fit to show (empty list when it is)."""
    out = []
    if any(c in title for c in DASHES):
        out.append("has a dash")
    if len(title.split()) > 8:
        out.append("is over 8 words")
    if re.search(r"[/_.]|\.py|\bbin\b|\bhook\b|\bbus\b|\bbcast\b|\bwt\b", title, re.I):
        out.append("has internal jargon")
    return out


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
                cmds.append(_js_subst(lit[1:-1], src))
    return cmds


def _js_subst(tmpl, src):
    """Resolve `${name}` in a JS template literal from plain string bindings in the same input
    (`let|const|var name = "..."`, as Codex writes `let p="/repo/bin/codex_save.py"`). A name with
    no such binding, or bound twice, stays as written, so it can never match a command."""
    binds = {}
    for m in re.finditer(r'\b(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=\s*("(?:[^"\\]|\\.)*")\s*[;,\n]', src):
        try:
            v = json.loads(m.group(2))
        except ValueError:
            continue
        binds[m.group(1)] = None if m.group(1) in binds else v
    return re.sub(r"\$\{([A-Za-z_$][\w$]*)\}", lambda m: binds.get(m.group(1)) or m.group(0), tmpl)


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


def render_table(results, pane_rows=()):
    """results: dicts with feature/title/cli (claude|codex|both)/status/detail. -> table text.
    `pane_rows`: feature ids whose title gets a " (pane)" mark (watch mode: they ran in a pane)."""
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
        rows.append((title + (" (pane)" if fid in pane_rows else ""), cells[0], cells[1]))
    if "run" in by:  # an aborted run: not a feature, so it is not in FEATURES
        rows.append((RUN_TITLE, "FAIL [both]", "FAIL [both]"))
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


# ---------------------------------------------------------------------------- watch mode helpers

def host_names():
    """The machine's short and full hostnames (names under 3 chars are too generic to scan for)."""
    names = set()
    for n in (socket.gethostname(), socket.gethostname().split(".")[0]):
        if n and len(n) >= 3:
            names.add(n)
    try:
        r = subprocess.run(["hostname", "-s"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and len(r.stdout.strip()) >= 3:
            names.add(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(names)


def _names(x):
    """A str or an iterable of str -> a de-duplicated list, empties dropped."""
    items = [x] if isinstance(x, str) else list(x or [])
    out = []
    for i in items:
        if i and i not in out:
            out.append(i)
    return out


def real_identity():
    """(home dirs, user names) of the REAL account. The passwd entry of the real uid comes first, so
    an unset or overridden USER/HOME cannot make a class vanish; the environment adds to it."""
    homes, names = [], []
    try:
        pw = pwd.getpwuid(os.getuid())
        homes.append(pw.pw_dir)
        names.append(pw.pw_name)
    except (KeyError, OSError):
        pass
    homes.append(os.path.expanduser("~"))
    names += [os.environ.get("USER", ""), os.environ.get("LOGNAME", "")]
    return _names(homes), _names(names)


def screen_needles(real_home, user, hosts, leak_terms, words=(), allow=()):
    """The screen guard's needles by class. A class with no needle is simply not scanned.
    `real_home` and `user` are a str or a list. `words` (compiled regexes) and `allow` (lowercase
    phrases) come from tests/leak_scan.py's reader, so a terms file means the same thing here."""
    return {"home": [h for h in _names(real_home) if h not in ("/", "")],
            "username": [u for u in _names(user) if len(u) >= 3],
            "hostname": list(hosts),
            "terms": [t for t in leak_terms if t],
            "words": list(words), "allow": [p for p in allow if p]}


def empty_classes(needles, want_terms=False):
    """Needle classes that would scan nothing; the watch run refuses to start with any of these."""
    out = [c for c in ("home", "username", "hostname") if not needles.get(c)]
    if want_terms and not (needles.get("terms") or needles.get("words")):
        out.append("terms")
    return out


def _screen_views(text):
    """The screen as read in rows, and with the row breaks removed: a needle that crosses the
    pane's right edge arrives as two fragments, and the viewer reads it across the wrap."""
    t = text or ""
    return [t, t.replace("\r", "").replace("\n", "")]


def screen_hits(text, needles):
    """Classes (never the matched text) of everything in `text` the guard forbids on screen."""
    hits = []
    views = _screen_views(text)
    lows = [v.lower() for v in views]
    for cls in ("home", "username", "hostname"):
        if any(n.lower() in low for n in needles.get(cls, []) for low in lows):
            hits.append(cls)
    allow = needles.get("allow", [])
    term_hit = any(rx.search(v) for rx in needles.get("words", []) for v in views)
    for low in lows:
        for t in needles.get("terms", []):
            t = t.lower()
            i = low.find(t)
            while i != -1 and not term_hit:
                if not leak_scan_ref()._inside_allowed(low, i, i + len(t), allow):
                    term_hit = True
                i = low.find(t, i + 1)
    if term_hit:
        hits.append("leak term")
    if any(KEY_RE.search(v) for v in views):
        hits.append("key shaped string")
    return hits


def leak_scan_ref():
    """tests/leak_scan.py, the one reader of the leak-terms file format (loaded by path)."""
    mod = sys.modules.get("_sn_leak_scan")
    if mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_sn_leak_scan", os.path.join(os.path.dirname(HERE), "leak_scan.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_sn_leak_scan"] = mod
        spec.loader.exec_module(mod)
    return mod


def load_leak_spec(path):
    """(terms, allow, words) from a leak-terms file, read by leak_scan.load_terms (`w:` word entries,
    `!` allowed phrases, comments)."""
    spec = leak_scan_ref().load_terms(path)
    if spec is None:
        raise Abort("--leak-terms: cannot read %s" % path)
    return spec


def load_leak_terms(path):
    """Plain term lines only, case kept (kept for callers of the old name; the guard uses load_leak_spec)."""
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]


def tmux_socket_dir(environ=None):
    """The directory a bare `tmux -L name` uses for its sockets: $TMUX_TMPDIR (else /tmp)/tmux-<uid>."""
    environ = os.environ if environ is None else environ
    return os.path.join(environ.get("TMUX_TMPDIR") or "/tmp", "tmux-%d" % os.getuid())


def _stale_note(link):
    """Say what an existing `sn-watch` link is: a dangling link, a socket nothing answers on, or live."""
    try:
        if os.path.islink(link) and not os.path.exists(link):
            return " (a dangling link left by a killed run: remove it with rm)"
        if stat.S_ISSOCK(os.stat(link).st_mode):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(1)
            try:
                c.connect(link)
                return " (a tmux server answers on it: another `tmux -L sn-watch` is running)"
            except ConnectionRefusedError:
                return " (a stale socket, nothing answers on it: remove it with rm if it is not yours)"
            except OSError:
                return ""
            finally:
                c.close()
    except OSError:
        pass
    return ""


def link_selftest_socket(sock_dir, target):
    """Make `tmux -L sn-watch` reach the sandbox server: symlink <sock_dir>/sn-watch -> target.
    Refuses (Abort) when anything named sn-watch is already there. -> True when sock_dir was created."""
    link = os.path.join(sock_dir, SOCKET_LABEL)
    if os.path.lexists(link):
        raise Abort("%s already exists%s; refusing to start a second watch run on top of it" % (link, _stale_note(link)))
    created = False
    if not os.path.isdir(sock_dir):
        os.makedirs(sock_dir, mode=0o700)
        created = True
    os.symlink(target, link)
    return created


def unlink_selftest_socket(sock_dir, target, created):
    """Remove the symlink only if it is still ours (points at `target`); drop sock_dir if we made it."""
    link = os.path.join(sock_dir, SOCKET_LABEL)
    try:
        if os.path.islink(link) and os.readlink(link) == target:
            os.remove(link)
    except OSError:
        pass
    if created:
        try:
            os.rmdir(sock_dir)
        except OSError:
            pass


def render_status(st, width=WATCH_W):
    """The three status rows for a status dict (the file the runner writes). -> [str, str, str]."""
    if st.get("phase") == "aborted":
        return ["Run aborted: %s" % st.get("note", ""), "", ""]
    head = "%s/%s \u00b7 %s \u00b7 claude %s \u00b7 codex %s" % (
        st.get("step", 0), st.get("total", 0), st.get("title", ""), st.get("claude", "idle"), st.get("codex", "idle"))
    counts = "pass %d   fail %d   skip %d" % (st.get("pass", 0), st.get("fail", 0), st.get("skip", 0))
    if st.get("paused"):
        counts += "   PAUSED (p to resume)"
    elif st.get("phase") == "done":
        counts += "   done (q to close)"
    hint = "Enter: next step (with --step)   p: pause   q: quit and clean up"
    return [head[:width], counts[:width], hint[:width]]


def status_pane_main(status_path, fifo):
    """The status pane's whole job: redraw the status file, and forward Enter, p and q (as n, p, q)
    into the runner's FIFO. It never types into another pane."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = None
    try:
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except (termios.error, OSError):
        old = None
    last, mtime = None, 0
    sys.stdout.write("\x1b[2J\x1b[?25l")
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.25)
            if r:
                ch = os.read(fd, 1).decode("utf-8", "ignore")
                key = "n" if ch in ("\r", "\n") else ch if ch in ("p", "q") else ""
                if key:
                    try:
                        w = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                        os.write(w, key.encode())
                        os.close(w)
                    except OSError:
                        pass
            try:
                mt = os.stat(status_path).st_mtime_ns
            except OSError:
                continue
            if mt == mtime:
                continue
            mtime = mt
            try:
                with open(status_path, encoding="utf-8") as fh:
                    st = json.load(fh)
            except (OSError, ValueError):
                mtime = 0
                continue
            lines = render_status(st)
            if st.get("phase") == "done" and st.get("table"):
                lines = st["table"] + [""] + lines
            if lines != last:
                last = lines
                # no newline after the last row: on a 3-row pane it would scroll the first row away
                sys.stdout.write("\x1b[H" + "\r\n".join(ln + "\x1b[K" for ln in lines) + "\x1b[J")
                sys.stdout.flush()
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h")


# ---------------------------------------------------------------------------- wt row (no CLI)

GH_STUB = r'''#!/usr/bin/env python3
# gh stub for the live selftest: records every call, answers from scenario.json. No network.
# `pr comment` appends to posted.jsonl, and the comments API returns scenario + posted comments,
# so a verdict posted by review.py is readable by wt merge.
import json, os, sys, time
here = os.path.dirname(os.path.realpath(__file__))  # realpath: the PATH entry is a symlink to this dir
args = sys.argv[1:]
with open(os.path.join(here, "gh-calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
try:
    sc = json.load(open(os.path.join(here, "scenario.json")))
except OSError:
    sc = {}
def posted():
    try:
        return [json.loads(x) for x in open(os.path.join(here, "posted.jsonl")).read().splitlines() if x.strip()]
    except OSError:
        return []
if args[:2] == ["auth", "token"]:
    print("faketoken")
elif args[:2] == ["pr", "list"]:
    print(json.dumps(sc.get("pr_list", [])))
elif args[:2] == ["pr", "create"]:
    print(sc.get("pr_url", "https://example.invalid/o/r/pull/7"))
elif args[:2] == ["pr", "view"]:
    print(json.dumps(sc.get("view", {})))
elif args[:2] == ["pr", "diff"]:
    sys.stdout.write(sc.get("diff", ""))
elif args[:2] == ["api", "user"]:
    print(sc.get("login", "tester"))
elif args[:1] == ["api"]:
    print(json.dumps(sc.get("comments", []) + posted()))
elif args[:2] == ["pr", "comment"] and "--body-file" in args:
    body = open(args[args.index("--body-file") + 1]).read()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n = len(posted()) + 1
    with open(os.path.join(here, "posted.jsonl"), "a") as fh:
        fh.write(json.dumps({"id": 1000 + n, "body": body, "user": {"login": sc.get("login", "tester")},
                             "created_at": now, "updated_at": now}) + "\n")
    print("https://example.invalid/o/r/pull/7#issuecomment-%d" % (1000 + n))
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
    var_js = 'let p="/x/repo/bin/codex_save.py"; let r=await tools.exec_command({cmd:`python3 ${p} commit`});'
    with tempfile.TemporaryDirectory() as d:
        rollout(d, "dv.jsonl",
                {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": var_js},
                {"type": "custom_tool_call_output", "call_id": "c1", "output": out_text(release)})
        check("commit proof (d): a ${p} template bound to the codex_save path passes", codex_commit_proven(d))
    for bad, why in ((var_js.replace('let p=', 'let q='), "an unbound ${p}"),
                     (var_js.replace('commit`', 'stage`'), "a bound ${p} running stage"),
                     ('let p="/x/repo/bin/other.py"; ' + var_js[len('let p="/x/repo/bin/codex_save.py"; '):], "${p} bound to another script"),
                     ('let p="/x/a/bin/codex_save.py"; ' + var_js, "${p} bound twice")):
        with tempfile.TemporaryDirectory() as d:
            rollout(d, "dn.jsonl",
                    {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": bad},
                    {"type": "custom_tool_call_output", "call_id": "c1", "output": out_text(release)})
            check("commit proof: NEGATIVE control, %s fails" % why, not codex_commit_proven(d))
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
    # ---- watch mode: chapter titles, screen guard, socket link, keys, status file, layout ----
    check("titles: every feature has one", set(CHAPTERS) == {f for f, _, _ in FEATURES}, str(set(CHAPTERS) ^ {f for f, _, _ in FEATURES}))
    check("titles: none has a dash, over 8 words or jargon", all(not title_problems(t) for t in CHAPTERS.values()),
          str({k: title_problems(t) for k, t in CHAPTERS.items() if title_problems(t)}))
    check("titles: NEGATIVE control, a hyphen, an en dash, an em dash, 9 words and `bcast` are each flagged",
          all(title_problems(t) for t in ("Self-test", "Boot " + chr(0x2013) + " rules", "Boot " + EM + " rules",
                                          "one two three four five six seven eight nine", "bcast works")))
    nd = screen_needles(FAKE_HOME, "zedname", ["zedhost", "zedhost.local"], ["SECRETTERM"])
    check("guard: clean text is clean", screen_hits("$ ls\nREADME\n/tmp/sn-live-1/home", nd) == [])
    for label, text in (("home path", "cd " + FAKE_HOME + "/Projects"), ("username", "by ZedName today"),
                        ("hostname short", "on zedhost:"), ("hostname full", "ssh zedhost.local"),
                        ("leak term", "see secretterm here"), ("key", "key sk-" + "x" * 20 + "yz"),):
        want = {"home path": "home", "username": "username", "hostname short": "hostname", "hostname full": "hostname",
                "leak term": "leak term", "key": "key shaped string"}[label]
        h = screen_hits(text, nd)
        check("guard: NEGATIVE control, planted %s is flagged as its class" % label, h == [want], str(h))
    check("guard: a class with no needle is not scanned (no leak terms file)", screen_hits("secretterm", screen_needles(FAKE_HOME, "zedname", [], [])) == [])
    check("guard: task-list is not key shaped", screen_hits("task-list ask-me sk-short", nd) == [])
    with tempfile.TemporaryDirectory() as d:
        lt = os.path.join(d, "terms.txt")
        with open(lt, "w") as fh:
            fh.write("# comment\nAlphaCorp\n\n  BetaCo  \n")
        check("guard: leak-terms file: comments and blanks skipped", load_leak_terms(lt) == ["AlphaCorp", "BetaCo"])

    # the `sn-watch` socket link: refuse when present, remove on every exit path
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        sd = os.path.join(d, "tmux-1")
        check("socket: dir follows TMUX_TMPDIR", tmux_socket_dir({"TMUX_TMPDIR": d}).startswith(d + os.sep)
              and tmux_socket_dir({}).startswith("/tmp/tmux-"))
        created = link_selftest_socket(sd, "/x/target")
        check("socket: link created (and the missing dir)", created and os.readlink(os.path.join(sd, SOCKET_LABEL)) == "/x/target")
        try:
            link_selftest_socket(sd, "/x/other")
            refused = False
        except Abort as exc:
            refused = "already exists" in str(exc)
        check("socket: NEGATIVE control, an existing `sn-watch` makes the start refuse", refused)
        unlink_selftest_socket(sd, "/not/ours", False)
        check("socket: a link that is not ours is left alone", os.path.islink(os.path.join(sd, SOCKET_LABEL)))
        unlink_selftest_socket(sd, "/x/target", created)
        check("socket: removed with the dir we made", not os.path.exists(sd))
        os.makedirs(sd)
        with open(os.path.join(sd, SOCKET_LABEL), "w") as fh:
            fh.write("a regular file")
        try:
            link_selftest_socket(sd, "/x/t")
            refused = False
        except Abort:
            refused = True
        check("socket: a regular file named sn-watch also refuses", refused)

        def watch_ns(**kw):
            base = dict(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0, step=False,
                        leak_terms=None, no_hold_final=True)
            base.update(kw)
            return argparse.Namespace(**base)

        def link_then(exc):
            live = Live(watch_ns())
            live.sock_dir = os.path.join(d, "tmux-exit")

            def fake_go():
                live.sock_target = "/x/t"
                live.sock_created = link_selftest_socket(live.sock_dir, live.sock_target)
                live.sock_linked = True
                if exc:
                    raise exc
            live.go = fake_go
            try:
                execute(live)
            finally:
                shutil.rmtree(live.root, ignore_errors=True)
            return os.path.lexists(os.path.join(live.sock_dir, SOCKET_LABEL)) or os.path.exists(live.sock_dir)
        for label, exc in (("a normal run", None), ("an Abort", Abort("x")), ("Ctrl-C", KeyboardInterrupt()),
                           ("a crash", RuntimeError("boom")), ("q in the status pane", Quit("q"))):
            check("socket: link removed after %s" % label, not link_then(exc))
        live = Live(watch_ns())
        try:
            live.sock_dir = sd
            try:
                live.go()
                refused = False
            except Abort as exc:
                refused = "already exists" in str(exc)
            check("socket: go() refuses before any setup when `sn-watch` exists", refused and live.before is None)
        finally:
            shutil.rmtree(live.root, ignore_errors=True)

    aborted = [{"feature": "run", "title": RUN_TITLE, "cli": "both", "status": FAIL,
                "detail": "run aborted: nothing live ran", "evidence": None}]
    t = render_table(aborted)
    check("abort: an aborted run shows its own row, not a real-dirs FAIL",
          RUN_TITLE in t and "FAIL [both]" in t and "real dirs" not in t)
    with tempfile.TemporaryDirectory() as d:
        sh = os.path.join(d, "ls")
        with open(sh, "w") as fh:
            fh.write(LS_SHIM)
        os.chmod(sh, 0o755)
        me = pwd.getpwuid(os.getuid()).pw_name
        out = subprocess.run([sh, "-la", d], capture_output=True, text=True).stdout
        check("ls shim: a long listing shows no user name", out and me not in out and str(os.getuid()) in out)
        real = subprocess.run(["/bin/ls", "-la", d], capture_output=True, text=True).stdout
        check("ls shim: NEGATIVE control, plain /bin/ls -la does show it", me in real)
        check("ls shim: a short listing stays short", subprocess.run([sh, d], capture_output=True, text=True).stdout.strip() == "ls")
        check("ls shim: a long option such as --color=auto does not make a long listing",
              subprocess.run([sh, "--color=auto", d], capture_output=True, text=True).stdout.strip() == "ls"
              and subprocess.run([sh, "--color=auto", "-la", d], capture_output=True, text=True).stdout.count(me) == 0)
    # ---- fix round 1: wrapped needles, leak-terms syntax, [wt] config, identity, abort paths ----
    nd2 = screen_needles(FAKE_HOME, "zedname", ["zedhost"], [])
    check("guard: NEGATIVE control, a home path wrapped across the pane edge is flagged",
          screen_hits("x" * 80 + FAKE_HOME[:-1] + "\n" + FAKE_HOME[-1:] + "/Projects", nd2) == ["home"])
    check("guard: NEGATIVE control, a user name wrapped across the pane edge is flagged",
          "username" in screen_hits("by zed\nname today", nd2))
    check("guard: unwrapped clean text over several rows stays clean", screen_hits("a\\nb\\nzed\\nhost2", nd2) == [])
    with tempfile.TemporaryDirectory() as d:
        lt = os.path.join(d, "terms.txt")
        with open(lt, "w") as fh:
            fh.write("# c\nAlphaCorp\nw:Zed\n!alphacorp is fine here\n")
        terms, allow, words = load_leak_spec(lt)
        nd3 = screen_needles(FAKE_HOME, "zedname", ["zedhost"], terms, words, allow)
        check("guard: leak-terms syntax is leak_scan's: a `w:` entry flags the whole word",
              screen_hits("call Zed today", nd3) == ["leak term"], str(screen_hits("call Zed today", nd3)))
        check("guard: a `w:` entry does not flag a longer word or another case",
              screen_hits("Zedekiah and zed", nd3) == [])
        check("guard: a plain term flags, in any case", screen_hits("see ALPHACORP", nd3) == ["leak term"])
        check("guard: an allowed phrase is not flagged", screen_hits("alphacorp is fine here", nd3) == [])
        check("guard: NEGATIVE control, the old plain-line reading would have missed the `w:` entry",
              screen_hits("call Zed today", screen_needles(FAKE_HOME, "zedname", ["zedhost"], load_leak_terms(lt))) == [])
        check("guard: needle classes: a comments-only terms file leaves the terms class empty",
              (lambda sp: empty_classes(screen_needles(FAKE_HOME, "zedname", ["h3"], sp[0], sp[2], sp[1]), True))(
                  (lambda: (open(os.path.join(d, "e.txt"), "w").write("# only\n"), load_leak_spec(os.path.join(d, "e.txt")))[1])())
              == ["terms"])
    check("guard: a class with no needle is named by empty_classes",
          empty_classes(screen_needles("", "", [], [])) == ["home", "username", "hostname"])
    saved_env = dict(os.environ)
    try:
        for k in ("USER", "LOGNAME"):
            os.environ.pop(k, None)
        os.environ["HOME"] = "/nonexistent-home"
        homes_, names_ = real_identity()
        pw_ = pwd.getpwuid(os.getuid())
        check("guard: needles come from the passwd entry when USER is unset and HOME is overridden",
              pw_.pw_name in names_ and pw_.pw_dir in homes_ and "/nonexistent-home" in homes_, str((homes_, names_)))
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    # B3: one `[wt]` table, however many rows add it; parsed by tomllib where the interpreter has it
    live = Live(argparse.Namespace(only=None, feature=None, keep=False, selftest=False))
    try:
        for _ in range(2):  # the review row runs once per CLI
            live.ensure_wt_token_command()
        with open(live.cfg) as fh:
            cfg_text = fh.read()
        check("config: the review row leaves exactly one [wt] table", cfg_text.count("[wt]") == 1, cfg_text)
        sys.path.insert(0, os.path.join(SRC_REPO, "bin"))
        import notes_config
        check("config: notes_config parses the sandbox config after two review rows",
              notes_config.parse_toml(cfg_text).get("wt", {}).get("token_command") == "echo faketoken")
        toml_py = [p for p in (sys.executable, shutil.which("python3.13"), shutil.which("python3.12"),
                               shutil.which("python3.11"), "/opt/homebrew/bin/python3.12") if p and os.path.exists(p)]
        ran = 0
        for py in toml_py:
            r = subprocess.run([py, "-c", "import sys\ntry:\n import tomllib\nexcept ImportError:\n sys.exit(7)\n"
                                "tomllib.load(open(sys.argv[1], 'rb'))", live.cfg], capture_output=True, text=True)
            if r.returncode != 7:
                ran += 1
                check("config: tomllib (%s) accepts the sandbox config" % os.path.basename(py), r.returncode == 0, r.stderr[-200:])
                break
        if not ran:
            print("skip tomllib parse of the sandbox config: no Python 3.11+ found")
    finally:
        shutil.rmtree(live.root, ignore_errors=True)
    # N10: a signal between makedirs and the end of link_socket must not leak the link or the made dir
    with tempfile.TemporaryDirectory() as d:
        live = Live(argparse.Namespace(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0,
                                       step=False, leak_terms=None, no_hold_final=True))
        live.sock_dir = os.path.join(d, "tmux-atomic")
        live.tmux = lambda *a, **k: argparse.Namespace(stdout="/x/t\n", returncode=0)
        real_symlink = os.symlink

        def boom(src, dst):
            real_symlink(src, dst)  # the link lands, then the signal arrives
            raise KeyboardInterrupt("signal 15")
        os.symlink = boom
        try:
            link_failed = False
            try:
                live.link_socket()
            except KeyboardInterrupt:
                link_failed = True
        finally:
            os.symlink = real_symlink
        live.unlink_socket()
        check("socket: a signal right after the link is created still removes the link and the dir we made",
              link_failed and not os.path.lexists(os.path.join(live.sock_dir, SOCKET_LABEL)) and not os.path.exists(live.sock_dir))
        shutil.rmtree(live.root, ignore_errors=True)
    # N6: every abort path writes phase `aborted` with the reason, and nothing overwrites it
    for label, exc, want in (("an Abort", Abort("because x"), "because x"), ("Ctrl-C", KeyboardInterrupt(), "interrupted"),
                             ("a crash", RuntimeError("boom"), "crashed"), ("q", Quit("stopped from q"), "stopped from q")):
        live = Live(argparse.Namespace(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0,
                                       step=False, leak_terms=None, no_hold_final=True))
        try:
            def go_raise(exc=exc):
                raise exc
            live.go = go_raise
            live.installed, live.before = True, {}
            live.begin_step = lambda *a, **k: None
            execute(live)
            live.write_status(phase="done", title="late")  # a later transition must not rewrite the file
            with open(live.status_path) as fh:
                st = json.load(fh)
            check("status: %s writes phase aborted with the reason, and it stays" % label,
                  st.get("phase") == "aborted" and want in st.get("note", "") and live.status_log[-1].get("phase") == "aborted", str(st))
        finally:
            shutil.rmtree(live.root, ignore_errors=True)
    # N2: a guard trip in the final hold is a failed run with its message
    live = Live(argparse.Namespace(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0,
                                   step=False, leak_terms=None, no_hold_final=True))
    try:
        live.go = lambda: None

        def hold_trips(wait=True):
            raise Abort("screen guard: the status pane showed username; the watch panes were cleared and the run stopped")
        live.finish_watch = hold_trips
        res2, lines2, _ = execute(live)
        check("guard trip in the final hold: the run fails, with the message in the failure lines",
              bool(lines2) and any("screen guard" in ln for ln in lines2) and any(r["feature"] == "run" for r in res2), str(lines2))
    finally:
        shutil.rmtree(live.root, ignore_errors=True)
    # N1: after a trip the cleared panes are never re-attached by the next step
    live = Live(argparse.Namespace(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0,
                                   step=False, leak_terms=None, no_hold_final=True))
    try:
        calls = []
        live.default_focus = lambda: calls.append(1)
        live.stopping = True
        live.begin_step("realdirs", "both", quick=True)
        after_trip = len(calls)
        live.stopping = False
        live.begin_step("realdirs", "both", quick=True)
        check("guard: begin_step does not re-attach sessions once the run is stopping (and does otherwise)",
              after_trip == 0 and len(calls) == 1, str((after_trip, len(calls))))
    finally:
        shutil.rmtree(live.root, ignore_errors=True)
    # N3: the watch run refuses to start when a class has nothing to scan for
    live = Live(argparse.Namespace(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0,
                                   step=False, leak_terms=None, no_hold_final=True))
    try:
        live.needles = screen_needles("", "", [], [])
        try:
            live.go()
            refused = False
        except Abort as exc:
            refused = "nothing to scan for" in str(exc) and "username" in str(exc)
        check("guard: --watch refuses to start with an empty needle class", refused and live.before is None)
    finally:
        shutil.rmtree(live.root, ignore_errors=True)
    # status file, pace, keys, step mode (no tmux)
    ns = argparse.Namespace(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0, step=False,
                            leak_terms=None, no_hold_final=True)
    live = Live(ns)
    try:
        live.have = {"claude": True, "codex": True}
        live.plan = [("boot", "claude"), ("wt", "both")]
        t0 = time.time()
        live.do("boot", "claude", lambda c: (PASS, "ok", None), "claude")
        live.do("wt", "both", lambda: (FAIL, "bad", None))
        phases = [(e["phase"], e["step"], e["total"], e["id"], e["claude"], e["codex"]) for e in live.status_log]
        check("status: title, running, result for a step; counts follow",
              phases[:3] == [("title", 1, 2, "boot", "idle", "idle"), ("running", 1, 2, "boot", "working", "idle"),
                             ("result", 1, 2, "boot", "pass", "idle")], str(phases))
        last = live.status_log[-1]
        check("status: a both step moves both CLIs, with pass and fail counts",
              last["claude"] == "fail" and last["codex"] == "fail" and last["pass"] == 1 and last["fail"] == 1
              and last["title"] == CHAPTERS["wt"] and last["result"] == FAIL, str(last))
        with open(live.status_path) as fh:
            check("status: the file on disk is the last write", json.load(fh)["phase"] == "result")
        check("pace 0: no holds", time.time() - t0 < 1.0)
        live.pace = 0.2
        t1 = time.time()
        live.plan.append(("boot", "claude"))
        live.do("boot", "claude", lambda c: (PASS, "ok", None), "claude")
        check("pace 0.2 holds about 0.4 s on the title and 0.8 s on the result", 1.0 <= time.time() - t1 < 4.0, "%.2f" % (time.time() - t1))
        # keys
        os.mkfifo(live.fifo)
        live.fifo_rd = os.open(live.fifo, os.O_RDONLY | os.O_NONBLOCK)
        w = os.open(live.fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.write(w, b"n")
        live.poll_keys()
        check("keys: Enter marks a pending step", live.enter_pending is True)
        os.write(w, b"p")
        live.poll_keys()
        check("keys: p pauses (and the status file says so)", live.paused and live.status_log[-1]["paused"] is True)
        os.write(w, b"p")
        live.poll_keys()
        check("keys: p again resumes", not live.paused and live.status_log[-1]["paused"] is False)
        os.write(w, b"q")
        try:
            live.poll_keys()
            quit_ = False
        except Quit:
            quit_ = live.quit
        check("keys: q aborts (Quit, an Abort) with the quit flag set", quit_ and issubclass(Quit, Abort))
        live.quit, live.step_mode, live.pace = False, True, 0.0
        threading.Timer(0.6, lambda: os.write(w, b"n")).start()
        t2 = time.time()
        live.plan.append(("boot", "claude"))
        live.do("boot", "claude", lambda c: (PASS, "ok", None), "claude")
        check("step mode: the step waits for Enter, then runs", 0.5 <= time.time() - t2 < 5.0, "%.2f" % (time.time() - t2))
        os.close(w)
        st = {"step": 3, "total": 16, "title": "Broadcast: one line reaches every pane", "claude": "working", "codex": "idle",
              "phase": "running", "pass": 2, "fail": 0, "skip": 1}
        rows = render_status(st)
        check("status pane: N/M title claude state codex state, then counts",
              rows[0] == "3/16 · Broadcast: one line reaches every pane · claude working · codex idle"
              and rows[1].startswith("pass 2   fail 0   skip 1") and len(rows) == 3, str(rows))
        check("status pane: NEGATIVE control, an aborted run says so, not a step line",
              render_status({"phase": "aborted", "note": "screen guard"})[0].startswith("Run aborted"))
        # a watch run's child env shows nothing real
        e = live.env()
        okp = all(x == live.bin or x in ("/usr/bin", "/bin", "/usr/sbin", "/sbin") for x in e["PATH"].split(os.pathsep))
        check("watch env: PATH is the sandbox bin plus system dirs only, USER and prompt scrubbed",
              okp and e["USER"] == "user" and e["PS1"] == "$ " and not any(live.real_home in str(v) for v in e.values()), str(e))
    finally:
        shutil.rmtree(live.root, ignore_errors=True)
    tbl = render_table([{"feature": "boot", "title": "boot context + clock line", "cli": "claude", "status": PASS, "detail": ""}],
                       pane_rows=PANE_ROWS)
    check("table: pane rows carry a (pane) mark in watch mode", "(pane)" in tbl and "(pane)" not in render_table(res), tbl)

    # the real layout, the screen guard through real panes, link removal at teardown (needs tmux)
    if shutil.which("tmux"):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            live = Live(argparse.Namespace(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0,
                                           step=False, leak_terms=None, no_hold_final=True))
            live.sock_dir = os.path.join(d, "tmux-t")
            try:
                live.plan = [("boot", "claude")]
                live.start_tmux()
                live.link_socket()
                geo = {n: live.tmux("display", "-p", "-t", pid, "#{pane_width}x#{pane_height}").stdout.strip()
                       for n, pid in live.panes_of.items()}
                check("layout: 180x3 status over 89x46 and 90x46, window fixed at 180x50",
                      geo["status"] == "180x3" and geo["claude"] == "89x46" and geo["codex"] == "90x46"
                      and live.tmux("display", "-p", "-t", "=watch:", "#{window_width}x#{window_height}").stdout.strip() == "180x50", str(geo))
                live.tmux("new-session", "-d", "-s", "newest", "sleep", "99")
                check("layout: `attach` is aliased to the watch session (not the newest one)",
                      "=watch" in live.tmux("show", "-s").stdout and "attach=attach-session" in live.tmux("show", "-s").stdout)
                live.needles = screen_needles(FAKE_HOME, "zedname", ["zedhost"], ["SECRETTERM"])
                planted = {"home": FAKE_HOME + "/x", "username": "zedname", "hostname": "zedhost", "leak term": "SECRETTERM",
                           "key shaped string": "sk-" + "a" * 24}

                def show(text):
                    live.tmux("respawn-pane", "-k", "-t", live.panes_of["claude"], "--", "sh", "-c",
                              "printf '%%s\\n' %s; exec sleep 99" % shlex.quote(text))
                    for _ in range(20):
                        if text.split()[0] in live.tmux("capture-pane", "-p", "-t", live.panes_of["claude"]).stdout:
                            return
                        time.sleep(0.1)
                show("hello there")
                live.stopping = False
                try:
                    live.guard()
                    clean = True
                except Abort:
                    clean = False
                check("guard through a real pane: NEGATIVE control, clean text does not trip", clean)
                for cls, text in planted.items():
                    live.stopping = False
                    show(text)
                    try:
                        live.guard()
                        msg = None
                    except Abort as exc:
                        msg = str(exc)
                    blank = all(not live.tmux("capture-pane", "-p", "-t", pid).stdout.strip() for pid in live.panes_of.values())
                    check("guard through a real pane: planted %s trips, names pane and class, not the text, and clears the panes" % cls,
                          bool(msg) and "claude pane" in msg and cls in msg and text not in msg and blank, str(msg))
                # fix round 1: a needle that crosses column 89 of a real 89 column pane
                live.stopping = False
                wrapped = "x" * 80 + " " + FAKE_HOME + "name/more"
                show(wrapped)
                plain = live.tmux("capture-pane", "-p", "-t", live.panes_of["claude"]).stdout
                try:
                    live.guard()
                    msg = None
                except Abort as exc:
                    msg = str(exc)
                check("guard through a real pane: NEGATIVE control, a home path and user name crossing column 89 trip",
                      FAKE_HOME not in plain and "zedname" not in plain and bool(msg) and "home" in msg and "username" in msg,
                      "plain capture split: %s; msg=%s" % (FAKE_HOME not in plain, msg))
            finally:
                live.teardown(False)
                try:
                    live.tmux("ls")
                    refused = False
                except FileNotFoundError:
                    refused = True
                check("teardown: the sn-watch link and the sandbox socket are gone; with the sandbox dir gone tmux is "
                      "refused, never redirected to the default server",
                      not os.path.lexists(os.path.join(live.sock_dir, SOCKET_LABEL)) and not os.path.exists(live.sock_target)
                      and refused)
                shutil.rmtree(live.root, ignore_errors=True)
    elif os.environ.get(REQUIRE_TMUX_ENV) is not None:
        # CI sets this and installs tmux: a missing tmux there is a broken runner, not a skip
        check("tmux on PATH (%s is set, so these checks may not be skipped)" % REQUIRE_TMUX_ENV, False)
    else:
        print("skip layout and guard-through-panes checks: no tmux (set %s=1 to fail instead)" % REQUIRE_TMUX_ENV)

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


class Quit(Abort):
    """The viewer pressed q in the watch status pane: stop, tear everything down."""


class Live:
    CLAUDE_TOOLS = ["Bash", "Write", "Edit", "Read", "Skill"]

    def __init__(self, a):
        self.a = a
        self.real_home = os.path.expanduser("~")
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="sn-live-", dir="/tmp"))
        j = lambda *p: os.path.join(self.root, *p)
        self.home, self.bin, self.tmuxd, self.repo, self.work, self.logs = (
            j("home"), j("bin"), j("tmux"), j("repo"), j("work"), j("logs"))
        self.ghdir = os.path.join(self.home, ".local", "state", "session-notes", "ghstub")
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
        # -- watch mode (every attribute read with getattr: older callers build Namespaces without them)
        self.watch = bool(getattr(a, "watch", False))
        self.pace = float(getattr(a, "pace", None) if getattr(a, "pace", None) is not None else 1.0)
        self.step_mode = bool(getattr(a, "step", False))
        self.hold_final = not getattr(a, "no_hold_final", False)
        spec = load_leak_spec(a.leak_terms) if getattr(a, "leak_terms", None) else ([], [], [])
        self.leak_terms = spec[0]
        homes, names = real_identity()
        self.user = names[0] if names else ""
        self.needles = screen_needles([self.real_home] + homes, names, host_names(), spec[0], spec[2], spec[1])
        self.status_path = j("status.json")
        self.fifo = j("keys.fifo")
        self.fifo_rd = self.fifo_wr = None
        self.paused = False
        self.enter_pending = False
        self.quit = False
        self.panes_of = {}                 # "status" | "claude" | "codex" -> tmux pane id (watch layout)
        self.focused = {}                  # "claude" | "codex" -> session name shown
        self.ephemeral = []                # sessions this step created; killed when the step ends
        self.state = {"claude": "idle", "codex": "idle"}
        self.cur = {"step": 0, "total": 0, "id": "", "title": "", "phase": "title"}
        self.plan = []
        self.status_log = []               # every status dict written (--selftest reads it)
        self.last_scan = 0.0
        self.sock_dir = tmux_socket_dir()  # where `tmux -L sn-watch` looks (the user's default dir)
        self.sock_created = False
        self.sock_linked = False
        self.sock_target = None
        self.step_no = 0
        self.stopping = False             # the run is ending (abort, guard, quit): no more holds
        self.guard_tripped = False
        self.aborted = False              # the status file carries phase `aborted` and is final
        self.tmux_bin = shutil.which("tmux") or "tmux"

    # -- environment ----------------------------------------------------------------------
    def env(self, **extra):
        dirs = [self.bin]
        if not self.watch:  # watch mode links tmux, git and python3 into the sandbox bin instead
            for t in ("tmux", "git", "python3"):
                p = shutil.which(t)
                if p and os.path.dirname(p) not in dirs:
                    dirs.append(os.path.dirname(p))
        dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        user = "user" if self.watch else os.environ.get("USER", "selftest")  # watch: nothing real on screen
        e = {"HOME": self.home, "PATH": os.pathsep.join(dirs), "TERM": "xterm-256color",
             "LANG": "en_US.UTF-8", "USER": user, "SHELL": "/bin/zsh",
             "TMUX_TMPDIR": self.tmuxd, "CLAUDE_CONFIG_DIR": self.claude_dir, "CODEX_HOME": self.codex_dir,
             "SESSION_NOTES_CONFIG": self.cfg, "DISABLE_AUTOUPDATER": "1",
             "GIT_CONFIG_GLOBAL": os.path.join(self.root, "gitconfig"),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
             "WT_GH": os.path.join(self.bin, "gh")}
        if self.watch:
            e.update(LOGNAME=user, PS1="$ ", PROMPT="$ ")
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
        # With the sandbox TMUX_TMPDIR gone, tmux falls back to the user's DEFAULT server. Never let it.
        if not os.path.isdir(self.tmuxd):
            raise FileNotFoundError("the sandbox tmux dir %s is gone; refusing to talk to any tmux server" % self.tmuxd)
        return subprocess.run([self.tmux_bin] + list(args), env=self.env(), capture_output=True, text=True,
                              timeout=timeout)

    def assert_sandbox_tmux(self):
        r = self.tmux("display", "-p", "#{socket_path}")
        sock = os.path.realpath(r.stdout.strip()) if r.stdout.strip() else ""
        if r.returncode != 0 or not sock.startswith(self.root + os.sep):
            raise Abort("tmux socket %r is not inside the sandbox %s; refusing to touch it" % (sock, self.root))

    def start_tmux(self):
        if self.watch:
            return self.start_watch_layout()
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
        w, h = (INNER_W, INNER_H) if self.watch else (180, 50)
        r = self.tmux("new-session", "-d", "-s", name, "-x", str(w), "-y", str(h), "-c", cwd or self.work,
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
        self.nap(0.8)
        self.tmux("send-keys", "-t", "=%s:" % name, "Enter")

    def wait_for(self, cond, timeout, step=1.5):
        end = time.time() + timeout
        while time.time() < end:
            v = cond()
            if v:
                return v
            self.nap(step)
        return None

    def nap(self, secs):
        """Sleep `secs`; in watch mode in 0.25 s slices, each one watching the keys and the screen."""
        if not self.watch:
            time.sleep(secs)
            return
        end = time.time() + secs
        while True:
            self.tick()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.25, left))

    def idle(self, name):
        """True when the pane shows an input box and is not mid-turn."""
        s = self.screen(name, 30)
        if not s.strip() or "interrupt" in s:
            return False
        tail = "\n".join([ln for ln in s.splitlines() if ln.strip()][-8:])
        return "❯" in tail or "›" in tail

    def wait_idle(self, name, timeout=90):
        self.nap(3)
        return self.wait_for(lambda: self.idle(name), timeout)

    # -- watch mode -------------------------------------------------------------------------
    def pane_cmd(self, argv, exec_=True):
        """A shell command string that runs argv under `env -i` with the sandbox env (no TMUX)."""
        parts = (["exec"] if exec_ else []) + ["env", "-i"] + [shlex.quote("%s=%s" % kv) for kv in sorted(self.env().items())]
        return " ".join(parts + [shlex.quote(x) for x in argv])

    @staticmethod
    def label_cmd(label):
        return "printf '\\n  %%s\\n' %s; exec sleep 86400" % shlex.quote(label)

    def start_watch_layout(self):
        """Session `watch`: status pane on top, Claude left, Codex right, fixed 180x50."""
        os.mkfifo(self.fifo)
        self.fifo_rd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.fifo_wr = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)  # keeps reads from seeing EOF
        shutil.copy(os.path.realpath(__file__), os.path.join(self.root, "watch_status.py"))
        self.write_status(step=0, total=len(self.plan), id="setup", title="Setting up the sandbox", phase="title")
        status_cmd = self.pane_cmd(["python3", os.path.join(self.root, "watch_status.py"),
                                    "--status-pane", self.status_path, self.fifo])
        r = self.tmux("new-session", "-d", "-s", "watch", "-x", str(WATCH_W), "-y", str(WATCH_H), "-c", self.root,
                      "--", "sh", "-c", status_cmd)
        if r.returncode != 0:
            raise Abort("could not start the sandbox tmux server: %s" % r.stderr.strip())
        self.assert_sandbox_tmux()
        # window-size manual is set on the watch window only: tmux 3.6b exits when a session is created
        # without -x/-y (handoff_session does that) while the GLOBAL option is manual.
        self.tmux("set", "-w", "-t", "=watch:", "window-size", "manual")
        for args in (("-g", "default-size", "%dx%d" % (INNER_W, INNER_H)),
                     ("-g", "status", "off"), ("-g", "status-left", ""), ("-g", "status-right", ""),
                     ("-g", "set-titles", "off"), ("-g", "automatic-rename", "off"), ("-g", "allow-rename", "off"),
                     ("-g", "remain-on-exit", "off"), ("-g", "mouse", "off"),
                     ("-g", "pane-border-style", "fg=colour238"), ("-g", "pane-active-border-style", "fg=colour238"),
                     ("-s", "escape-time", "0"),
                     # a bare `tmux -L sn-watch attach` lands on the watch window, not the newest session
                     ("-s", "command-alias[100]", "attach=attach-session -t =watch")):
            self.tmux("set", *args)
        self.tmux("resize-window", "-t", "=watch:", "-x", str(WATCH_W), "-y", str(WATCH_H))
        status = self.tmux("display", "-p", "-t", "=watch:0.0", "#{pane_id}").stdout.strip()
        claude = self.tmux("split-window", "-v", "-t", status, "-l", str(INNER_H), "-P", "-F", "#{pane_id}", "-c", self.root,
                           "--", "sh", "-c", self.label_cmd("claude")).stdout.strip()
        codex = self.tmux("split-window", "-h", "-t", claude, "-l", str(WATCH_W - INNER_W - 1), "-P", "-F", "#{pane_id}",
                          "-c", self.root, "--", "sh", "-c", self.label_cmd("codex")).stdout.strip()
        if not (status and claude and codex):
            raise Abort("could not build the watch layout")
        self.panes_of = {"status": status, "claude": claude, "codex": codex}
        for name, pid in self.panes_of.items():
            self.tmux("select-pane", "-t", pid, "-T", name)
        geo = {n: self.tmux("display", "-p", "-t", pid, "#{pane_width}x#{pane_height}").stdout.strip()
               for n, pid in self.panes_of.items()}
        if geo["status"] != "%dx%d" % (WATCH_W, STATUS_H) or geo["claude"] != "%dx%d" % (INNER_W, INNER_H) \
                or not geo["codex"].endswith("x%d" % INNER_H):
            raise Abort("the watch layout came out as %s, not 180x3 over 89x46 and 90x46" % geo)

    def link_socket(self):
        target = os.path.realpath(self.tmux("display", "-p", "#{socket_path}").stdout.strip())
        # Ownership is recorded BEFORE the link exists: unlink_selftest_socket removes only a link
        # that points at `target`, so a signal anywhere in here can neither leak ours nor touch another.
        self.sock_target = target
        self.sock_created = not os.path.isdir(self.sock_dir)
        self.sock_linked = True
        link_selftest_socket(self.sock_dir, target)

    def unlink_socket(self):
        if self.sock_linked:
            unlink_selftest_socket(self.sock_dir, self.sock_target, self.sock_created)
            self.sock_linked = False

    def focus(self, cli, name):
        """Show session `name` (or a blank label when None or gone) in the `cli` side, read-only."""
        if not self.watch or not self.panes_of:
            return
        if name is not None and self.tmux("has-session", "-t", "=" + name).returncode != 0:
            name = None
        if self.focused.get(cli, "?") == name:
            return
        if name is None:
            cmd = self.label_cmd(cli)
        else:
            cmd = self.pane_cmd([self.tmux_bin, "attach-session", "-r", "-f", "ignore-size", "-t", "=" + name],
                                exec_=False) + "; exec sleep 86400"
        self.tmux("respawn-pane", "-k", "-t", self.panes_of[cli], "--", "sh", "-c", cmd)
        self.focused[cli] = name

    def default_focus(self):
        for cli in ("claude", "codex"):
            base = self.pane_pair(cli)[0]
            self.focus(cli, base if base in self.panes else None)

    def launch_eph(self, name, argv, cwd=None, out=None):
        """A session that lives for one step only: killed (and the side refocused) when it ends."""
        self.launch(name, argv, cwd=cwd, out=out)
        self.ephemeral.append(name)

    def kill_ephemeral(self):
        names, self.ephemeral = self.ephemeral, []
        if self.watch:
            for cli in ("claude", "codex"):
                if self.focused.get(cli) in names:
                    self.focus(cli, None)
            self.default_focus()
        for n in names:
            self.tmux("kill-session", "-t", "=" + n)
            self.panes.discard(n)

    def stop_inner(self):
        """Watch mode, before the real-dirs check: end every session under test, keep the window."""
        for cli in ("claude", "codex"):
            self.focus(cli, None)
        for n in sorted(self.panes):
            self.tmux("kill-session", "-t", "=" + n)
        self.panes.clear()

    # keys, guard, status ---------------------------------------------------------------
    def poll_keys(self):
        if self.fifo_rd is None:
            return
        try:
            data = os.read(self.fifo_rd, 64).decode("utf-8", "ignore")
        except (BlockingIOError, OSError):
            return
        for ch in data:
            if ch == "n":
                self.enter_pending = True
            elif ch == "p":
                self.paused = not self.paused
                self.write_status()
            elif ch == "q":
                self.quit = True
                raise Quit("stopped from the status pane (q)")

    def tick(self):
        """Called while waiting: keys, pause, and the screen guard (at most once a second)."""
        if not self.watch:
            return
        self.poll_keys()
        while self.paused:
            time.sleep(0.2)
            self.poll_keys()
            self.guard(throttle=True)
        self.guard(throttle=True)

    def guard(self, throttle=False):
        """The screen guard. A hit clears the watch panes and aborts; the message names the pane and
        the class of hit, never the matched text."""
        if not self.panes_of or self.stopping:
            return
        now = time.time()
        if throttle and now - self.last_scan < 1.0:
            return
        self.last_scan = now
        for name in ("status", "claude", "codex"):
            r = self.tmux("capture-pane", "-p", "-J", "-t", self.panes_of[name])
            hits = screen_hits(r.stdout if r.returncode == 0 else "", self.needles)
            if hits:
                self.trip_guard(name, hits)

    def trip_guard(self, name, hits):
        self.stopping = self.guard_tripped = True
        for pid in self.panes_of.values():
            self.tmux("respawn-pane", "-k", "-t", pid, "--", "sh", "-c", "exec sleep 86400")
            self.tmux("clear-history", "-t", pid)
        msg = "screen guard: the %s pane showed %s; the watch panes were cleared and the run stopped" % (
            name, ", ".join(hits))
        self.mark_aborted("screen guard tripped (%s)" % ", ".join(hits))
        raise Abort(msg)

    def counts(self):
        c = {"pass": 0, "fail": 0, "skip": 0}
        for r in self.results:
            k = r["status"].lower()
            if k in c:
                c[k] += 1
        return c

    def write_status(self, **kw):
        """Atomically rewrite the status file at a transition. See docs/tools.md for the format.
        Once the run is marked aborted the file is final: later transitions do not rewrite it."""
        if self.aborted:
            return
        self.cur.update(kw)
        st = dict(self.cur, claude=self.state["claude"], codex=self.state["codex"], paused=self.paused,
                  t=int(time.time()), **self.counts())
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, self.status_path)
        self.status_log.append(st)

    def mark_aborted(self, note):
        """Phase `aborted` with the reason, on every abort path; written once, never overwritten."""
        if not self.watch or self.aborted:
            return
        self.write_status(phase="aborted", note=redact(str(note))[:160])
        self.aborted = True

    def hold(self, base):
        secs = 0.0 if self.stopping else base * self.pace
        if secs > 0:
            self.nap(secs)

    def begin_step(self, fid, cli, quick=False):
        self.ephemeral = []
        if not self.watch:
            return
        self.step_no += 1
        self.enter_pending = False
        self.state = {"claude": "idle", "codex": "idle"}
        self.write_status(step=self.step_no, total=len(self.plan), id=fid, cli=cli, title=CHAPTERS[fid],
                          phase="title", result="")
        if not self.stopping:  # after a trip the panes stay cleared: never re-attach a session
            self.default_focus()
        self.guard()
        if quick or self.stopping:
            return
        self.hold(TITLE_HOLD)
        while self.step_mode and not self.enter_pending and not self.stopping:
            self.nap(0.25)
        for c in (("claude", "codex") if cli == "both" else (cli,)):
            self.state[c] = "working"
        self.write_status(phase="running")

    def end_step(self, status, quick=False):
        if self.watch:
            for c in ("claude", "codex"):
                if self.state[c] == "working" or (quick and self.cur.get("cli") in (c, "both")):
                    self.state[c] = status.lower()
            self.write_status(phase="result", result=status)
            self.guard()
            if not quick:
                self.hold(RESULT_HOLD)
        self.kill_ephemeral()

    def finish_watch(self, wait=True):
        """Table into the status pane (zoomed), then (unless the run failed to finish) wait for q."""
        if not self.watch or not self.panes_of:
            return
        table = render_table(self.results, pane_rows=PANE_ROWS).splitlines()
        if self.guard_tripped or self.quit or self.aborted:  # the panes were blanked or the viewer left: nothing to show
            return
        self.write_status(phase="done", title="Finished", table=table, step=self.cur.get("total", 0))
        self.tmux("resize-pane", "-Z", "-t", self.panes_of["status"])
        if not wait or not self.hold_final:
            return
        try:
            while True:
                self.nap(0.25)
        except Quit:
            return

    # -- setup ----------------------------------------------------------------------------
    def store_dirs(self):
        # The Claude store sits outside .claude/ (config paths.global_store): Claude treats writes
        # under .claude/ as sensitive and would prompt, which a headless run cannot answer.
        return (os.path.join(self.root, "store-claude"),
                os.path.join(self.codex_dir, "session-notes", "global"))

    def stub_bins(self):
        with open(os.path.join(self.bin, "launchctl"), "w") as fh:
            fh.write('#!/bin/sh\necho "$@" >> "%s/launchctl-calls.txt"\nexit 0\n' % self.logs)
        # The gh stub keeps its state (call log, scenario, posted comments) beside itself, in a dir
        # Codex's workspace-write sandbox may write (install.sh grants the session-notes state dir);
        # bin/gh is a symlink to it, which is what WT_GH and PATH name.
        os.makedirs(self.ghdir, exist_ok=True)
        with open(os.path.join(self.ghdir, "gh"), "w") as fh:
            fh.write(GH_STUB)
        os.chmod(os.path.join(self.ghdir, "gh"), 0o755)
        os.symlink(os.path.join(self.ghdir, "gh"), os.path.join(self.bin, "gh"))
        os.chmod(os.path.join(self.bin, "launchctl"), 0o755)
        for n, real in (("claude", self.claude_bin), ("codex", self.codex_bin)):
            if real:
                os.symlink(real, os.path.join(self.bin, n))
        if self.watch:  # only sandbox dirs (plus the system ones) on the panes' PATH: link what they need in
            for n in ("tmux", "git", "python3"):
                real = shutil.which(n)
                if real and not os.path.lexists(os.path.join(self.bin, n)):
                    os.symlink(real, os.path.join(self.bin, n))
            # `ls -l` prints the real owner name (the 08:25 11/23 take tripped the screen guard on
            # it): the shim adds -n, so a long listing shows the numeric uid instead
            with open(os.path.join(self.bin, "ls"), "w") as fh:
                fh.write(LS_SHIM)
            os.chmod(os.path.join(self.bin, "ls"), 0o755)
            # a login zsh (Codex runs its commands in one) lets path_helper put /bin first, so the
            # shim would lose: put the sandbox bin back in front after it
            line = 'export PATH="%s:$PATH"\n' % self.bin
            for rc in (".zshenv", ".zprofile"):
                with open(os.path.join(self.home, rc), "a") as fh:
                    fh.write(line)

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
        # Watch mode names no keychain path (it would show the real home). Without a path `security`
        # looks in $HOME/Library/Keychains (a bare temp HOME finds nothing: exit 44), so the sandbox
        # home gets a symlink to the real login keychain. The link is removed with the sandbox.
        if self.watch:
            kdir = os.path.join(self.home, "Library", "Keychains")
            os.makedirs(kdir, exist_ok=True)
            link = os.path.join(kdir, "login.keychain-db")
            if not os.path.lexists(link):
                os.symlink(self.keychain, link)
        doc["apiKeyHelper"] = "security find-generic-password -s %s -w%s" % (
            shlex.quote(os.environ.get("SELFTEST_CLAUDE_KEYCHAIN_SERVICE", "session-notes-selftest")),
            "" if self.watch else " " + shlex.quote(self.keychain))
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
    def claude_argv(self, prompt, name=None, headless=False, tools=None, budget="0.60"):
        a = [self.claude_bin]
        if headless:
            a += ["-p", "--max-budget-usd", budget]
        if name:
            a += ["-n", name]
        return a + ["--model", self.claude_model, "--allowedTools"] + list(tools or self.CLAUDE_TOOLS) + ["--", prompt]

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

    def turn(self, cli, prompt, log, timeout, name, cwd=None, until=None, tools=None, budget="0.60",
             sandbox="workspace-write"):
        """One session turn. CI mode: a headless one-shot. Watch mode: an interactive pane with the
        same prompt as its first message (the "(pane)" rows); returns once `until()` holds, or the
        pane has gone idle when there is no `until`, or `timeout` passes (rc 124)."""
        if not self.watch:
            if cli == "claude":
                return self.run(self.claude_argv(prompt, headless=True, tools=tools, budget=budget), log,
                                timeout=timeout, cwd=cwd)
            return self.codex_headless(prompt, log, sandbox=sandbox, cwd=cwd, timeout=timeout)
        argv = self.claude_argv(prompt, name=name, tools=tools) if cli == "claude" else self.codex_argv(prompt)
        self.launch_eph(name, argv, cwd=cwd)
        self.focus(cli, name)
        if until:
            ok, idle_since = None, None
            end = time.time() + timeout
            while time.time() < end and not ok:
                ok = until()
                # a session idle for 45 s straight has finished its turn: stop waiting for a condition
                # it can no longer meet (a refusal, a failed command), instead of the full timeout
                if not ok and self.idle(name):
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since > 45:
                        break
                else:
                    idle_since = None
                self.nap(3)
        else:
            ok = self.wait_idle(name, timeout)
        self.log(log, "pane %s: %s\n%s\n" % (name, "condition met" if ok else "gave up (timeout %ss, or the session went idle)" % timeout,
                                              self.screen(name, 80)))
        return subprocess.CompletedProcess([], 0 if ok else 124, "", "")

    def pane_reply(self, cli, rid, assistant_only=True):
        """Transcript text of the sessions whose prompt carried the run id `rid`."""
        d = self.scoped(cli, rid)
        try:
            return transcript_text(d, assistant_only)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # -- rows -------------------------------------------------------------------------------
    def row_boot(self, cli):
        now_part = ("NOW=<the text inside the square brackets of the line in your context that starts with "
                    "[now: (copy its weekday, date, time and zone)>")
        if cli == "claude":
            # What reaches Claude through session-notes: install.sh's standing-rules @import in
            # CLAUDE.md (heading below) and the time_sense hook. No runner-written import is judged.
            p = ("Reply with exactly one line of the form RULES=<heading> %s. <heading> is the text of the "
                 "first heading of the standing rules in your instructions." % now_part)
            rid = "BOOT%s%s" % (self.tok, cli[:2].upper())
            r = self.turn("claude", p + (" Run id %s." % rid if self.watch else ""), "boot-claude.txt", 180, "bootC",
                          until=lambda: clock_ok(self.pane_reply("claude", rid)))
            text = self.pane_reply("claude", rid) if self.watch else r.stdout
            if r.returncode != 0:
                return FAIL, "claude %s rc=%d" % ("pane" if self.watch else "-p", r.returncode), "boot-claude.txt"
            if "Standing rules (session-notes)" not in text:
                return FAIL, "the install-wired standing rules did not reach the model", "boot-claude.txt"
        else:
            p = ("Reply with exactly one line of the form MARK=<word> %s. <word> is the selftest marker word "
                 "from your session memory." % now_part)
            rid = "BOOT%s%s" % (self.tok, cli[:2].upper())
            r = self.turn("codex", p + (" Run id %s." % rid if self.watch else ""), "boot-codex.txt", 240, "bootX",
                          until=lambda: clock_ok(self.pane_reply("codex", rid)))
            text, mark = (self.pane_reply("codex", rid) if self.watch else r.stdout), "CODEXMARK" + self.tok
            if r.returncode != 0:
                return FAIL, "codex %s rc=%d" % ("pane" if self.watch else "exec", r.returncode), "boot-codex.txt"
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
        if self.watch:
            self.ephemeral.append("child")
            self.focus("claude", "child")
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
        def find_hit():
            hit = None
            for f in glob.glob(os.path.join(g, "*.md")):
                try:
                    with open(f) as fh:
                        if tok in fh.read():
                            hit = f
                except OSError:
                    pass
            return hit

        def proven():
            d = self.scoped("codex", tok)
            try:
                return codex_commit_proven(d)
            finally:
                shutil.rmtree(d, ignore_errors=True)

        until = lambda: find_hit() and (cli != "codex" or proven())
        r = self.turn(cli, ("/reflect  " if cli == "claude" else "$reflect  ") + ask, "reflect-%s.txt" % cli, 420,
                      "reflect" + cli[:2], until=until)
        if r.returncode != 0:
            return FAIL, "%s rc=%d" % (cli, r.returncode), "reflect-%s.txt" % cli
        hit = find_hit()
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
        def done():
            try:
                with open(note, encoding="utf-8") as fh:
                    if EM not in fh.read():
                        return False
            except OSError:
                return False
            return "EM-DASH GUARD" in self.pane_reply(cli, tok, assistant_only=False)

        if cli == "claude":
            r = self.turn("claude", "Use the Write tool to create the file %s %s" % (note, tail),
                          "emdash-claude.txt", 180, "emdC", until=done)
        else:
            r = self.turn("codex", "Use apply_patch to add a file %s %s" % (note, tail), "emdash-codex.txt", 240,
                          "emdX", until=done)
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
        refusal = re.compile(r"checkout_guard: \S*tracked\.txt is tracked on")
        r = self.turn("claude", "Use the Edit tool to replace the text 'original' with 'changed-%s' in %s. "
                      "Make exactly that one Edit call on exactly that path: this is a test of the edit guard, so do not "
                      "create a worktree, do not run wt, do not edit any other path. If the "
                      "call is refused, reply REFUSED and stop." % (tok, tracked),
                      "checkout-claude.txt", 180, "chkC", cwd=self.guard_repo,
                      until=lambda: refusal.search(self.pane_reply("claude", tok, assistant_only=False)))
        if r.returncode != 0:
            return FAIL, "claude %s rc=%d (the session did not run)" % ("pane" if self.watch else "-p", r.returncode), "checkout-claude.txt"
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

    def row_wt(self):
        with tempfile.TemporaryDirectory(dir=self.root) as d:
            st, detail = wt_row(os.path.join(self.repo, "bin", "wt.py"), os.path.realpath(d))
        return st, detail, None

    def live_file(self):
        return os.path.join(self.home, ".local", "state", "session-notes", "live-context.md")

    def row_live(self, cli):
        """The OTHER CLI's pane writes a note with live.py; a FRESH `cli` session then boots and its
        start-of-session injection (in its own transcript) must carry the note. The column is the reader."""
        other = "codex" if cli == "claude" else "claude"
        if not (self.have["claude"] and self.have["codex"]):
            return SKIP, "needs both CLIs", None
        writer = self.pane_pair(other)[0]
        if writer not in self.panes:
            return FAIL, "the %s writer pane %s is not running" % (other, writer), None
        tok = "LIVE%s%s" % (self.tok, cli[:2].upper())
        rid = "LR%s%s" % (self.tok, cli[:2].upper())
        self.focus(other, writer)
        self.wait_idle(writer)
        cmd = "python3 %s note %s" % (os.path.join(self.repo, "bin", "live.py"), tok)
        self.send(writer, "Run exactly this shell command, then reply DONE: %s" % cmd)

        def noted():
            try:
                with open(self.live_file(), encoding="utf-8") as fh:
                    return tok in fh.read()
            except OSError:
                return False

        if not self.wait_for(noted, 150):
            return FAIL, "the %s pane never wrote the note (live file has no %s)" % (other, tok), None
        name = "liveR" + cli[:2]
        prompt = "Reply with only the word READY %s" % rid
        argv = self.claude_argv(prompt, name=name) if cli == "claude" else self.codex_argv(prompt)
        self.launch_eph(name, argv)
        self.focus(cli, name)
        self.wait_idle(name, 150)
        raw = self.pane_reply(cli, rid, assistant_only=False)
        self.log("live-%s.txt" % cli, "reader transcript lines: %d\n" % len(raw.splitlines()))
        if not raw.strip():
            return FAIL, "the fresh %s session left no transcript (it never booted)" % cli, "live-%s.txt" % cli
        if not any(tok in ln and "Live context (newest first" in ln for ln in raw.splitlines()):
            return FAIL, ("the fresh %s session's start-of-session context does not carry the note the %s pane wrote"
                          % (cli, other)), "live-%s.txt" % cli
        return PASS, "a fresh %s session's start-of-session context carried the note the %s pane wrote" % (cli, other), None

    # -- review: each CLI runs the review skill on a PR against a local bare repo, stubbed gh ----
    def make_review_pr(self, cli):
        parent = os.path.join(self.root, "rv-" + cli[:2])
        os.makedirs(parent, exist_ok=True)
        cfg = os.path.join(parent, "wt.toml")
        with open(cfg, "w") as fh:
            fh.write('[wt]\ntoken_command = "echo faketoken"\nroot = "%s/wts"\n' % parent)
        home = os.path.join(parent, "h")
        os.makedirs(home, exist_ok=True)
        env = git_env(home)
        env.update(SESSION_NOTES_CONFIG=cfg, WT_GH=os.path.join(self.bin, "gh"))
        wt = [sys.executable, os.path.join(self.repo, "bin", "wt.py")]
        main = make_origin_repo(parent, "proj", env)
        path = sh(wt + ["new", "feat"], main, env).stdout.strip().splitlines()[-1]
        with open(os.path.join(path, "tracked.txt"), "a") as fh:
            fh.write("v2\n")
        sh(["git", "commit", "-qam", "Add a v2 line", "--no-verify"], path, env)
        sh(wt + ["pr", "--title", "Add a v2 line to tracked.txt"], path, env)
        head = sh(["git", "rev-parse", "HEAD"], path, env).stdout.strip()
        diff = sh(["git", "diff", "origin/main...HEAD"], path, env).stdout
        view = {"number": 7, "headRefOid": head, "state": "OPEN", "url": "https://example.invalid/o/r/pull/7",
                "title": "Add a v2 line to tracked.txt",
                "body": "Appends the line v2 to tracked.txt. A one line, correct change."}
        with open(os.path.join(self.ghdir, "scenario.json"), "w") as fh:
            json.dump({"view": view, "login": "tester", "comments": [], "diff": diff}, fh)
        try:
            os.remove(os.path.join(self.ghdir, "posted.jsonl"))
        except OSError:
            pass
        return {"path": path, "head": head, "env": env, "wt": wt}

    def trust_path(self, path):
        """Pre-trust a directory created mid-run, in the sandbox's Claude and Codex configs."""
        for p in {path, os.path.realpath(path)}:
            cj = os.path.join(self.claude_dir, ".claude.json")
            with open(cj) as fh:
                doc = json.load(fh)
            doc.setdefault("projects", {})[p] = {"hasTrustDialogAccepted": True, "allowedTools": []}
            with open(cj, "w") as fh:
                json.dump(doc, fh, indent=2)
            with open(os.path.join(self.codex_dir, "config.toml"), "a") as fh:
                fh.write('\n[projects."%s"]\ntrust_level = "trusted"\n' % p)

    def posted_comments(self):
        try:
            with open(os.path.join(self.ghdir, "posted.jsonl")) as fh:
                return [json.loads(x) for x in fh.read().splitlines() if x.strip()]
        except (OSError, ValueError):
            return []

    def ensure_wt_token_command(self):
        """Put `[wt] token_command` in the sandbox config exactly once. This row runs once per CLI;
        a second `[wt]` table is a TOML error on Python 3.11+ (tomllib), which every tool reads."""
        with open(self.cfg) as fh:
            text = fh.read()
        if re.search(r"(?m)^\[wt\]\s*$", text):
            return
        with open(self.cfg, "a") as fh:
            fh.write('\n[wt]\ntoken_command = "echo faketoken"\n')

    def row_review(self, cli):
        pr = self.make_review_pr(cli)
        self.trust_path(pr["path"])
        # Codex runs its commands in a login shell, whose PATH a real `gh` can win; a token command
        # that needs no gh keeps review.py on the stub (WT_GH names it for the other calls).
        self.ensure_wt_token_command()
        rules = ("This is a throwaway test repo with no checks: skip Step 1. In Step 3 spawn the reviewer with "
                 "%s instead of the model the skill names (a cost capped test). The change is one correct added "
                 "line, so the reviewer's verdict is APPROVE unless it finds a real defect. In Step 4 post "
                 "exactly what the reviewer returned, then stop." %
                 ("model haiku at low effort" if cli == "claude" else "model gpt-6.2-sol at low effort"))
        prompt = ("/review 7 " if cli == "claude" else "$review 7 ") + rules
        tools = self.CLAUDE_TOOLS + ["Agent"]
        r = self.turn(cli, prompt, "review-%s.txt" % cli, 720, "review" + cli[:2], cwd=pr["path"], tools=tools,
                      budget="2.00", until=lambda: bool(self.posted_comments()))
        posted = self.posted_comments()
        self.log("review-%s.txt" % cli, "\nposted comments: %s\n" % json.dumps(posted))
        if not posted:
            return FAIL, "the %s session ended (rc=%s) without the stub recording a verdict comment" % (cli, r.returncode), "review-%s.txt" % cli
        first = (posted[-1].get("body") or "").splitlines()[0] if posted[-1].get("body") else ""
        m = re.fullmatch(r"Review verdict: (APPROVE|CHANGES) ([0-9a-f]{40})", first)
        if not m:
            return FAIL, "the recorded comment does not start with the verdict marker: %r" % first[:80], "review-%s.txt" % cli
        if m.group(2) != pr["head"]:
            return FAIL, "the verdict names sha %s, not the PR head %s" % (m.group(2)[:10], pr["head"][:10]), "review-%s.txt" % cli
        if m.group(1) != "APPROVE":
            return FAIL, "the reviewer returned CHANGES on a one line correct change (marker and sha were well formed)", "review-%s.txt" % cli
        g = sh(pr["wt"] + ["merge", "feat", "--dry-run"], pr["path"], pr["env"], check=False)
        self.log("review-%s.txt" % cli, "wt merge --dry-run rc=%d\n%s\n%s\n" % (g.returncode, g.stdout, g.stderr))
        if g.returncode != 0 or "verdict gate passed" not in g.stdout:
            return FAIL, "wt merge --dry-run did not pass the gate on the posted APPROVE (rc=%d)" % g.returncode, "review-%s.txt" % cli
        try:
            with open(os.path.join(self.ghdir, "gh-calls.jsonl")) as fh:
                calls = [json.loads(x) for x in fh.read().splitlines() if x.strip()]
        except (OSError, ValueError):
            calls = []
        if any(c[:2] == ["pr", "merge"] for c in calls):
            return FAIL, "gh pr merge was called", "review-%s.txt" % cli
        return PASS, "verdict comment APPROVE at the PR head recorded; wt merge --dry-run passes the gate on it", None

    # -- env: render into the sandbox, then judge what each CLI actually loaded ------------------
    def env_apply(self):
        """Render a synthetic environment.md into the sandbox once. -> None, or a failure detail."""
        if getattr(self, "_env_applied", None) is not None:
            return self._env_applied or None
        src = os.path.join(self.root, "environment.md")
        with open(src, "w") as fh:
            fh.write("# Environment\n\n- The selftest marker for this run is ENVMARK%s.\n"
                     "- example.invalid is a trusted internal host.\n" % self.tok)
        r = self.run([sys.executable, os.path.join(self.repo, "bin", "environment_render.py"), "apply", "--yes"],
                     "env-apply.txt", cwd=self.repo, timeout=120)
        self._env_applied = "" if r.returncode == 0 else "environment_render apply rc=%d: %s" % (
            r.returncode, (r.stderr or r.stdout).strip()[-160:])
        return self._env_applied or None

    def show_command(self, cli, name, shell_cmd, timeout=60):
        """Run a shell command in its own pane (visible in watch mode) until it ends. The command
        writes its own output file; the caller reads that."""
        self.launch_eph(name, ["sh", "-c", "%s; sleep 4" % shell_cmd])
        self.focus(cli, name)
        self.wait_for(lambda: self.tmux("has-session", "-t", "=" + name).returncode != 0, timeout, 0.5)

    def row_env(self, cli):
        bad = self.env_apply()
        if bad:
            return FAIL, bad, "env-apply.txt"
        mark = "ENVMARK" + self.tok
        if cli == "claude":
            out = os.path.join(self.logs, "env-claude.txt")
            self.show_command(
                "claude", "envC", "%s auto-mode config > %s 2>&1; echo 'auto mode environment entries with the marker:'; "
                "grep -c %s %s" % (shlex.quote(self.claude_bin), shlex.quote(out), mark, shlex.quote(out)))
            try:
                with open(out, encoding="utf-8", errors="replace") as fh:
                    cfg = fh.read()
            except OSError:
                cfg = ""
            try:
                env_list = json.loads(cfg).get("environment", [])
            except (ValueError, AttributeError):
                return FAIL, "`claude auto-mode config` did not print JSON", "env-claude.txt"
            if not any(mark in str(e) for e in env_list):
                return FAIL, "`claude auto-mode config` does not list the rendered environment (no %s)" % mark, "env-claude.txt"
            return PASS, "`claude auto-mode config` lists the rendered environment entries", None
        # codex: a pane started WITHOUT the -a/-s flags, so /status shows what the rendered config sets
        name = "envX"
        argv = [self.codex_bin, "--no-daemon", "--dangerously-bypass-hook-trust", "-m", self.codex_model,
                "-c", "model_reasoning_effort=low"]
        self.launch_eph(name, argv)
        self.focus("codex", name)
        self.wait_idle(name, 120)
        self.send(name, "/status")
        want = self.CODEX_STATUS
        scr = ""
        end = time.time() + 40
        while time.time() < end:
            scr = self.screen(name, 60)
            if all(w in scr for w in want):
                break
            self.nap(2)
        self.log("env-codex.txt", "screen after /status:\n%s\n" % scr)
        missing = [w for w in want if w not in scr]
        if missing:
            return FAIL, "Codex /status does not show %s" % ", ".join(repr(w) for w in missing), "env-codex.txt"
        # negative control: the pane started with explicit -a/-s flags overrides the rendered config, so
        # its /status must NOT show the reviewer line (else the check above proves nothing)
        ctl = self.pane_pair("codex")[0]
        if ctl in self.panes:
            self.wait_idle(ctl, 60)
            self.send(ctl, "/status")
            self.nap(6)
            cscr = self.screen(ctl, 60)
            self.log("env-codex.txt", "control pane %s after /status:\n%s\n" % (ctl, cscr))
            if "Permissions" in cscr and want[-1] in cscr:
                return FAIL, "negative control: a pane started with -a/-s flags also shows %r, so /status does not tell the rendered config apart" % want[-1], "env-codex.txt"
        return PASS, "Codex /status shows %s (the rendered config); a flag-started pane does not" % " and ".join(repr(w) for w in want), None

    CODEX_STATUS = ("Workspace", "Approve for me")

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
            self.begin_step(fid, cli, quick=True)
            self.record(fid, cli, FAIL, "codex login failed", self.evid("codex-login.txt"))
            self.end_step(FAIL, quick=True)
            return
        if cli in ("claude", "codex") and not self.have[cli]:
            self.begin_step(fid, cli, quick=True)
            self.record(fid, cli, SKIP, self.why.get(cli, "cli unavailable"))
            self.end_step(SKIP, quick=True)
            return
        self.begin_step(fid, cli)
        try:
            status, detail, ev = fn(*args)
        except Abort:
            raise
        except Exception as exc:  # a crashing row is a FAIL, never a silent skip
            status, detail, ev = FAIL, "row crashed: %r" % (exc,), None
        self.record(fid, cli, status, detail, self.evid(ev) if ev else None)
        self.end_step(status, quick=(status == SKIP))

    ORDER = (("boot", "row_boot"), ("peers", "row_peers"), ("bcast", "row_bcast"), ("bus", "row_bus"),
             ("handoff", "row_handoff"), ("refresh", "row_refresh"), ("reflect", "row_reflect"),
             ("emdash", "row_emdash"), ("checkout", "row_checkout"))

    def build_plan(self):
        """Every step this run will take, in order: (feature, cli|both). The status file's `total`."""
        plan = []
        for fid, _fn in self.ORDER:
            if self.want(fid):
                plan += [(fid, "claude"), (fid, "codex")]
        for fid in ("wt", "hookpath"):
            if self.want(fid):
                plan.append((fid, "both"))
        for fid in ("review", "live", "env"):
            if self.want(fid):
                plan += [(fid, "claude"), (fid, "codex")]
        return plan + [("realdirs", "both")]

    def go(self):
        if self.watch:
            gone = empty_classes(self.needles, bool(getattr(self.a, "leak_terms", None)))
            if gone:  # a class with nothing to look for would let that kind of leak onto a public frame
                raise Abort("screen guard: nothing to scan for in class(es) %s (no passwd entry, hostname or "
                            "usable term found); refusing to start --watch" % ", ".join(gone))
        if self.watch:  # refuse before any cost: a leftover `sn-watch` link means another run (or a killed one)
            link = os.path.join(self.sock_dir, SOCKET_LABEL)
            if os.path.lexists(link):
                link_selftest_socket(self.sock_dir, "/nonexistent")  # raises the Abort naming the link
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
        self.plan = self.build_plan()
        self.stub_bins()
        self.clone_and_install()
        self.seed_claude()
        self.seed_codex()
        if self.have["codex"] and not self.codex_login(ok):
            self.have["codex"], self.login_failed = False, True
        ok = None
        self.start_tmux()
        if self.watch:
            self.link_socket()
            print("watch: attach with   tmux -L sn-watch attach\nwatch: status file %s" % self.status_path)
            sys.stdout.flush()
        if any(self.want(f) for f in ("peers", "bcast", "bus", "refresh", "live")):
            if self.have["claude"]:
                for n in ("cA", "cB"):
                    self.launch(n, self.claude_argv("Reply with only the word READY", name=n))
            if self.have["codex"]:
                for n in ("xA", "xB"):
                    self.launch(n, self.codex_argv("Reply with only the word READY"))
            self.default_focus()
            for n in sorted(self.panes):
                self.wait_idle(n, 120)
        for fid, name in self.ORDER:
            for cli in ("claude", "codex"):
                self.do(fid, cli, getattr(self, name), cli)
        self.do("wt", "both", self.row_wt)
        self.do("hookpath", "both", self.row_hookpath)
        for fid, name in (("review", "row_review"), ("live", "row_live"), ("env", "row_env")):
            for cli in ("claude", "codex"):
                self.do(fid, cli, getattr(self, name), cli)
        if not self.watch:
            self.stop_tmux()

    def finish_realdirs(self):
        """The real-dirs row runs whenever install.sh ran, under any --feature filter and even
        when go() aborted or was interrupted: a leak is likeliest exactly then."""
        if not self.installed or self.before is None:
            return
        if self.watch:
            self.begin_step("realdirs", "both", quick=True)
            self.stop_inner()
        else:
            self.stop_tmux()
        try:
            status, detail, ev = self.row_realdirs()
        except Exception as exc:
            status, detail = FAIL, "real-dirs check crashed: %r" % (exc,)
        self.record("realdirs", "both", status, detail)
        if self.watch:
            self.end_step(status, quick=True)

    def teardown(self, failed):
        try:
            self.unlink_socket()  # first: the `sn-watch` link must not outlive any exit path
        except Exception:
            pass
        try:
            os.remove(os.path.join(self.codex_dir, "auth.json"))
        except OSError:
            pass
        try:
            self.stop_tmux()
        except Exception:
            pass
        for fd in (self.fifo_rd, self.fifo_wr):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fifo_rd = self.fifo_wr = None
        if self.a.keep or (failed and not self.quit):
            return self.root
        shutil.rmtree(self.root, ignore_errors=True)
        return None


def main():
    ap = argparse.ArgumentParser(description="Live selftest: real Claude Code and Codex sessions in a sandbox")
    ap.add_argument("--only", choices=("claude", "codex"))
    ap.add_argument("--feature", type=lambda s: set(s.split(",")), help="comma list of: " + ",".join(f for f, _, _ in FEATURES))
    ap.add_argument("--keep", action="store_true", help="keep the sandbox and print its path")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--watch", action="store_true",
                    help="one tmux window to watch and film: status pane, Claude left, Codex right (attach: tmux -L sn-watch attach)")
    ap.add_argument("--pace", type=float, default=None,
                    help="watch mode: scale the holds on each step's title (~2 s) and result (~4 s); default 1, 0 = none")
    ap.add_argument("--step", action="store_true", help="watch mode: hold each step until Enter is pressed in the status pane")
    ap.add_argument("--leak-terms", metavar="FILE", help="watch mode: also abort when any term in FILE (one per line) is on screen")
    ap.add_argument("--no-hold-final", action="store_true", help="watch mode: tear down when the run ends instead of waiting for q")
    ap.add_argument("--status-pane", nargs=2, metavar=("STATUS", "FIFO"), help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.status_pane:
        status_pane_main(*a.status_pane)
        return 0
    if a.selftest:
        return selftest()
    if not a.watch and (a.pace is not None or a.step or a.leak_terms or a.no_hold_final):
        print("--pace, --step, --leak-terms and --no-hold-final need --watch", file=sys.stderr)
        return 2
    if a.pace is not None and a.pace < 0:
        print("--pace must be 0 or more", file=sys.stderr)
        return 2
    if a.leak_terms and not os.path.isfile(a.leak_terms):
        print("--leak-terms: no such file %s" % a.leak_terms, file=sys.stderr)
        return 2
    known = {f for f, _, _ in FEATURES}
    if a.feature and not a.feature <= known:
        print("unknown feature(s): %s" % ", ".join(sorted(a.feature - known)), file=sys.stderr)
        return 2
    live = Live(a)
    results, lines, kept = execute(live)
    out = [render_table(results, pane_rows=PANE_ROWS if live.watch else ())] + [redact(ln) for ln in lines]
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
        live.stopping = live.stopping or bool(err)
        if err:
            live.mark_aborted(err)  # q, Ctrl-C, a crash, any Abort: the status file says why
        try:
            live.finish_realdirs()
        except KeyboardInterrupt:
            err = err or "interrupted"
        except Abort as exc:  # the screen guard can trip while the last step runs
            err = err or str(exc)
            live.mark_aborted(err)
        if err:
            # its own row: filing it under realdirs read as "the real dirs were touched" when no
            # CLI had a key and nothing ran (realdirs keeps its own row, when it was measured)
            live.results.append({"feature": "run", "title": RUN_TITLE,
                                 "cli": "both", "status": FAIL, "detail": "run aborted: " + err, "evidence": None})
        lines = fail_lines(live.results)
        try:
            live.finish_watch(wait=not err)
        except KeyboardInterrupt:
            pass
        except Abort as exc:  # a guard trip in the final hold: still a failed run, with its message
            if not err:
                err = str(exc)
                live.results.append({"feature": "run", "title": RUN_TITLE, "cli": "both", "status": FAIL,
                                     "detail": "run aborted: " + err, "evidence": None})
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
