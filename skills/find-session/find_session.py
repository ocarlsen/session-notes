#!/usr/bin/env python3
"""find_session.py -- search Claude Code session transcripts for a remembered
conversation, and say WHICH tmux session it is (live) or how to resume it (ended).

Mechanics live here, not in SKILL.md prose, on purpose: a step a SKILL only
*describes* is an instruction to a model and goes missing silently.
Everything below is the part that must not be re-derived per search.

Four traps this encodes, each found by measuring real searches:
  1. isSidechain entries (subagent and reviewer prompts) dominate the corpus
     and drown real conversation. Always dropped.
  2. A bare word like "meter" matches "parameter": one live session showed 91
     hits, all noise, with the real hit buried. Bare words are \\b-anchored.
  3. Searching only the human's turns misses the answer whenever it lives in
     the assistant's reply (the human pasted a screenshot, or wrote two words).
     Both roles are searched by default.
  4. The pane registry (hooks/session_pane_registry.py) goes STALE: a pane
     whose claude process was restarted still carries the old session id, and
     that id may have no transcript at all. Liveness is confirmed by matching
     the registry's claude_pid against tmux's CURRENT pane_pid, never by the
     registry alone.

Paths come from the config: transcripts under
<paths.claude_home>/projects, the registry at <paths.state_dir>/session_panes.json.
Pane detection is bin/session_peers.py's.

Never persist output: transcript content is judgment-only, and a transcript
is more sensitive than a pane snapshot, not less.

    find_session.py [--all] [--since DAYS] [--role any|user|assistant] WORDS
    find_session.py --selftest
"""
import argparse
import glob
import json
import os
import re
import sys
import time

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__)))), "bin")
sys.path.insert(0, BIN)
import notes_config  # noqa: E402
import session_peers   # noqa: E402

REGISTRY_NAME = "session_panes.json"


def paths(cfg=None):
    """(transcripts_dir, registry_path) from the config."""
    cfg = cfg or notes_config.load()
    return (os.path.join(cfg["paths"]["claude_home"], "projects"),
            os.path.join(cfg["paths"]["state_dir"], REGISTRY_NAME))

# Entries that are plumbing rather than conversation. A match inside one of
# these is never the thing anyone is looking for.
SKIP_MARKERS = ("<system-reminder>", "<task-notification>", "<command-name>",
                "<local-command-stdout>", "<bash-stdout>")
SKIP_PREFIXES = ("You are ", "Caveat:", "This session is being continued",
                 "<!--", "/private/tmp/")


def anchor(pattern, raw=False):
    """Bare alphanumeric words get \\b anchors so 'meter' cannot match
    'parameter'. A pattern carrying regex metacharacters is passed through
    untouched: the caller asked for a regex, so give them one."""
    if raw or re.search(r"[\\\[\](){}|^$*+?]", pattern):
        return pattern
    return r"\b" + r"\b.{0,80}\b".join(re.escape(w) for w in pattern.split()) + r"\b"


def literal_prefilter(pattern):
    """Longest literal word in the pattern, lowercased, for a cheap substring
    test before JSON parsing. None when the pattern has no usable literal."""
    words = re.findall(r"[A-Za-z]{4,}", pattern)
    return max(words, key=len).lower() if words else None


def live_sessions(registry, panes=None):
    """(mapping, unmapped) where mapping is {session_id: (tmux_name, cwd)} for
    panes whose REGISTERED claude pid still matches the pane's current pid, and
    unmapped is the count of live claude panes we could not attribute.

    `panes` is session_peers.list_peers() output (fetched when omitted); None
    means tmux is unreachable, which means 'cannot tell': every session is then
    reported as ended-or-unknown rather than claiming any of them is live.

    The pid match is the whole point: a pane restarted under a new pid still
    carries its old registration, and that session id may have no transcript
    on disk at all. Trusting the registry alone would report a session that
    does not exist.
    """
    try:
        with open(registry) as fh:
            reg = json.load(fh)
        reg = reg if isinstance(reg, dict) else {}
    except Exception:
        reg = {}
    if panes is None:
        panes = session_peers.list_peers()
    by_id = {p["pane_id"]: p for p in (panes or [])}
    mapping, claimed = {}, set()
    for pane_id, entry in reg.items():
        pane = by_id.get(pane_id)
        if pane is None or not isinstance(entry, dict):
            continue
        if str(entry.get("claude_pid") or "") != pane["pane_pid"]:
            continue          # pane restarted: registration is stale
        mapping[entry.get("session_id")] = (pane["tmux_session"], entry.get("cwd", ""))
        claimed.add(pane_id)
    unmapped = sum(1 for pid, p in by_id.items()
                   if pid not in claimed and p["is_claude"])
    return mapping, unmapped


