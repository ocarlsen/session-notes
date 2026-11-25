#!/usr/bin/env python3
"""UserPromptSubmit hook: record the user's yes to ONE specific merge.

`wt merge` refuses unless the human said yes to that merge. The session ends its reply with a
question naming exactly one PR, `merge #N?`, and the user answers with a bare affirmative
("yes", "y", "yep", "yeah", "ok", "go", "do it", "merge it", any case, trailing punctuation
ok) or types `merge #N`. This hook turns that human prompt into a grant `wt merge` consumes.
It only RECORDS: it never blocks, alters or answers a prompt, prints nothing, and exits 0 on
any error of its own (a message to stderr).

A prompt is NOT a human answer, and grants nothing, when it is:
  * the first prompt this hook sees in a session (a handoff launch passes its task as argv);
  * a bus nudge (contains the bus_nudge marker) or a bcast line (contains the bcast prefix);
  * a headless run (CLAUDE_CODE_ENTRYPOINT starting with "sdk"). Otherwise, when unsure, nothing.

Grant rules:
  * Claude Code: the hook input carries `transcript_path`; the last assistant text in that
    JSONL is the question. Its LAST question must mention exactly one distinct `merge #N`,
    and the prompt must be an affirmative. A yes to a final question naming two PRs, or none,
    grants nothing.
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
prompt verbatim and the session id, in `<shared_state_dir>/merge-consent/grants.json`. The
file is protected from agent writes by checkout_guard. `wt.py` imports this module for the
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
_MENTION = re.compile(r"merge\s+#(\d+)", re.I)
_TAIL_PUNCT = re.compile(r"[\s.!,]+$")
_CLOSERS = "`*_\"')]>"
TRANSCRIPT_TAIL = 8 * 1024 * 1024


def _cfg(cfg=None):
    if cfg is not None:
        return cfg
    import notes_config
    return notes_config.load()


def grants_path(cfg=None):
    import notes_config
    return notes_config.shared_state_path("merge-consent", "grants.json", cfg=_cfg(cfg))


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


def add_grant(grant, now, cfg=None):
    path = grants_path(cfg)
    with _Lock(path):
        keep = [g for g in _read(path) if now - float(g.get("ts") or 0) <= KEEP
                and not (_live(g, now) and g.get("pr") == grant["pr"] and g.get("repo") == grant["repo"])]
        keep.append(grant)
        _write(path, keep)


def find_grant(repo, pr, now=None, cfg=None):
    """The newest unused, unexpired grant for (repo, pr), or None. Does not consume it."""
    now = time.time() if now is None else now
    hits = [g for g in _read(grants_path(cfg)) if _live(g, now) and _matches(g, repo, pr)]
    return max(hits, key=lambda g: g.get("ts") or 0) if hits else None


def find_null_repo_grant(pr, now=None, cfg=None):
    """A live grant for `pr` whose repo could not be resolved (wt refuses it, saying why)."""
    now = time.time() if now is None else now
    return next((g for g in _read(grants_path(cfg)) if _live(g, now) and g.get("pr") == pr
                 and not g.get("repo")), None)


def consume_grant(repo, pr, head, now=None, cfg=None):
    """Mark the newest live grant for (repo, pr) used and bind it to `head`. Returns the entry,
    or None when there is none (so a second call with the same grant gets None)."""
    now = time.time() if now is None else now
    path = grants_path(cfg)
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


def last_assistant_text(path):
    """The previous assistant reply from a Claude Code transcript (newest main-thread assistant
    text) or a Codex rollout (newest task_complete.last_agent_message, else the newest assistant
    response_item message). None when there is none."""
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
    fallback = None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind, pl = e.get("type"), e.get("payload")
        if kind == "assistant" and not e.get("isSidechain"):
            msg = e.get("message")
            text = _blocks_text(msg.get("content") if isinstance(msg, dict) else None, ("text",))
            if text.strip():
                return text
        elif kind == "event_msg" and isinstance(pl, dict) and pl.get("type") == "task_complete":
            text = pl.get("last_agent_message")
            if isinstance(text, str) and text.strip():
                return text
        elif (kind == "response_item" and isinstance(pl, dict) and pl.get("type") == "message"
              and pl.get("role") == "assistant" and fallback is None):
            text = _blocks_text(pl.get("content"), ("output_text", "text"))
            if text.strip():
                fallback = text
    return fallback


def final_question(text):
    """The last sentence of the message when the message ends in a question, else None."""
    t = (text or "").strip().rstrip(_CLOSERS + " \t\r\n")
    if not t.endswith("?"):
        return None
    para = re.split(r"\n\s*\n", t)[-1].strip()
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n", para) if s.strip()]
    return sentences[-1] if sentences else None


def asked_pr(text):
    """N when the message's last question names exactly one `merge #N`, else None."""
    q = final_question(text)
    nums = {int(n) for n in _MENTION.findall(q or "")}
    return nums.pop() if len(nums) == 1 else None


