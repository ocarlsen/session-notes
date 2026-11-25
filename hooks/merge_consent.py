#!/usr/bin/env python3
"""UserPromptSubmit hook: record the user's yes to ONE specific merge.

`wt merge` refuses unless the human said yes to that merge. The session ends its reply with a
question naming exactly one PR, `merge #N?`, and the user answers with a bare affirmative
("yes", "y", "yep", "yeah", "ok", "go", "do it", "merge it", any case, trailing punctuation
ok) or types `merge #N`. This hook turns that human prompt into a grant `wt merge` consumes.
It only RECORDS: it never blocks, alters or answers a prompt, prints nothing, and exits 0 on
any error of its own (a message to stderr).

Limits (docs/tools.md, "What the gate does and does not protect"): this stops a session that follows
its refusals, not one that forges. Any same-user process can write the grant file; a session can
type into its own pane, preset CLAUDE_CODE_ENTRYPOINT, `--resume <id> "merge #N"` or `codex exec`;
any session can spend a grant.

A prompt is NOT a human answer, and grants nothing, when it is:
  * the first prompt this hook sees in a session (a handoff launch passes its task as argv);
  * a bus nudge (contains the bus_nudge marker) or a bcast line (contains the bcast prefix);
  * a headless run (CLAUDE_CODE_ENTRYPOINT starting with "sdk"). Otherwise, when unsure, nothing.

Grant rules:
  * Claude Code: the hook input carries `transcript_path`; the last assistant text in that
    JSONL is the question. Its FINAL SENTENCE must BE the ask: `merge #N?`, optionally after a
    short lead-in ending in `:` or followed by ` in owner/repo`, no other `#N`, no negation.
    The prompt must be an affirmative, and no human prompt may sit between that reply and now.
    A yes to a sentence naming two PRs, none, or asking something else grants nothing.
  * Either harness: the prompt exactly `merge #N` (one N) grants #N, even with no prior question.
  * Codex: the UserPromptSubmit input carries `transcript_path` too (a rollout JSONL). The
    question is the newest `event_msg` / `task_complete` `last_agent_message`, falling back to
    the newest assistant `response_item` message. Same rules as Claude Code.
  * A human prompt that is not a grant leaves existing grants alone; grants expire after 30
    minutes and are single use (wt marks them used before it merges).

The grant records the repo, resolved in this order: owner/repo of the session cwd's `origin`; the
one `owner/repo` or github PR URL the reply's final paragraph names; the one git checkout child of
a configured scope root that the cwd is (its `origin` on github). When none resolves the repo is
null and `wt merge` REFUSES that grant (it never matches by PR number alone). Also the PR number, the time, the user's
prompt verbatim and the session id, in `~/.local/state/session-notes-consent/grants.json` under the passwd home of the user
(never $HOME, never the config). checkout_guard refuses the ordinary ways an agent could write it. `wt.py` imports this module for the
grant lookup, so the format has one definition.

    python3 merge_consent.py --selftest
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin")
sys.path.insert(0, BIN)

TTL = 30 * 60
KEEP = 24 * 3600
NUDGE_MARKER_FALLBACK = "[bus-nudge]"
BCAST_PREFIX_FALLBACK = "[user via bcast]"
AFFIRMATIVES = {"yes", "y", "yep", "yeah", "ok", "go", "do it", "merge it"}
_EXPLICIT = re.compile(r"^merge\s+#(\d+)$", re.I)
_TAIL_PUNCT = re.compile(r"[\s.!,]+$")
_CLOSERS = "`*_\"')]>"
TRANSCRIPT_TAIL = 8 * 1024 * 1024


def _cfg(cfg=None):
    if cfg is not None:
        return cfg
    import notes_config
    return notes_config.load()


# Test-only: a selftest patches this module constant in-process. It is never read from the
# environment or the config, so no variable on a command line can move the grant store.
GRANTS_PATH_OVERRIDE = None
GRANTS_REL = (".local", "state", "session-notes-consent", "grants.json")


def grants_path():
    """The grant file: the real home of the running user (the passwd entry, NOT $HOME) plus a
    fixed relative path, independent of SESSION_NOTES_CONFIG and of the configured state_dir.
    The hook, `wt merge` and checkout_guard all call this one function."""
    if GRANTS_PATH_OVERRIDE:
        return GRANTS_PATH_OVERRIDE
    import pwd
    return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, *GRANTS_REL)


def stamp(now, cfg=None):
    """Local time with a zone label, in the configured time.timezone (empty = system zone)."""
    name = (_cfg(cfg).get("time") or {}).get("timezone", "")
    if name:
        from zoneinfo import ZoneInfo
        t = datetime.fromtimestamp(now, ZoneInfo(name))
    else:
        t = datetime.fromtimestamp(now).astimezone()
    return t.strftime("%Y-%m-%d %H:%M ") + (t.tzname() or "local")


# ------------------------------------------------------------------ the grant file
class _Lock:
    def __init__(self, path):
        self.path = path + ".lock"
        self.fh = None

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        self.fh.close()


def _read(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        g = d.get("grants")
        return [x for x in g if isinstance(x, dict)] if isinstance(g, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def _write(path, grants):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump({"grants": grants}, fh, indent=1)
    os.replace(tmp, path)


def _live(g, now):
    return not g.get("used") and now - float(g.get("ts") or 0) <= TTL


def _matches(g, repo, pr):
    gr = g.get("repo")
    return g.get("pr") == pr and bool(gr) and bool(repo) and gr.lower() == repo.lower()


def add_grant(grant, now):
    path = grants_path()
    with _Lock(path):
        keep = [g for g in _read(path) if now - float(g.get("ts") or 0) <= KEEP
                and not (_live(g, now) and g.get("pr") == grant["pr"] and g.get("repo") == grant["repo"])]
        keep.append(grant)
        _write(path, keep)


def find_grant(repo, pr, now=None):
    """The newest unused, unexpired grant for (repo, pr), or None. Does not consume it."""
    now = time.time() if now is None else now
    hits = [g for g in _read(grants_path()) if _live(g, now) and _matches(g, repo, pr)]
    return max(hits, key=lambda g: g.get("ts") or 0) if hits else None


def find_null_repo_grant(pr, now=None):
    """A live grant for `pr` whose repo could not be resolved (wt refuses it, saying why)."""
    now = time.time() if now is None else now
    return next((g for g in _read(grants_path()) if _live(g, now) and g.get("pr") == pr
                 and not g.get("repo")), None)


def consume_grant(repo, pr, head, now=None):
    """Mark the newest live grant for (repo, pr) used and bind it to `head`. Returns the entry,
    or None when there is none (so a second call with the same grant gets None)."""
    now = time.time() if now is None else now
    path = grants_path()
    with _Lock(path):
        grants = _read(path)
        hits = [g for g in grants if _live(g, now) and _matches(g, repo, pr)]
        if not hits:
            return None
        g = max(hits, key=lambda x: x.get("ts") or 0)
        g["used"] = True
        g["used_at"] = now
        g["head"] = head
        _write(path, grants)
        return dict(g)


# ------------------------------------------------------------------ reading the prompt
def repo_identity(cwd, github_only=False):
    """owner/repo (lowercase) from the cwd's `origin`, or None."""
    try:
        r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    url = r.stdout.strip()
    if github_only and "github.com" not in url:
        return None
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url) if r.returncode == 0 else None
    return ("%s/%s" % (m.group(1), m.group(2))).lower() if m else None