def transcripts_for(transcripts, session_ids):
    files = []
    for sid in session_ids:
        files.extend(glob.glob(os.path.join(transcripts, "*", "%s.jsonl" % sid)))
    return files


def all_transcripts(transcripts, since_days=None):
    files = glob.glob(os.path.join(transcripts, "*", "*.jsonl"))
    if since_days:
        cut = time.time() - float(since_days) * 86400
        files = [f for f in files if os.path.getmtime(f) >= cut]
    return files


def texts_of(entry, roles):
    etype = entry.get("type")
    if etype not in roles:
        return []
    if entry.get("isSidechain") or entry.get("isMeta"):
        return []
    if etype == "user" and entry.get("userType") not in (None, "external"):
        return []
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        blocks = [content]
    elif isinstance(content, list):
        blocks = [b.get("text", "") for b in content
                  if isinstance(b, dict) and b.get("type") == "text"]
    else:
        blocks = []
    out = []
    for text in blocks:
        text = (text or "").strip()
        if not text or text.startswith(SKIP_PREFIXES):
            continue
        if any(m in text for m in SKIP_MARKERS):
            continue
        out.append(text)
    return out


def search_file(path, rx, roles, prefilter, window):
    hits = []
    try:
        fh = open(path, errors="replace")
    except OSError:
        return hits
    with fh:
        for lineno, line in enumerate(fh, 1):
            if prefilter and prefilter not in line.lower():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue          # torn last line of a live append target
            for text in texts_of(entry, roles):
                m = rx.search(text)
                if not m:
                    continue
                start = max(0, m.start() - window)
                snippet = text[start:m.end() + window].replace("\n", " ⏎ ")
                if start:
                    snippet = "…" + snippet
                hits.append({
                    "ts": (entry.get("timestamp") or "")[:16].replace("T", " "),
                    "line": lineno,
                    "role": entry.get("type"),
                    "text": snippet,
                })
                break             # one hit per entry is enough to find it
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Find which Claude session discussed something.")
    ap.add_argument("pattern", help="words to find (bare words are \\b-anchored)")
    ap.add_argument("--all", action="store_true",
                    help="search every transcript, not just live tmux sessions")
    ap.add_argument("--since", metavar="DAYS",
                    help="with --all, only transcripts touched in the last DAYS")
    ap.add_argument("--role", choices=("any", "user", "assistant"), default="any",
                    help="whose words to match (default: any)")
    ap.add_argument("--raw", action="store_true",
                    help="treat the pattern as a regex, no \\b anchoring")
    ap.add_argument("--limit", type=int, default=4,
                    help="hits shown per session (default: 4)")
    ap.add_argument("--window", type=int, default=110,
                    help="context characters either side of the match")
    args = ap.parse_args(argv)
    transcripts, registry = paths()

    try:
        rx = re.compile(anchor(args.pattern, args.raw), re.I)
    except re.error as exc:
        print("bad pattern: %s" % exc, file=sys.stderr)
        return 2
    roles = {"any": ("user", "assistant")}.get(args.role, (args.role,))
    prefilter = literal_prefilter(args.pattern)

    mapping, unmapped = live_sessions(registry)
    if args.all:
        files = all_transcripts(transcripts, args.since)
        scope = "all transcripts" + (" (last %s days)" % args.since if args.since else "")
    else:
        files = transcripts_for(transcripts, mapping)
        scope = "%d live tmux session(s)" % len(files)

    results = []
    for path in files:
        hits = search_file(path, rx, roles, prefilter, args.window)
        if not hits:
            continue
        sid = os.path.basename(path)[:-6]
        results.append((hits[-1]["ts"], sid, path, hits))
    results.sort()

    print("searched %s for /%s/\n" % (scope, rx.pattern))
    for _ts, sid, path, hits in results:
        if sid in mapping:
            name, cwd = mapping[sid]
            where = "tmux session '%s'  (%s)" % (name, os.path.basename(cwd) or cwd)
            how = "tmux attach -t %s" % name
        else:
            where = "(ended)  %s" % os.path.basename(os.path.dirname(path))
            how = "claude --resume %s" % sid
        print("== %s\n   %s   %d hit(s)" % (where, how, len(hits)))
        for h in hits[-args.limit:]:
            print("   %s  %-9s L%-6d %s" % (h["ts"], h["role"], h["line"], h["text"]))
        print()

    if not results:
        print("no matches.")
        if not args.all:
            print("live sessions only; retry with --all to search ended ones.")
    if unmapped:
        print("note: %d live claude pane(s) could not be attributed to a "
              "transcript (started before the registry hook, or restarted); "
              "they were NOT searched. Use --all to cover them." % unmapped)
    return 0