_PR_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/\d+", re.I)
_OWNER_REPO = re.compile(r"(?<![\w/.:-])([\w-]+)/([\w][\w.-]*)(?![\w/-])")


def repo_from_reply(text):
    """owner/repo when the reply's final paragraph (the one holding the question) names exactly one (a github PR URL, or a bare
    owner/repo), else None."""
    q = re.split(r"\n\s*\n", (text or "").strip())[-1] if final_question(text) else ""
    urls = {("%s/%s" % m).lower() for m in _PR_URL.findall(q)}
    if len(urls) == 1:
        return urls.pop()
    bare = {("%s/%s" % m).lower().rstrip(".") for m in _OWNER_REPO.findall(_PR_URL.sub(" ", q))}
    return bare.pop() if len(bare) == 1 and not urls else None


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
    return repo_identity(cwd) or repo_from_reply(reply) or repo_from_scope(cwd, cfg)


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
    d = os.path.join(cfg["paths"]["shared_state_dir"], "merge-consent", "seen")
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
    reply = last_assistant_text(payload["transcript_path"]) if payload.get("transcript_path") else None
    if kind == "affirm":
        n = asked_pr(reply) if reply else None
    if not n:
        return None
    return {"repo": resolve_repo(payload.get("cwd") or os.getcwd(), reply, cfg), "pr": n, "ts": now,
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
            add_grant(g, now, cfg)
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
    with tempfile.TemporaryDirectory() as t0:
        T = os.path.realpath(t0)
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
                  5000.0, cfg)
        check("find: live grant", find_grant("example/proj", 12, 5100.0, cfg) is not None)
        check("find: other PR / other repo: none",
              find_grant("example/proj", 13, 5100.0, cfg) is None and find_grant("other/x", 12, 5100.0, cfg) is None)
        check("find: expired at 31 minutes", find_grant("example/proj", 12, 5000.0 + 31 * 60, cfg) is None)
        check("find: still live at 29 minutes", find_grant("example/proj", 12, 5000.0 + 29 * 60, cfg) is not None)
        e = consume_grant("example/proj", 12, "h" * 40, 5200.0, cfg)
        check("consume binds the head and marks used", e and e["used"] and e["head"] == "h" * 40, e)
        check("single use: a second consume gets nothing", consume_grant("example/proj", 12, "h" * 40, 5201.0, cfg) is None)
        add_grant({"repo": None, "pr": 20, "ts": 6000.0, "used": False}, 6000.0, cfg)
        check("a null-repo grant never matches a repo, but is found as null-repo",
              find_grant("example/proj", 20, 6001.0, cfg) is None
              and find_null_repo_grant(20, 6001.0, cfg) is not None
              and consume_grant("example/proj", 20, "h" * 40, 6002.0, cfg) is None)
        check("repo A #12 does not authorize repo B #12",
              find_grant("example/other", 12, 5100.0, cfg) is None)

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
        transcript("Ready: merge #12 in https://github.com/Example/Other/pull/12 ?")
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

        # main(): never prints, never fails, even on garbage
        env = dict(os.environ, SESSION_NOTES_CONFIG=cfgf)
        for inp in ("not json", "[]", json.dumps({"prompt": "yes"}), json.dumps({"prompt": 5})):
            r = subprocess.run([sys.executable, os.path.realpath(__file__)], input=inp, env=env,
                               capture_output=True, text=True)
            check("main on %r: rc 0, stdout empty" % inp[:12], r.returncode == 0 and r.stdout == "", (r.returncode, r.stdout))
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("all merge_consent checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv[1:] else main())