def _blocks_text(content, kinds):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in kinds)
    return ""


def _human_claude(e):
    """The text of a Claude Code transcript entry that is a human prompt, else None (tool
    results, injected meta entries and non-message entries are not)."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain"):
        return None
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return _blocks_text(c, ("text",)) or "[non-text prompt]"
    return None


def last_assistant_text(path, prompt=None):
    """The previous assistant reply of a Claude Code transcript or Codex rollout, or None. None
    too when a human prompt (or a second Codex turn start) sits between that reply and now: the
    yes then answers something else. `prompt` is the current prompt, skipped when the harness
    already wrote it to the transcript. Position decides, newest first: a Codex task_complete
    and an assistant response_item are the same reply, whichever is newer is taken."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TRANSCRIPT_TAIL))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = raw.splitlines()
    if size > TRANSCRIPT_TAIL:
        lines = lines[1:]
    cur = (prompt or "").strip()
    started = 0
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind, pl = e.get("type"), e.get("payload")
        h = _human_claude(e)
        if h is not None:
            if h.strip() == cur:
                continue
            return None
        if kind == "assistant" and not e.get("isSidechain"):
            msg = e.get("message")
            text = _blocks_text(msg.get("content") if isinstance(msg, dict) else None, ("text",))
            if text.strip():
                return text
        if not isinstance(pl, dict):
            continue
        ptype = pl.get("type")
        if kind == "event_msg" and ptype == "task_complete":
            text = pl.get("last_agent_message")
            if isinstance(text, str) and text.strip():
                return text
        elif kind == "event_msg" and ptype == "task_started":
            started += 1
            if started > 1:
                return None
        elif (kind == "event_msg" and ptype == "user_message") or (
                kind == "response_item" and ptype == "message" and pl.get("role") == "user"):
            text = pl.get("message") if ptype == "user_message" else _blocks_text(
                pl.get("content"), ("input_text", "text"))
            text = (text or "").strip()
            if text == cur or text.startswith("<") or not text:
                continue  # the current prompt, or injected context
            return None
        elif kind == "response_item" and ptype == "message" and pl.get("role") == "assistant":
            text = _blocks_text(pl.get("content"), ("output_text", "text"))
            if text.strip():
                return text
    return None