def selftest():
    import contextlib
    import io
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    check("bare word anchored", re.search(anchor("meter"), "a parameter") is None
          and re.search(anchor("meter"), "the meter read") is not None)
    check("regex passed through", anchor("a|b") == "a|b")
    check("prefilter picks longest", literal_prefilter("box dimensions") == "dimensions")

    def entry(etype, text, **kw):
        e = {"type": etype, "timestamp": "2026-01-02T03:04:05Z",
             "message": {"content": text}}
        e.update(kw)
        return json.dumps(e)

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        conf = os.path.join(home, "config.toml")
        with open(conf, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/ch"\nstate_dir = "%s/st"\n' % (home, home))
        os.environ["SESSION_NOTES_CONFIG"] = conf
        transcripts, registry = paths()
        check("paths from config", transcripts == os.path.join(home, "ch", "projects")
              and registry == os.path.join(home, "st", REGISTRY_NAME))

        proj = os.path.join(transcripts, "-tmp-alpha")
        os.makedirs(proj)
        with open(os.path.join(proj, "sid-alpha.jsonl"), "w") as fh:
            fh.write("\n".join([
                entry("user", "how big is the box"),
                entry("assistant", [{"type": "text", "text": "The box dimensions are 4x6."}]),
                entry("user", "box dimensions from a subagent", isSidechain=True),
                entry("user", "<system-reminder>box dimensions</system-reminder>"),
                '{"torn',
            ]) + "\n")
        with open(os.path.join(proj, "sid-beta.jsonl"), "w") as fh:
            fh.write(entry("user", "nothing relevant") + "\n")

        rx = re.compile(anchor("dimensions"), re.I)
        hits = search_file(os.path.join(proj, "sid-alpha.jsonl"), rx,
                           ("user", "assistant"), "dimensions", 40)
        check("assistant reply found, plumbing skipped",
              len(hits) == 1 and hits[0]["role"] == "assistant")
        check("role filter", search_file(os.path.join(proj, "sid-alpha.jsonl"), rx,
                                         ("user",), None, 40) == [])

        os.makedirs(os.path.dirname(registry))
        with open(registry, "w") as fh:
            json.dump({"%1": {"session_id": "sid-alpha", "claude_pid": "111", "cwd": "/tmp/alpha"},
                       "%2": {"session_id": "sid-stale", "claude_pid": "999", "cwd": "/tmp/beta"}},
                      fh)
        panes = [
            {"pane_id": "%1", "pane_pid": "111", "tmux_session": "alpha", "is_claude": True},
            {"pane_id": "%2", "pane_pid": "222", "tmux_session": "beta", "is_claude": True},
            {"pane_id": "%3", "pane_pid": "333", "tmux_session": "shell", "is_claude": False},
        ]
        mapping, unmapped = live_sessions(registry, panes)
        check("live pane attributed", mapping == {"sid-alpha": ("alpha", "/tmp/alpha")})
        check("restarted pane is unmapped", unmapped == 1)
        check("no tmux = nothing live", live_sessions(registry, [])[0] == {})

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--all", "dimensions"])
        out = buf.getvalue()
        check("cli --all finds ended session", rc == 0 and "claude --resume sid-alpha" in out
              and "sid-beta" not in out)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    sys.exit(main())