def final_question(text):
    """The last sentence of the message when the message ends in a question, else None."""
    t = (text or "").strip().rstrip(_CLOSERS + " \t\r\n")
    if not t.endswith("?"):
        return None
    para = re.split(r"\n\s*\n", t)[-1].strip()
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n", para) if s.strip()]
    return sentences[-1] if sentences else None


_ASK = re.compile(r"^(?:(?P<lead>[^:?#]{1,60}):\s*)?merge\s+#(?P<n>\d+)"
                  r"(?:\s+in\s+(?P<repo>[\w.-]+/[\w.-]+))?\s*\?$", re.I)
_NEGATION = re.compile(r"\b(not|no|never|don'?t|do not|hold|wait|skip|without)\b|n't", re.I)


def parse_ask(text):
    """(N, repo or None) when the reply's FINAL sentence IS the ask: `merge #N?`, optionally
    preceded by a short lead-in ending in `:` (no negation) or followed by ` in owner/repo`,
    with no other `#N` in it. Else None."""
    q = final_question(text)
    if not q:
        return None
    # code and bold marks go anywhere; an underscore (emphasis) only at a word edge, so my_repo survives
    q = re.sub(r"[`*]", "", q)
    q = re.sub(r"(?:^|(?<=\s))_+(?=\S)|(?<=\S)_+(?=\s|$)", "", q).strip().strip("_\"'()[] ").strip()
    m = _ASK.match(q)
    if not m or len(re.findall(r"#\d+", q)) != 1:
        return None
    if m.group("lead") and _NEGATION.search(m.group("lead")):
        return None
    repo = m.group("repo")
    return int(m.group("n")), (repo.lower() if repo else None)


def asked_pr(text):
    a = parse_ask(text)
    return a[0] if a else None


_PR_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/\d+", re.I)
_OWNER_REPO = re.compile(r"(?<![\w/.:-])([\w-]+)/([\w][\w.-]*)(?![\w/-])")


def _final_para(text):
    return re.split(r"\n\s*\n", (text or "").strip())[-1] if final_question(text) else ""


def explicit_repo(text):
    """The repo the reply states outright: `merge #N in owner/repo?` or one github PR URL in the
    final paragraph. None when absent or when it names two."""
    ask = parse_ask(text)
    urls = {("%s/%s" % m).lower() for m in _PR_URL.findall(_final_para(text))}
    named = ({ask[1]} if ask and ask[1] else set()) | urls
    return next(iter(named)) if len(named) == 1 else None


def repo_from_reply(text):
    """Fallback when the cwd has no repo: the explicit repo, else the one bare owner/repo in the
    final paragraph."""
    e = explicit_repo(text)
    if e:
        return e
    para = _PR_URL.sub(" ", _final_para(text))
    bare = {("%s/%s" % m).lower().rstrip(".") for m in _OWNER_REPO.findall(para)}
    return bare.pop() if len(bare) == 1 else None


def repo_from_scope(cwd, cfg):
    """The one git checkout child of the configured scope root `cwd` is, on github, else None."""
    real = os.path.realpath(cwd)
    for sc in cfg.get("scopes") or []:
        root = sc.get("root")
        if not root or os.path.realpath(root) != real:
            continue
        try:
            kids = [os.path.join(real, n) for n in sorted(os.listdir(real))
                    if os.path.exists(os.path.join(real, n, ".git"))]
        except OSError:
            return None
        return repo_identity(kids[0], github_only=True) if len(kids) == 1 else None
    return None


def resolve_repo(cwd, reply, cfg):
    """(repo, ok). ok is False when the reply names a repo other than the cwd's origin."""
    here, named = repo_identity(cwd), explicit_repo(reply)
    if here and named and here != named:
        return None, False
    return here or repo_from_reply(reply) or repo_from_scope(cwd, cfg), True


def classify(prompt):
    """('affirm', None) | ('explicit', N) | (None, None)."""
    p = _TAIL_PUNCT.sub("", (prompt or "").strip())
    m = _EXPLICIT.match(p)
    if m:
        return "explicit", int(m.group(1))
    if p.lower() in AFFIRMATIVES:
        return "affirm", None
    return None, None


def _markers(cfg):
    nudge, bcast = NUDGE_MARKER_FALLBACK, BCAST_PREFIX_FALLBACK
    try:
        bcast = cfg["bcast"]["prefix"] or bcast
    except (KeyError, TypeError):
        pass
    try:
        import bus_nudge
        nudge = bus_nudge.NUDGE.split("]", 1)[0] + "]"
    except Exception:
        pass
    return nudge, bcast


def _first_prompt(session, now, cfg):
    """True when this session has no prompt on record; records this one either way."""
    d = os.path.join(os.path.dirname(grants_path()), "seen")  # beside the grants, never the config state dir
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, session)
    first = not os.path.exists(p)
    with open(p, "w") as fh:
        fh.write(str(now))
    try:
        for e in os.scandir(d):
            if e.is_file() and now - e.stat().st_mtime > 30 * 86400:
                os.unlink(e.path)
    except OSError:
        pass
    return first


def decide(payload, now, cfg=None, env=None):
    """The grant dict this prompt earns, or None. Records the session's prompt as seen."""
    cfg = _cfg(cfg)
    env = os.environ if env is None else env
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return None
    session = str(payload.get("session_id") or "default")
    session = "".join(c for c in session if c.isalnum() or c in "-_")[:64] or "default"
    first = _first_prompt(session, now, cfg)
    nudge, bcast = _markers(cfg)
    if first or nudge in prompt or bcast in prompt:
        return None
    if str(env.get("CLAUDE_CODE_ENTRYPOINT", "")).startswith("sdk"):
        return None
    kind, n = classify(prompt)
    reply = (last_assistant_text(payload["transcript_path"], prompt)
             if payload.get("transcript_path") else None)
    if kind == "affirm":
        n = asked_pr(reply) if reply else None
    if not n:
        return None
    repo, ok = resolve_repo(payload.get("cwd") or os.getcwd(), reply, cfg)
    if not ok:
        return None
    return {"repo": repo, "pr": n, "ts": now,
            "prompt": prompt, "session": session, "used": False}


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        now = time.time()
        cfg = _cfg()
        g = decide(payload, now, cfg)
        if g:
            add_grant(g, now)
    except Exception as exc:  # a crashing prompt hook disrupts the user
        sys.stderr.write("merge_consent: %s: %s\n" % (type(exc).__name__, exc))
    return 0


# ------------------------------------------------------------------ selftest
def selftest():
    import tempfile
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
        if not cond:
            fails.append(name)

    import notes_config
    global GRANTS_PATH_OVERRIDE
    with tempfile.TemporaryDirectory() as t0:
        T = os.path.realpath(t0)
        GRANTS_PATH_OVERRIDE = os.path.join(T, "grants.json")
        cfgf = os.path.join(T, "config.toml")
        with open(cfgf, "w") as fh:
            fh.write('[paths]\nstate_dir = "%s"\n' % os.path.join(T, "state"))
        cfg = notes_config.load(cfgf)
        repo = os.path.join(T, "r")
        os.makedirs(repo)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin",
                        "ssh://host.invalid/Example/Proj.git"], check=True)
        tp = os.path.join(T, "t.jsonl")

        def transcript(*texts):
            with open(tp, "w") as fh:
                fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
                for t in texts:
                    fh.write(json.dumps({"type": "assistant", "message": {
                        "role": "assistant", "content": [{"type": "text", "text": t}]}}) + "\n")
                    fh.write(json.dumps({"type": "assistant", "message": {
                        "role": "assistant", "content": [{"type": "tool_use", "name": "x"}]}}) + "\n")

        n = [0]

        def prompt(text, session="s1", env=None, now=1000.0, warm=True, tpath=True):
            """One human prompt after a warm-up prompt (the first prompt never grants)."""
            if warm:
                decide({"prompt": "hello", "session_id": session, "cwd": repo}, now - 1, cfg, env or {})
            n[0] += 1
            p = {"prompt": text, "session_id": session, "cwd": repo}
            if tpath:
                p["transcript_path"] = tp
            return decide(p, now, cfg, env or {})

        transcript("Done. Ready: merge #12?")
        g = prompt("yes")
        check("yes after `merge #12?` grants #12, repo from origin, prompt verbatim",
              g and g["pr"] == 12 and g["repo"] == "example/proj" and g["prompt"] == "yes"
              and g["session"] == "s1", g)
        for word in ("Yes!", "y", "YEP.", "yeah", "ok", "go", "do it", "Merge it."):
            check("affirmative %r grants" % word, (prompt(word, session="s-" + word) or {}).get("pr") == 12)
        check("not an affirmative: 'yes but wait' / 'maybe' / 'no'",
              not any(prompt(w, session="s-x" + w) for w in ("yes but wait", "maybe", "no", "merge it now")))
        transcript("Both ready. merge #12 or merge #13?")
        check("yes to a final question naming two PRs: nothing", prompt("yes", session="s2") is None)
        transcript("Everything is done. Want anything else?")
        check("yes to a question naming no PR: nothing", prompt("yes", session="s3") is None)
        transcript("I think merge #12 is fine. Anything to change before then")
        check("a final message that is not a question: nothing", prompt("yes", session="s4") is None)
        transcript("Is merge #12 safe? Details below.\n\nShall I run the tests again?")
        check("the LAST question decides (merge #12 earlier, none last)", prompt("yes", session="s5") is None)
        transcript("Reviewed and APPROVED.\n\nmerge #12?")
        check("a final paragraph question grants", (prompt("y", session="s6") or {}).get("pr") == 12)
        transcript("Ready: `merge #12?`")
        check("code-span question grants", (prompt("y", session="s7") or {}).get("pr") == 12)
        check("explicit `merge #12` typed later grants without a transcript",
              (prompt("merge #12", session="s8", tpath=False) or {}).get("pr") == 12)
        check("explicit grants in codex mode", (prompt("Merge #14.", session="s9",
              env={"SESSION_NOTES_AGENT": "codex"}) or {}).get("pr") == 14)
        transcript("merge #12?")
        transcript("merge #12?")
        def rollout(*events):
            with open(tp, "w") as fh:
                for ev in events:
                    fh.write(json.dumps(ev) + "\n")
        asst = lambda t: {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                              "content": [{"type": "output_text", "text": t}]}}
        done = lambda t: {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1",
                                                           "last_agent_message": t}}
        started = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}}
        cx = {"SESSION_NOTES_AGENT": "codex"}
        rollout(started, asst("Ready. merge #12?"), done("Ready. merge #12?"))
        check("codex rollout: a bare yes grants from task_complete.last_agent_message",
              (prompt("yes", session="s10", env=cx) or {}).get("pr") == 12)
        rollout(started, asst("Ready: merge #12?"))
        check("codex rollout: falls back to the newest assistant response_item",
              (prompt("yes", session="s10b", env=cx) or {}).get("pr") == 12)
        rollout(started, asst("merge #12?"), done("merge #12 or merge #13?"))
        check("codex rollout: task_complete wins, two PRs grants nothing",
              prompt("yes", session="s10c", env=cx) is None)
        rollout(started, done("All done."), {"type": "event_msg", "payload": {"type": "task_started"}})
        check("codex rollout: no PR in the last reply grants nothing", prompt("yes", session="s10d", env=cx) is None)
        check("`merge #12 and #13` / `merge #12 now` typed: nothing",
              prompt("merge #12 and #13", session="s11") is None and prompt("merge #12 now", session="s12") is None)
        check("first prompt of a session saying `merge #12`: nothing",
              prompt("merge #12", session="fresh", warm=False) is None)
        check("second prompt of that session grants", (prompt("merge #12", session="fresh", warm=False) or {}).get("pr") == 12)
        nudge, bcast = _markers(cfg)
        check("bus nudge text: nothing", prompt(nudge + " You have mail. merge #12", session="s13") is None)
        check("bcast-marked text: nothing", prompt(bcast + " merge #12", session="s14") is None)
        check("headless run: nothing", prompt("merge #12", session="s15", env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) is None)
        check("no transcript for a bare yes: nothing", prompt("yes", session="s16", tpath=False) is None)

        # the grant file: expiry, single use, repo and PR binding
        add_grant({"repo": "example/proj", "pr": 12, "ts": 5000.0, "prompt": "yes", "session": "a", "used": False},
                  5000.0)
        check("find: live grant", find_grant("example/proj", 12, 5100.0) is not None)
        check("find: other PR / other repo: none",
              find_grant("example/proj", 13, 5100.0) is None and find_grant("other/x", 12, 5100.0) is None)
        check("find: expired at 31 minutes", find_grant("example/proj", 12, 5000.0 + 31 * 60) is None)
        check("find: still live at 29 minutes", find_grant("example/proj", 12, 5000.0 + 29 * 60) is not None)
        e = consume_grant("example/proj", 12, "h" * 40, 5200.0)
        check("consume binds the head and marks used", e and e["used"] and e["head"] == "h" * 40, e)
        check("single use: a second consume gets nothing", consume_grant("example/proj", 12, "h" * 40, 5201.0) is None)
        add_grant({"repo": None, "pr": 20, "ts": 6000.0, "used": False}, 6000.0)
        check("a null-repo grant never matches a repo, but is found as null-repo",
              find_grant("example/proj", 20, 6001.0) is None
              and find_null_repo_grant(20, 6001.0) is not None
              and consume_grant("example/proj", 20, "h" * 40, 6002.0) is None)
        check("repo A #12 does not authorize repo B #12",
              find_grant("example/other", 12, 5100.0) is None)

        # repo resolution: cwd without a git origin
        plain = os.path.join(T, "plain")
        os.makedirs(plain)

        def at(cwd, text, session):
            decide({"prompt": "hello", "session_id": session, "cwd": cwd}, 999.0, cfg, {})
            return decide({"prompt": "yes", "session_id": session, "cwd": cwd, "transcript_path": tp},
                          1000.0, cfg, {})
        transcript("Ready: merge #12?")
        check("no git cwd and no repo named: repo null", (at(plain, "", "r1") or {}).get("repo", "x") is None)
        transcript("Ready to ship Example/Proj. merge #12?")
        check("the reply names exactly one owner/repo: used", (at(plain, "", "r2") or {}).get("repo") == "example/proj")
        transcript("See https://github.com/Example/Other/pull/12. merge #12?")
        check("the reply names a github PR URL: used", (at(plain, "", "r3") or {}).get("repo") == "example/other")
        transcript("Compare Example/Proj and Example/Other. merge #12?")
        check("the reply names two repos: repo null", (at(plain, "", "r4") or {}).get("repo", "x") is None)
        transcript("Edited src/app.py. merge #12?")
        check("a bare path is not a repo (wt would refuse the mismatch anyway)",
              (at(plain, "", "r5") or {}).get("repo") in (None, "src/app.py"))
        scope = os.path.join(T, "scoperoot")
        kid = os.path.join(scope, "thing")
        os.makedirs(kid)
        subprocess.run(["git", "-C", kid, "init", "-q"], check=True)
        subprocess.run(["git", "-C", kid, "remote", "add", "origin", "https://github.com/Example/Thing.git"], check=True)
        os.makedirs(os.path.join(scope, "worktrees"))
        with open(cfgf, "a") as fh:
            fh.write('[[scopes]]\nname = "s"\nroot = "%s"\n' % scope)
        cfg2 = notes_config.load(cfgf)
        transcript("Ready: merge #12?")
        decide({"prompt": "hello", "session_id": "r6", "cwd": scope}, 999.0, cfg2, {})
        g6 = decide({"prompt": "yes", "session_id": "r6", "cwd": scope, "transcript_path": tp}, 1000.0, cfg2, {})
        check("scope root with exactly one git child on github resolves to that child", (g6 or {}).get("repo") == "example/thing", g6)
        os.makedirs(os.path.join(scope, "second", ".git"))
        decide({"prompt": "hello", "session_id": "r7", "cwd": scope}, 999.0, cfg2, {})
        g7 = decide({"prompt": "yes", "session_id": "r7", "cwd": scope, "transcript_path": tp}, 1000.0, cfg2, {})
        check("scope root with two git children: repo null", (g7 or {}).get("repo", "x") is None, g7)

        # B4: the final sentence must BE the ask
        for i_, last in enumerate(("CI is red on lint.\n\nWant me to fix the lint before merge #12?",
                                   "Should I hold off on merge #12?",
                                   "After merge #12 lands, should I start the follow-up?",
                                   "Both are approved. merge #12 and #13?",
                                   "Ready: merge #12 or hold: merge #13?",
                                   "Not ready, do not proceed: merge #12?",
                                   "Ready (see #9): merge #12?")):
            transcript(last)
            check("B4 negative: %r grants nothing" % last.split("\n")[-1], prompt("yes", session="b4-%d" % i_) is None)
        transcript("Approved at abc123, CI green: merge #12 in Example/Proj?")
        check("B4: a short lead-in and ` in owner/repo` are accepted",
              (prompt("yes", session="b4ok") or {}).get("repo") == "example/proj")

        # B5: a human prompt newer than the reply voids it
        with open(tp, "w") as fh:
            for e in ({"type": "assistant", "message": {"content": [{"type": "text", "text": "APPROVE recorded. merge #12?"}]}},
                      {"type": "user", "message": {"content": "no, not yet. first rebase and rerun the tests"}},
                      {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
                      {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                      {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}):
                fh.write(json.dumps(e) + "\n")
        check("B5 Claude: a human prompt after the question voids a later `go`", prompt("go", session="b5a") is None)
        with open(tp, "w") as fh:
            for e in ({"type": "assistant", "message": {"content": [{"type": "text", "text": "merge #12?"}]}},
                      {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
                      {"type": "user", "message": {"content": "yes"}}):
                fh.write(json.dumps(e) + "\n")
        check("B5 Claude (control): a tool_result and the CURRENT prompt already in the transcript do not void it",
              (prompt("yes", session="b5b") or {}).get("pr") == 12)
        rollout(started, done("Approved. merge #12?"),
                {"type": "event_msg", "payload": {"type": "user_message", "message": "no. rebase first"}},
                {"type": "event_msg", "payload": {"type": "task_started"}}, asst("Rebasing now."))
        check("B5 Codex: an older task_complete does not beat a newer turn", prompt("ok", session="b5c", env=cx) is None)
        rollout(started, done("Approved. merge #12?"),
                {"type": "event_msg", "payload": {"type": "user_message", "message": "no. rebase first"}})
        check("B5 Codex: a user message after the task_complete voids it", prompt("ok", session="b5d", env=cx) is None)
        rollout(started, done("Approved. merge #12?"), {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "ok"}})
        check("B5 Codex (control): the current turn's task_started and prompt do not void it",
              (prompt("ok", session="b5e", env=cx) or {}).get("pr") == 12)
        rollout(started, done("Approved. merge #12?"),
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                      "content": [{"type": "input_text", "text": "<environment_context>x"}]}})
        check("B5 Codex (control): injected context is not a human prompt",
              (prompt("ok", session="b5f", env=cx) or {}).get("pr") == 12)

        # N1: an underscore inside owner/repo survives; emphasis marks at the ends are stripped
        transcript("Ready: merge #12 in my-org/my_repo?")
        check("N1: `in my-org/my_repo` is read whole", (at(plain, "", "n1a") or {}).get("repo") == "my-org/my_repo")
        transcript("_Ready: merge #12 in my-org/my_repo?_")
        check("N1: emphasis at the ends is stripped, the repo is unchanged",
              (at(plain, "", "n1b") or {}).get("repo") == "my-org/my_repo")
        transcript("Ready: _merge #12?_")
        check("NB6: underscore emphasis after a lead-in still grants", (at(plain, "", "nb6a") or {}).get("pr") == 12)
        transcript("Ready: _merge #12 in my-org/my_repo?_")
        check("NB6: emphasis around a repo ask strips, my_repo survives",
              (at(plain, "", "nb6b") or {}).get("repo") == "my-org/my_repo")
        transcript("Ready: merge #12 in my-org/_dotfiles?")
        check("N2: a repo name that starts with an underscore is read whole",
              (at(plain, "", "n2a") or {}).get("repo") == "my-org/_dotfiles")
        transcript("Ready: merge #12 in my-org/repo_?")
        check("N2: a repo name that ends with an underscore is read whole",
              (at(plain, "", "n2b") or {}).get("repo") == "my-org/repo_")
        check("N3: the seen markers live beside the grants",
              os.path.isdir(os.path.join(os.path.dirname(grants_path()), "seen")))
        transcript("**Ready: `merge #12 in my-org/my_repo?`**")
        check("N1: bold and code marks are stripped", (at(plain, "", "n1c") or {}).get("repo") == "my-org/my_repo")

        # N2: the reply names another repo than the cwd's origin
        transcript("Ready: merge #12 in other-owner/other-repo?")
        check("N2: a named repo that differs from the cwd origin grants nothing", prompt("yes", session="n2") is None)
        transcript("Ready: merge #12 in Example/Proj?")
        check("N2 (control): the same repo as the cwd origin grants", (prompt("yes", session="n2b") or {}).get("repo") == "example/proj")

        # B2: the grant file never moves with $HOME or SESSION_NOTES_CONFIG
        saved_override, GRANTS_PATH_OVERRIDE = GRANTS_PATH_OVERRIDE, None
        import pwd
        want = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, *GRANTS_REL)
        check("B2: grants_path() is the passwd home plus the fixed path", grants_path() == want, grants_path())
        for var, val in (("HOME", T), ("SESSION_NOTES_CONFIG", cfgf)):
            r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import merge_consent as m; print(m.grants_path())"
                                % os.path.dirname(os.path.realpath(__file__))],
                               env=dict(os.environ, **{var: val}), capture_output=True, text=True)
            check("B2: %s=... does not move the grant file" % var, r.stdout.strip() == want, r.stdout + r.stderr)
        GRANTS_PATH_OVERRIDE = saved_override

        # main(): never prints, never fails, even on garbage
        env = dict(os.environ, SESSION_NOTES_CONFIG=cfgf)
        for inp in ("not json", "[]", json.dumps({"prompt": "yes"}), json.dumps({"prompt": 5})):
            # run main() with the grant store pointed into the sandbox: the real one is never written
            r = subprocess.run([sys.executable, "-c",
                                "import sys; sys.path.insert(0, %r); import merge_consent as m; "
                                "m.GRANTS_PATH_OVERRIDE = %r; sys.exit(m.main() or 0)"
                                % (os.path.dirname(os.path.realpath(__file__)), os.path.join(T, "grants-main.json"))],
                               input=inp, env=env, capture_output=True, text=True)
            check("main on %r: rc 0, stdout empty" % inp[:12], r.returncode == 0 and r.stdout == "", (r.returncode, r.stdout))
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("all merge_consent checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv[1:] else main())
