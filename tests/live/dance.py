#!/usr/bin/env python3
"""The dance: a scripted story film on the live selftest's watch harness.

A Claude Code session (left pane) and a Codex session (right pane) work together in the selftest
sandbox, one scene per step: boot, mail over the bus, a poem one line each, the checkout guard,
a build / review / merge across the two CLIs, shared live notes, separate memories, and a replay
of the bus log. It sits next to the proof film:

    python3 tests/live/selftest_live.py --watch --scenario dance [--pace X] [--step] [--keep] ...
    make dance-watch ARGS="--pace 2"

Everything runs through `Live` (selftest_live.py): the same sandbox, window, status line, screen
guard and teardown. This file only adds the scenes. Never run it where the OpenAI key is not the
capped selftest key: Codex is launched live.
"""
import glob
import json
import os
import re
import shlex
import shutil
import sys
import time

import selftest_live as sl

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if os.path.join(REPO, "bin") not in sys.path:
    sys.path.append(os.path.join(REPO, "bin"))
import notes_config  # noqa: E402  (the module the tools resolve their state paths with)


def load_hook():
    """The consent hook module, loaded from this checkout (its own last_assistant_text and asked_pr
    decide what counts as the ask)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("consent_hook_mod", os.path.join(REPO, "hooks", "merge_consent.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

PASS, FAIL = sl.PASS, sl.FAIL

# Per-scene step tables (feature id, table title, scope). The ids are scene names; `realdirs`
# is the harness's own closing isolation check and keeps its title and chapter.
FEATURES = [
    ("setup", "scene 1: both sessions boot (rules, clock)", "cli"),
    ("mail", "scene 2: mail between the two models", "cli"),
    ("poem", "scene 3: a poem over the bus", "cli"),
    ("guard", "scene 4: the checkout guard refuses main", "cli"),
    ("review", "scene 5: build, review, merge across CLIs", "cli"),
    ("news", "scene 6: the merge reaches the other side", "cli"),
    ("memory", "scene 7: separate memories, both clean", "cli"),
    ("replay", "scene 8: the bus log, replayed", "cli"),
    ("realdirs", "real dirs untouched", "both"),
]
CHAPTERS = {
    "setup": "Two agents, one setup",
    "mail": "Mail between models",
    "poem": "A poem, one line each",
    "guard": "The guard says no",
    "review": "One builds, the other reviews",
    "news": "Everyone hears about it",
    "memory": "Separate memories",
    "replay": "The whole story, replayed",
    "realdirs": sl.CHAPTERS["realdirs"],
}
PANE_ROWS = ()

# The launchers the sandbox swaps in so the REAL hook and the REAL `wt` keep their grants in the
# sandbox. merge_consent.grants_path() is the passwd home of the running user, never $HOME, so
# without this a sandbox session's "yes" (and its seen/ marker) would be written under the real
# home, at the hook's GRANTS_REL. Both use the
# module-constant override the selftests use (sl.WT_TEST_LAUNCH); no variable can move it.
HOOK_LAUNCH = """#!/usr/bin/env python3
import importlib.util, sys
spec = importlib.util.spec_from_file_location("merge_consent", %(hook)r)
m = importlib.util.module_from_spec(spec)
sys.modules["merge_consent"] = m
spec.loader.exec_module(m)
m.GRANTS_PATH_OVERRIDE = %(grants)r
sys.exit(m.main())
"""
WT_LAUNCH = """#!/usr/bin/env python3
import importlib.util, sys
p, g = %(wt)r, %(grants)r
spec = importlib.util.spec_from_file_location('wt_under_test', p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.merge_consent.GRANTS_PATH_OVERRIDE = g
sys.exit(m.main(sys.argv[1:]))
"""
MARKER_MD = ("---\nname: Marker\ndescription: the selftest marker word\nmetadata:\n  type: reference\n---\n\n"
             "The selftest marker word is in the index line.\n")


def fix_prompt(path, finding, tok):
    """The fix beat's prompt: Claude fixes, commits AND pushes (`wt merge` refuses an unpushed head)."""
    return ("The reviewer asked for changes on the poem PR. Fix exactly what it found, in poem.md inside %s "
            "(Edit or Write tool on that path), then run: cd %s && git add poem.md && git commit -q -m "
            "'Address review' && git push -q origin poem . Do not run wt merge. Then reply DONE. The finding: %s (run id FIX%s)"
            % (path, shlex.quote(path), " ".join(finding.split())[:1200], tok))


def remote_head(git, path, branch):
    """What the remote (not the local tracking ref) holds for `branch`, or None."""
    out = git(path, "ls-remote", "origin", "refs/heads/" + branch).stdout.split()
    return out[0] if out else None


def write_launcher(path, template, **kw):
    with open(path, "w") as fh:
        fh.write(template % kw)
    os.chmod(path, 0o755)


CODEX_FORM = re.compile(r"\S+ hook merge_consent\b")


def patch_consent_hook(settings_path, launcher):
    """Point every merge_consent hook command in a Claude settings.json (`python3 .../merge_consent.py`)
    or a Codex hooks.json (`<launcher> hook merge_consent`) at `launcher`. -> how many."""
    with open(settings_path) as fh:
        doc = json.load(fh)
    n = [0]

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "command" and isinstance(v, str) and "hooks/merge_consent.py" in v:
                    o[k] = re.sub(r"\S*hooks/merge_consent\.py", launcher, v)
                    n[0] += 1
                elif k == "command" and isinstance(v, str) and CODEX_FORM.search(v):
                    o[k] = CODEX_FORM.sub("python3 " + launcher, v)
                    n[0] += 1
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)
    if n[0]:
        with open(settings_path, "w") as fh:
            json.dump(doc, fh, indent=2)
    return n[0]


def timeline_lines(msgs, width=86):
    """One line per bus message: time, route, text. Pure, so the replay is checkable offline."""
    out = []
    for m in sorted(msgs, key=lambda x: x.get("ts", 0)):
        stamp = time.strftime("%H:%M:%S", time.localtime(m.get("ts", 0)))
        body = " ".join(str(m.get("body", "")).split())
        head = "%s %s -> %s: " % (stamp, m.get("from", "?"), m.get("to", "?"))
        room = max(width - len(head), 12)
        out.append(head + (body if len(body) <= room else body[:room - 3] + "..."))
    return out


class Dance(sl.Live):
    FEATURES = FEATURES
    CHAPTERS = CHAPTERS
    PANE_ROWS = PANE_ROWS
    CLAUDE_PANE, CODEX_PANE = "cA", "xA"

    def __init__(self, a):
        super().__init__(a)
        if "SELFTEST_CLAUDE_MODEL" not in os.environ:
            self.claude_model = "sonnet"  # the plan: Sonnet for Claude, gpt-6.2-sol for Codex
        self.proj = os.path.join(self.root, "proj")
        self.wts = os.path.join(self.root, "wts")
        self.consent_dir = os.path.join(self.root, "consent")
        self.grants = os.path.join(self.consent_dir, "grants.json")
        self.poem = []

    # -- plan -------------------------------------------------------------------------------
    def build_plan(self):
        """The 8 scenes in order, then the isolation check."""
        return [(fid, "both") for fid, _t, _s in self.FEATURES]

    # -- helpers ----------------------------------------------------------------------------
    def sb(self):
        """The session_bus.py command prefix as the environment's allow rule spells it."""
        return "python3 %s" % shlex.quote(os.path.join(os.path.realpath(self.repo), "bin", "session_bus.py"))

    def state_paths(self):
        """(bus.jsonl, last-check.json) where the tools put them under THIS run's environment, asked of
        notes_config as session_bus.py and nightly_check.py ask it (they sit in a dot directory: no glob)."""
        env = self.env()
        old = os.environ.get("HOME")
        os.environ["HOME"] = env["HOME"]  # notes_config expands `~` from the process HOME, as the tools' own HOME does
        try:
            paths = notes_config.load(env=env)["paths"]
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old
        return os.path.join(paths["shared_state_dir"], "session-bus", "bus.jsonl"), paths["last_check"]

    def bus_msgs(self):
        out = []
        try:
            with open(self.state_paths()[0], encoding="utf-8") as fh:
                out = [json.loads(x) for x in fh.read().splitlines() if x.strip()]
        except (OSError, ValueError):
            pass
        return sorted(out, key=lambda m: m.get("ts", 0))

    def read_last_check(self):
        try:
            with open(self.state_paths()[1]) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def asked_merge(self, rid, n=7):
        """True when the newest assistant reply of the Claude session whose prompt carried `rid` ends on
        the ask `merge #n?`, read by the consent hook's own last_assistant_text and asked_pr."""
        hook = load_hook()
        d = self.scoped("claude", rid)
        try:
            for f in glob.glob(os.path.join(d, "*.jsonl")):
                text = hook.last_assistant_text(f)
                if text and hook.asked_pr(text) == n:
                    return True
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return False

    @staticmethod
    def store_has(store, tok):
        for f in glob.glob(os.path.join(store, "**", "*"), recursive=True):
            try:
                if os.path.isfile(f) and tok in open(f, encoding="utf-8", errors="replace").read():
                    return True
            except OSError:
                pass
        return False

    def bus_find(self, frm, to, needle):
        for m in self.bus_msgs():
            if m.get("from") == frm and m.get("to") == to and needle in str(m.get("body", "")):
                return m
        return None

    def user_types(self, name, text):
        """The viewer-facing keystroke of a human. The status line says so BEFORE the keys go in,
        so the film never presents it as a model decision."""
        self.write_status(note="user types: %s" % text)
        self.hold(sl.TITLE_HOLD)
        self.send(name, text)
        self.write_status(note="")

    def wt_path(self, branch="poem"):
        r = self.run(["git", "-C", self.proj, "worktree", "list", "--porcelain"], env=sl.git_env(self.home))
        path = None
        for ln in r.stdout.splitlines():
            if ln.startswith("worktree "):
                path = ln[len("worktree "):]
            elif ln.startswith("branch ") and ln.endswith("/" + branch):
                return path
        return None

    def git(self, cwd, *args):
        return sl.sh(["git"] + list(args), cwd, sl.git_env(self.home), check=False)

    # -- preparation (after install, before any scene) ---------------------------------------------
    def prepare(self):
        for g in self.store_dirs():  # the seeded indexes name marker.md: without the file the canary flags it
            with open(os.path.join(g, "marker.md"), "w") as fh:
                fh.write(MARKER_MD)
        # A real setup imports its store's index from ~/.claude/CLAUDE.md; install.sh wires only the rules, and
        # the custom global_store gets no native pass, so nightly_check would call it an orphan.
        claude_md = os.path.join(self.claude_dir, "CLAUDE.md")
        if not os.path.realpath(claude_md).startswith(self.root + os.sep):
            raise sl.Abort("refusing to wire the store outside the sandbox: %s" % claude_md)
        with open(claude_md, "a") as fh:
            fh.write("\n@%s\n" % os.path.join(self.store_dirs()[0], "MEMORY.md"))
        os.makedirs(self.wts, exist_ok=True)
        os.makedirs(self.consent_dir, exist_ok=True)
        with open(self.cfg, "a") as fh:
            fh.write('\n[wt]\ntoken_command = "echo faketoken"\nroot = "%s"\n' % self.wts)
        env = sl.git_env(self.home)
        # origin's path ends in o/r.git, so the consent hook reads the repo as o/r, as the stub's PR url says
        bare = os.path.join(self.root, "gh", "o", "r.git")
        seed = os.path.join(self.root, "proj-seed")
        os.makedirs(os.path.dirname(bare), exist_ok=True)
        sl.sh(["git", "init", "-q", "--bare", "-b", "main", bare], self.root, env)
        sl.sh(["git", "clone", "-q", bare, seed], self.root, env)
        for name, text in (("README.md", "A scratch project.\n"), ("poem.md", "# Poem\n")):
            with open(os.path.join(seed, name), "w") as fh:
                fh.write(text)
        sl.sh(["git", "add", "."], seed, env)
        sl.sh(["git", "commit", "-q", "-m", "seed", "--no-verify"], seed, env)
        sl.sh(["git", "push", "-q", "origin", "HEAD:main"], seed, env)
        sl.sh(["git", "clone", "-q", bare, self.proj], self.root, env)
        self.trust_path(self.proj)
        # the real hook and the real wt, with their grants kept in the sandbox
        hook = os.path.join(self.root, "consent_hook.py")
        write_launcher(hook, HOOK_LAUNCH, hook=os.path.join(self.repo, "hooks", "merge_consent.py"), grants=self.grants)
        if not patch_consent_hook(os.path.join(self.claude_dir, "settings.json"), hook):
            raise sl.Abort("the sandbox Claude settings carry no merge_consent hook to point at the sandbox grants")
        if not patch_consent_hook(os.path.join(self.codex_dir, "hooks.json"), hook):
            raise sl.Abort("the sandbox Codex hooks.json carries no merge_consent hook to point at the sandbox grants")
        wt = os.path.join(self.bin, "wt")
        if os.path.lexists(wt):
            os.remove(wt)
        write_launcher(wt, WT_LAUNCH, wt=os.path.join(self.repo, "bin", "wt.py"), grants=self.grants)
        # the rendered environment: the Codex pane's `session_bus.py send` passes on its allow rule
        bad = self.env_apply()
        if bad:
            raise sl.Abort(bad)

    def claude_pane_argv(self, prompt, name):
        argv = self.claude_argv(prompt, name=name)
        i = argv.index("--allowedTools")
        argv[i:i] = ["--add-dir", self.wts]  # the worktree `wt new` makes is outside the pane's cwd
        return argv

    def codex_pane_argv(self, prompt):
        """No -a/-s flags: the rendered config (workspace-write, on-request, auto review) governs."""
        return [self.codex_bin, "--no-daemon", "--dangerously-bypass-hook-trust", "-m", self.codex_model,
                "-c", "model_reasoning_effort=low", prompt]

    def launch_panes(self):
        self.launch(self.CLAUDE_PANE, self.claude_pane_argv("Reply with only the word READY", self.CLAUDE_PANE), cwd=self.proj)
        self.launch(self.CODEX_PANE, self.codex_pane_argv("Reply with only the word READY"), cwd=self.proj)
        self.default_focus()
        for n in (self.CLAUDE_PANE, self.CODEX_PANE):
            self.wait_idle(n, 150)

    # -- orchestration --------------------------------------------------------------------------
    def run_steps(self):
        if not (self.have["claude"] and self.have["codex"]):
            raise sl.Abort("the dance needs both CLIs (%s)" % "; ".join(
                "%s: %s" % (c, self.why.get(c, "login failed" if self.login_failed else "unavailable"))
                for c in ("claude", "codex") if not self.have[c]))
        self.prepare()
        for fid, fn in (("setup", self.scene_setup), ("mail", self.scene_mail), ("poem", self.scene_poem),
                        ("guard", self.scene_guard), ("review", self.scene_review), ("news", self.scene_news),
                        ("memory", self.scene_memory), ("replay", self.scene_replay)):
            self.do(fid, "both", fn)

    # -- scene 1 --------------------------------------------------------------------------------
    def scene_setup(self):
        """Both CLIs boot in the fresh sandbox: row_boot's checks (rules block for Claude, the
        SessionStart marker for Codex, and a clock line within 5 minutes in both)."""
        bad = []
        for cli in ("claude", "codex"):
            st, detail, ev = self.row_boot(cli)
            if st != PASS:
                bad.append("%s: %s" % (cli, detail))
                evidence = ev
        if bad:
            return FAIL, "; ".join(bad), evidence
        self.launch_panes()
        return PASS, "both fresh sessions loaded their start-of-session context with a clock line; the story panes are up", None

    # -- scene 2 --------------------------------------------------------------------------------
    def scene_mail(self):
        c, x, t = self.CLAUDE_PANE, self.CODEX_PANE, self.tok
        note, reply = "Hello from Claude. Codeword MAIL%s." % t, "Codex here, got it. Codeword REPLY%s." % t
        self.wait_idle(c)
        self.send(c, "Run exactly this shell command, then reply DONE: %s send --to %s %s" % (
            self.sb(), x, shlex.quote(note)))
        if not self.wait_for(lambda: self.bus_find(c, x, "MAIL" + t), 150):
            return FAIL, "Claude's note never reached the bus (no %s -> %s message with MAIL%s)" % (c, x, t), None
        self.wait_idle(x)
        self.send(x, "New mail has arrived. Read it, then reply to the session that wrote it by running exactly this "
                     "shell command, then reply DONE: %s send --to %s %s" % (self.sb(), c, shlex.quote(reply)))
        if not self.wait_for(lambda: self.hook_injected("codex", "MAIL" + t), 120):
            return FAIL, "the bus hook never put MAIL%s into Codex's next prompt" % t, None
        if not self.wait_for(lambda: self.bus_find(x, c, "REPLY" + t), 180):
            return FAIL, "Codex never sent its reply over the bus (no %s -> %s message with REPLY%s)" % (x, c, t), None
        self.wait_idle(c)
        self.send(c, "What is 2+2? Answer in one short line.")  # neutral: the reply can only arrive through the hook
        if not self.wait_for(lambda: self.hook_injected("claude", "REPLY" + t), 120):
            return FAIL, "the bus hook never put REPLY%s into Claude's next prompt" % t, None
        return PASS, "Claude's note reached Codex through the hook, and Codex's reply reached Claude's next prompt", None

    # -- scene 3 --------------------------------------------------------------------------------
    def scene_poem(self):
        c, x, t = self.CLAUDE_PANE, self.CODEX_PANE, self.tok
        tags = []
        for r in (1, 2):
            ct, xt = "P%dC%s" % (r, t), "P%dX%s" % (r, t)
            tags += [(ct, c, x), (xt, x, c)]
            lead = ("Start a short poem about the sea: write its first line." if r == 1 else
                    "Mail from Codex has arrived with the poem so far. Write the next line.")
            self.wait_idle(c)
            self.send(c, "%s Then send ONLY your line, after the tag %s and a space, to %s by running this shell command "
                         "with your line in place of LINE (no quotes or apostrophes inside it): %s send --to %s '%s LINE'. "
                         "Then reply DONE." % (lead, ct, x, self.sb(), x, ct))
            if not self.wait_for(lambda: self.bus_find(c, x, ct), 180):
                return FAIL, "Claude's line %s never reached the bus" % ct, None
            self.wait_idle(x)
            self.send(x, "Mail from the other session has arrived with the poem so far. Write the next line, then send ONLY "
                         "your line, after the tag %s and a space, to %s by running this shell command with your line in "
                         "place of LINE (no quotes or apostrophes inside it): %s send --to %s '%s LINE'. Then reply DONE."
                         % (xt, c, self.sb(), c, xt))
            if not self.wait_for(lambda: self.bus_find(x, c, xt), 180):
                return FAIL, "Codex's line %s never reached the bus" % xt, None
        self.wait_idle(c)
        self.send(c, "Mail has arrived: the last line of the poem. Read it, then reply DONE.")  # delivers P2X
        lines = []
        for tag, frm, to in tags:
            m = self.bus_find(frm, to, tag)
            receiver = "claude" if to == c else "codex"
            if not self.wait_for(lambda: self.hook_injected(receiver, tag), 120):
                return FAIL, "the line %s never reached %s's transcript through the hook" % (tag, receiver), None
            lines.append(str(m["body"]).split(" ", 1)[1].strip() if " " in str(m["body"]) else "")
        if not all(lines):
            return FAIL, "a poem line came through empty: %r" % lines, None
        self.poem = lines
        self.log("poem.txt", "\n".join(lines) + "\n")
        return PASS, "four lines, one each way twice, each in both transcripts (sent by one, injected into the other)", "poem.txt"

    # -- scene 4 --------------------------------------------------------------------------------
    def scene_guard(self):
        c, t = self.CLAUDE_PANE, self.tok
        if not self.poem:
            return FAIL, "no poem to add: the poem scene did not produce its four lines", None
        rid = "GUARD" + t
        refusal = re.compile(r"checkout_guard: \S*poem\.md is tracked on")
        main_poem = os.path.join(self.proj, "poem.md")
        self.wait_idle(c)
        self.send(c, "Add this poem to %s, one line each, below the title: %s. Put a blank line between consecutive "
                     "lines so each renders as its own line. Use the Edit tool directly on that exact "
                     "path. If the edit is refused, follow what the refusal tells you: run `wt new poem` and write the "
                     "poem into poem.md in the worktree it creates (Edit or Write tool on that path), without committing. "
                     "Then reply DONE. (run id %s)" % (main_poem, " | ".join(self.poem), rid))
        done = lambda: refusal.search(self.pane_reply("claude", rid, assistant_only=False)) and self.wt_path()
        if not self.wait_for(done, 300):
            return FAIL, "no guard refusal plus a `poem` worktree within 300 s", None
        self.wait_idle(c, 120)
        path = self.wt_path()
        with open(main_poem) as fh:
            if fh.read() != "# Poem\n":
                return FAIL, "poem.md on main was changed", None
        dirty = self.git(self.proj, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        if dirty:
            return FAIL, "the main checkout has tracked changes: %s" % dirty[:120], None
        try:
            with open(os.path.join(path, "poem.md")) as fh:
                got = " ".join(fh.read().split())
        except OSError:
            return FAIL, "no poem.md in the worktree %s" % path, None
        missing = [ln for ln in self.poem if " ".join(ln.split()) not in got]
        if missing:
            return FAIL, "the worktree's poem.md lacks %d of the 4 lines (first: %r)" % (len(missing), missing[0][:60]), None
        return PASS, "the edit on main was refused with the guard's own text, main untouched; the poem is in the `poem` worktree", None

    def stub_pr(self, head, diff, reset=False):
        """Point the stub gh at the PR as it stands now (head and diff); reset also forgets posted comments."""
        view = {"number": 7, "headRefOid": head, "state": "OPEN", "url": "https://example.invalid/o/r/pull/7",
                "title": "Add the poem", "body": "Adds a four line poem to poem.md, one verse line per line. A small, correct change."}
        with open(os.path.join(self.ghdir, "scenario.json"), "w") as fh:
            json.dump({"view": view, "login": "tester", "comments": [], "diff": diff}, fh)
        if reset:
            try:
                os.remove(os.path.join(self.ghdir, "posted.jsonl"))
            except OSError:
                pass

    def review_round(self, path, head, rules, rnd):
        """One Codex `$review 7`. Returns (error or None, verdict, comment body); the verdict must be the newest
        posted comment, in the marker form, naming `head`."""
        before = len(self.posted_comments())
        log = "dance-review.txt" if rnd == 1 else "dance-review-2.txt"
        r = self.turn("codex", "$review 7 " + rules, log, 720, "danceRev%d" % rnd, cwd=path,
                      until=lambda: len(self.posted_comments()) > before)
        posted = self.posted_comments()
        self.log(log, "\nposted comments: %s\n" % json.dumps(posted))
        if len(posted) <= before:
            return "the Codex review ended (rc=%s) without a verdict comment" % r.returncode, None, ""
        body = posted[-1].get("body") or ""
        first = body.splitlines()[0] if body else ""
        m = re.fullmatch(r"Review verdict: (APPROVE|CHANGES) ([0-9a-f]{40})", first)
        if not m:
            return "the recorded comment does not start with the verdict marker: %r" % first[:80], None, body
        if m.group(2) != head:
            return "the verdict names sha %s, not the PR head %s" % (m.group(2)[:10], head[:10]), None, body
        return None, m.group(1), body

    # -- scene 5 --------------------------------------------------------------------------------
    def scene_review(self):
        c, t = self.CLAUDE_PANE, self.tok
        path = self.wt_path()
        if not path:
            return FAIL, "no `poem` worktree from the last scene", None
        self.trust_path(path)
        # a) Claude commits and opens the PR (the stub gh answers)
        seen_calls = len(self.gh_calls())
        self.wait_idle(c)
        self.send(c, "Run exactly this one shell command, then reply DONE: cd %s && git add poem.md && git commit -q -m "
                     "'Add the poem' && wt pr --title 'Add the poem'" % shlex.quote(path))
        if not self.wait_for(lambda: any(x[:2] == ["pr", "create"] for x in self.gh_calls()[seen_calls:]), 180):
            return FAIL, "`wt pr` never reached the stub gh (no `pr create` call)", None
        self.wait_idle(c, 90)
        head = self.git(path, "rev-parse", "HEAD").stdout.strip()
        if self.git(path, "log", "--oneline", "origin/main..HEAD").stdout.strip() == "":
            return FAIL, "the worktree has no commit ahead of origin/main", None
        diff = self.git(path, "diff", "origin/main...HEAD").stdout
        self.stub_pr(head, diff, reset=True)
        # b) wt merge refuses: nobody has approved yet
        rid1 = "MERGEA" + t
        self.send(c, "Run exactly this shell command once and tell me what it printed; do not retry it and do not fix "
                     "anything: wt merge poem  (run id %s)" % rid1)
        if not self.wait_for(lambda: "verdict gate refused PR #7" in self.pane_reply("claude", rid1, assistant_only=False), 120):
            return FAIL, "`wt merge` before any review did not print the verdict gate refusal", None
        self.wait_idle(c, 90)
        # c) Codex reviews, on gpt-6.2-sol at low effort (row_review's prompt)
        rules = ("This is a throwaway test repo with no checks: skip Step 1. In Step 3 spawn the reviewer with model "
                 "gpt-6.2-sol at low effort instead of the model the skill names (a cost capped test). The change is a "
                 "small correct addition, so the reviewer's verdict is APPROVE unless it finds a real defect. In Step 4 "
                 "post exactly what the reviewer returned, then stop.")
        fixed = False
        for rnd in (1, 2):
            err, verdict, body = self.review_round(path, head, rules, rnd)
            if err:
                return FAIL, err, "dance-review.txt" if rnd == 1 else "dance-review-2.txt"
            if verdict == "APPROVE":
                break
            if rnd == 2:
                return FAIL, "the reviewer returned CHANGES twice (once more after one fix round): %s" % " ".join(body.split())[:140], "dance-review-2.txt"
            # one reasonable CHANGES: exactly one fix beat by Claude, then one re-review at the new head
            old_head = head
            self.wait_idle(c)
            self.send(c, fix_prompt(path, body, t))
            if not self.wait_for(lambda: self.git(path, "rev-parse", "HEAD").stdout.strip() != old_head, 240):
                return FAIL, "after the first CHANGES, Claude made no fix commit in the `poem` worktree within 240 s", "dance-review.txt"
            new_head = self.git(path, "rev-parse", "HEAD").stdout.strip()
            if not self.wait_for(lambda: remote_head(self.git, path, "poem") == new_head, 120):
                return FAIL, ("the fix commit %s was never pushed: origin/poem is %s (`wt merge` would stop at 'push first')"
                              % (new_head[:10], (remote_head(self.git, path, "poem") or "absent")[:10])), "dance-review.txt"
            self.wait_idle(c, 90)
            head = self.git(path, "rev-parse", "HEAD").stdout.strip()
            diff = self.git(path, "diff", "origin/main...HEAD").stdout
            self.stub_pr(head, diff)
            fixed = True
        if any(x[:2] == ["pr", "merge"] for x in self.gh_calls()):
            return FAIL, "a merge happened before the user's yes", "dance-review.txt"
        # d) the merge question: Claude asks, the user types yes, the real hook records it
        rid2 = "MERGEB" + t
        self.wait_idle(c)
        self.send(c, "Run exactly this shell command: wt merge poem  If it is refused, do what its message says. When the "
                     "user later answers yes, run `wt merge poem` once more. End the reply in which you ask for the yes "
                     "with exactly this last line: merge #7?  (run id %s)" % rid2)
        if not self.wait_for(lambda: self.asked_merge(rid2), 240):
            return FAIL, "Claude never ended a reply on `merge #7?`", None
        self.wait_idle(c, 90)
        if any(x[:2] == ["pr", "merge"] for x in self.gh_calls()):
            return FAIL, "a merge happened before the user's yes", None
        self.user_types(c, "yes")
        merged = lambda: any(x[:2] == ["pr", "merge"] for x in self.gh_calls())
        if not self.wait_for(merged, 240):
            return FAIL, "no `gh pr merge` after the yes: the hook's grant was not found, or Claude did not run `wt merge` again", None
        self.wait_idle(c, 90)
        try:
            with open(self.grants) as fh:
                gr = json.load(fh).get("grants", [])
        except (OSError, ValueError):
            gr = []
        if not any(g.get("pr") == 7 and g.get("repo") == "o/r" and g.get("prompt") == "yes" for g in gr):
            return FAIL, "the sandbox grants file has no grant for PR 7 with the prompt `yes` (%s)" % json.dumps(gr)[:120], None
        if not any("Merge authorized by the user" in str(p.get("body")) for p in self.posted_comments()):
            return FAIL, "wt merge posted no authorization comment", None
        return PASS, ("wt merge refused before a review; %sCodex's APPROVE sits at the final PR head; the typed yes was "
                      "recorded by the real hook and the merge went through the stub gh"
                      % ("Codex asked for one change, Claude fixed it, and the re-review passed; " if fixed else "")), "dance-review.txt"

    # -- scene 6 --------------------------------------------------------------------------------
    def scene_news(self):
        x, t = self.CODEX_PANE, self.tok
        try:
            with open(self.live_file(), encoding="utf-8") as fh:
                live = fh.read()
        except OSError:
            live = ""
        if "merged PR #7" not in live:
            return FAIL, "the merge wrote no `merged PR #7` line to the live-context file", None
        rid = "NEWS" + t
        self.wait_idle(x)
        self.send(x, "$refresh then reply with the newest line of the live context it shows, word for word. (run id %s)" % rid)
        if not self.wait_for(lambda: "merged PR #7" in self.pane_reply("codex", rid, assistant_only=False), 240):
            return FAIL, "Codex's $refresh never surfaced the merge line", None
        return PASS, "the merge wrote a live-context line and Codex's $refresh showed it", None

    # -- scene 7 --------------------------------------------------------------------------------
    def scene_memory(self):
        bad = []
        for cli in ("claude", "codex"):
            st, detail, ev = self.row_reflect(cli)
            if st != PASS:
                bad.append("%s: %s" % (cli, detail))
                evidence = ev
        if bad:
            return FAIL, "; ".join(bad), evidence
        ctok, xtok = ["REFLECT%s%s" % (self.tok, c[:2].upper()) for c in ("claude", "codex")]
        gc, gx = self.store_dirs()
        for tok, store, what in ((ctok, gx, "Claude's fact is in the Codex store"), (xtok, gc, "Codex's fact is in the Claude store")):
            if self.store_has(store, tok):
                return FAIL, "the memories are not separate: " + what, None
        out = os.path.join(self.logs, "nightly.txt")
        self.show_command("claude", "nightly", "python3 %s > %s 2>&1; echo \"nightly_check exit code: $?\"" % (
            shlex.quote(os.path.join(self.repo, "bin", "nightly_check.py")), shlex.quote(out)))
        doc = self.read_last_check()
        if doc is None:
            return FAIL, "nightly_check wrote no readable last-check.json", "nightly.txt"
        checks = doc.get("checks", [])
        agents = {c.get("agent") for c in checks}
        notclean = ["%s/%s %s" % (c.get("agent"), c.get("check"), c.get("status")) for c in checks if c.get("status") != "clean"]
        if doc.get("rc") != 0 or notclean or not {"claude", "codex"} <= agents:
            return FAIL, "nightly_check rc=%s, agents %s, not clean: %s" % (doc.get("rc"), sorted(a for a in agents if a), notclean), "nightly.txt"
        return PASS, "each CLI saved its own fact; nightly_check ran %d checks over both agents, all clean" % len(checks), None

    # -- scene 8 --------------------------------------------------------------------------------
    def scene_replay(self):
        lines = timeline_lines(self.bus_msgs())
        tl = os.path.join(self.logs, "timeline.txt")
        with open(tl, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        if len(lines) < 6:
            return FAIL, "the bus log holds %d messages, not the 6 or more the story sent" % len(lines), "timeline.txt"
        self.show_command("claude", "replay", "cat %s; sleep 6" % shlex.quote(tl), timeout=90)
        return PASS, "the bus log replayed as a %d line timeline" % len(lines), "timeline.txt"


# ---------------------------------------------------------------------------- offline checks

def offline_checks(check):
    """Run by selftest_live's selftest(). `check(name, cond, detail)` records one result."""
    import argparse
    import subprocess
    import tempfile
    here = os.path.dirname(os.path.realpath(__file__))
    main_py = os.path.join(here, "selftest_live.py")
    ids = [f for f, _t, _s in FEATURES]
    check("dance: every step has a chapter title and no other", set(CHAPTERS) == set(ids), str(set(CHAPTERS) ^ set(ids)))
    check("dance: chapter titles have no dash, at most 8 words, no jargon",
          all(not sl.title_problems(v) for v in CHAPTERS.values()),
          str({k: sl.title_problems(v) for k, v in CHAPTERS.items() if sl.title_problems(v)}))
    check("dance: the 8 scene titles are the planned ones", [CHAPTERS[f] for f in ids[:8]] == [
        "Two agents, one setup", "Mail between models", "A poem, one line each", "The guard says no",
        "One builds, the other reviews", "Everyone hears about it", "Separate memories", "The whole story, replayed"])
    check("dance: negative control, a dashed title is caught", bool(sl.title_problems("Two agents - one setup")))
    ns = argparse.Namespace(only=None, feature=None, keep=False, selftest=False, watch=True, pace=0.0, step=False,
                            leak_terms=None, no_hold_final=True, scenario="dance", codex_alone=False)
    d = Dance(ns)
    try:
        plan = d.build_plan()
        check("dance: build_plan is the 8 scenes in order, then realdirs, each for both CLIs",
              plan == [(f, "both") for f in ["setup", "mail", "poem", "guard", "review", "news", "memory", "replay", "realdirs"]],
              str(plan))
        check("dance: Live's own tables are untouched", sl.Live.FEATURES is sl.FEATURES and Dance.FEATURES is not sl.FEATURES)
        check("dance: Claude runs Sonnet unless the environment says otherwise",
              d.claude_model == os.environ.get("SELFTEST_CLAUDE_MODEL", "sonnet"), d.claude_model)
        check("dance: realdirs is in Dance's own tables", "realdirs" in CHAPTERS and any(f == "realdirs" for f, _t, _s in FEATURES))
        d.record("mail", "both", PASS)
        d.record("realdirs", "both", PASS)
        tbl = sl.render_table(d.results, pane_rows=d.PANE_ROWS, features=d.FEATURES)
        check("dance: record() and the table use Dance's own titles",
              "scene 2: mail between the two models" in tbl and "real dirs untouched" in tbl and "boot context" not in tbl, tbl)
        d.panes_of = {"status": "%0"}
        d.guard_tripped = d.quit = d.aborted = False
        d.hold_final = False
        d.tmux = lambda *a, **k: None
        d.finish_watch(wait=False)
        done = d.status_log[-1]
        check("dance: finish_watch's final table is rendered from Dance's tables",
              done.get("phase") == "done" and any("scene 2: mail between the two models" in ln for ln in done.get("table", [])),
              str(done.get("table")))
        d.results.clear()
        d.panes_of = {}
        # B1: the bus log and the nightly record are read where the tools write them (dot directories)
        bus_path, lc_path = d.state_paths()
        if not (bus_path.startswith(d.home + os.sep) and lc_path.startswith(d.home + os.sep)):
            check("dance: the state paths sit under the sandbox home", False, "%s %s" % (bus_path, lc_path))
            raise SystemExit("refusing to write outside the sandbox: %s %s" % (bus_path, lc_path))
        for pth in (bus_path, lc_path):
            os.makedirs(os.path.dirname(pth), exist_ok=True)
        with open(bus_path, "w") as fh:
            fh.write(json.dumps({"id": "a", "ts": 1.0, "from": "cA", "to": "xA", "body": "MAIL" + d.tok}) + "\n")
        with open(lc_path, "w") as fh:
            json.dump({"rc": 0, "checks": [{"agent": "claude", "check": "x", "status": "clean"}]}, fh)
        check("dance: the bus log written at the notes_config path is read back by bus_msgs and bus_find",
              len(d.bus_msgs()) == 1 and d.bus_find("cA", "xA", "MAIL" + d.tok) is not None, bus_path)
        check("dance: the nightly record at the notes_config path is read back (scene 7's lookup)",
              (d.read_last_check() or {}).get("rc") == 0, lc_path)
        check("dance: the state paths sit under the sandbox home", True)
        check("dance: negative control, a message not on the bus is not found", d.bus_find("xA", "cA", "MAIL" + d.tok) is None)
        # B2: the ask is read from the newest assistant reply, in the real Claude key order (reply text NOT last)
        def transcript(rid, reply):
            tdir = os.path.join(d.claude_dir, "projects", "p" + rid)
            os.makedirs(tdir, exist_ok=True)
            with open(os.path.join(tdir, "s.jsonl"), "w") as fh:
                fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "go (run id %s)" % rid}}) + "\n")
                fh.write(json.dumps({"parentUuid": "p1", "isSidechain": False,
                                     "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]},
                                     "requestId": "r", "type": "assistant", "uuid": "u1", "timestamp": "2026-11-24T10:00:00Z",
                                     "userType": "external", "cwd": "/w", "sessionId": "s1", "version": "2", "gitBranch": "main"}) + "\n")
        transcript("RIDA", "The merge is refused: no yes yet.\n\nmerge #7?")
        transcript("RIDB", "Done. Nothing to ask.")
        transcript("RIDC", "Ready.\n\nmerge #8?")
        check("dance: a reply ending on `merge #7?` is read as the ask though the line's last string is metadata",
              d.asked_merge("RIDA"))
        check("dance: negative control, a reply with no ask is not", not d.asked_merge("RIDB"))
        check("dance: negative control, an ask for another PR is not", not d.asked_merge("RIDC"))
        # the "user types: yes" note is on the status BEFORE the keystroke, and cleared after
        d.plan = plan
        d.panes.add("cA")
        d.pace = 0.0
        d.begin_step("review", "both", quick=True)
        at_send = []
        d.send = lambda name, text: at_send.append((name, text, d.status_log[-1].get("note")))
        d.user_types("cA", "yes")
        check("dance: the note `user types: yes` is in the status when the keystroke goes in",
              at_send == [("cA", "yes", "user types: yes")], str(at_send))
        check("dance: the note is cleared after the keystroke", d.status_log[-1].get("note") == "", str(d.status_log[-1]))
        check("dance: the status pane shows the note",
              "user types: yes" in sl.render_status({"phase": "running", "note": "user types: yes"})[2])
        check("dance: negative control, no note shows the usual hint",
              "Enter: next step" in sl.render_status({"phase": "running", "note": ""})[2])
    finally:
        shutil.rmtree(d.root, ignore_errors=True)
    # --scenario argument validation, in-process: main() runs with a stub in place of every constructor,
    # so a refusal that regressed would raise instead of starting a live run
    import io
    def main_rc(*args):
        class Boom(Exception):
            pass
        def boom(*a, **k):
            raise Boom("main() went on to build a run")
        old = (sys.argv, sys.stderr, sl.Live, sl.load_scenario)
        sys.argv, sys.stderr, sl.Live, sl.load_scenario = ["selftest_live.py"] + list(args), io.StringIO(), boom, boom
        try:
            return sl.main(), sys.stderr.getvalue()
        except Boom as exc:
            return "built", str(exc)
        except SystemExit as exc:
            return exc.code, sys.stderr.getvalue()
        finally:
            sys.argv, sys.stderr, sl.Live, sl.load_scenario = old
    rc, err = main_rc("--scenario", "dance")
    check("dance: --scenario without --watch is refused (exit 2)", rc == 2 and "--watch" in err, "%s %s" % (rc, err))
    for flag in (["--feature", "boot"], ["--only", "claude"], ["--codex-alone"]):
        rc, err = main_rc("--watch", "--scenario", "dance", *flag)
        check("dance: --scenario dance with %s is refused (exit 2)" % flag[0], rc == 2 and flag[0] in err, "%s %s" % (rc, err))
    rc, _err = main_rc("--watch", "--scenario", "nope")
    check("dance: an unknown scenario is refused (exit 2)", rc == 2)
    rc, _err = main_rc("--watch", "--scenario", "dance")
    check("dance: negative control, a valid --watch --scenario dance goes on to build the run", rc == "built", str(rc))
    check("dance: negative control, scenario_problem allows --watch --scenario dance alone",
          sl.scenario_problem(argparse.Namespace(scenario="dance", watch=True, feature=None, only=None, codex_alone=False)) is None)
    # the settings patch and the launchers
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        sp = os.path.join(tmp, "settings.json")
        with open(sp, "w") as fh:
            json.dump({"hooks": {"UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "python3 /x/hooks/session_bus_hook.py"}]},
                {"hooks": [{"type": "command", "command": "python3 /x/hooks/merge_consent.py"}]}]}}, fh)
        n = patch_consent_hook(sp, "/sb/consent_hook.py")
        txt = open(sp).read()
        check("dance: the settings patch repoints the consent hook and nothing else",
              n == 1 and "python3 /sb/consent_hook.py" in txt and "session_bus_hook.py" in txt and "merge_consent" not in txt, txt)
        with open(sp, "w") as fh:
            json.dump({"UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "SESSION_NOTES_AGENT=codex '/h/.local/bin/session-notes' hook time_sense"},
                {"type": "command", "command": "SESSION_NOTES_AGENT=codex '/h/.local/bin/session-notes' hook merge_consent"}]}]}, fh)
        n = patch_consent_hook(sp, "/sb/consent_hook.py")
        txt = open(sp).read()
        check("dance: the Codex hooks.json consent hook is repointed too, and no other hook",
              n == 1 and "SESSION_NOTES_AGENT=codex python3 /sb/consent_hook.py" in txt and "hook time_sense" in txt
              and "hook merge_consent" not in txt, txt)
        with open(sp, "w") as fh:
            json.dump({"hooks": {}}, fh)
        check("dance: negative control, no consent hook patches nothing", patch_consent_hook(sp, "/sb/c.py") == 0)
        # the real hook through the launcher: a typed `merge #7` lands in the sandbox grants file
        env = sl.git_env(os.path.join(tmp, "home"))
        os.makedirs(env["HOME"])
        cfg = os.path.join(tmp, "config.toml")
        open(cfg, "w").close()
        env["SESSION_NOTES_CONFIG"] = cfg
        bare, proj = os.path.join(tmp, "gh", "o", "r.git"), os.path.join(tmp, "proj")
        os.makedirs(os.path.dirname(bare))
        sl.sh(["git", "init", "-q", "--bare", "-b", "main", bare], tmp, env)
        sl.sh(["git", "clone", "-q", bare, proj], tmp, env)
        grants = os.path.join(tmp, "consent", "grants.json")
        launcher = os.path.join(tmp, "consent_hook.py")
        repo = os.path.dirname(os.path.dirname(here))
        write_launcher(launcher, HOOK_LAUNCH, hook=os.path.join(repo, "hooks", "merge_consent.py"), grants=grants)
        import pwd
        hook_mod = load_hook()
        check("dance: negative control, the loaded hook's override is unset", hook_mod.GRANTS_PATH_OVERRIDE is None)
        real = hook_mod.grants_path()  # the real one: the passwd home plus the hook's own GRANTS_REL
        check("dance: the real grants path is the passwd home plus the hook's GRANTS_REL",
              real == os.path.join(pwd.getpwuid(os.getuid()).pw_dir, *hook_mod.GRANTS_REL), real)
        real_seen = os.path.join(os.path.dirname(real), "seen", "dance")
        stat = lambda p: os.stat(p).st_mtime_ns if os.path.exists(p) else None
        before = (stat(real), stat(real_seen))
        for _ in (1, 2):  # the first prompt of a session never grants
            subprocess.run([sys.executable, launcher], input=json.dumps({"prompt": "merge #7", "session_id": "dance",
                                                                         "cwd": proj}), env=env, capture_output=True, text=True, timeout=60)
        try:
            gr = json.load(open(grants))["grants"]
        except (OSError, ValueError, KeyError):
            gr = []
        check("dance: the real consent hook, through the launcher, records the grant in the sandbox with repo o/r",
              len(gr) == 1 and gr[0].get("pr") == 7 and gr[0].get("repo") == "o/r" and gr[0].get("prompt") == "merge #7", str(gr))
        after = (stat(real), stat(real_seen))
        check("dance: the real grants file and its seen/dance marker were not touched", before == after, "%s -> %s" % (before, after))
        check("dance: the sandbox got its own seen/dance marker beside the sandbox grants",
              os.path.exists(os.path.join(os.path.dirname(grants), "seen", "dance")))
        wt = os.path.join(tmp, "wt")
        write_launcher(wt, WT_LAUNCH, wt=os.path.join(repo, "bin", "wt.py"), grants=grants)
        r = subprocess.run([sys.executable, wt, "--help"], env=env, capture_output=True, text=True, timeout=60)
        check("dance: the wt launcher runs wt", r.returncode == 0 and "merge" in r.stdout, "%s %s" % (r.returncode, r.stderr[-120:]))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        genv = sl.git_env(tmp)
        gg = lambda cwd, *a: sl.sh(["git"] + list(a), cwd, genv, check=False)
        bare, clone = os.path.join(tmp, "o", "r.git"), os.path.join(tmp, "clone")
        os.makedirs(os.path.dirname(bare))
        gg(tmp, "init", "-q", "--bare", "-b", "main", bare)
        gg(tmp, "clone", "-q", bare, clone)
        with open(os.path.join(clone, "poem.md"), "w") as fh:
            fh.write("one\n")
        gg(clone, "add", "poem.md")
        gg(clone, "commit", "-q", "-m", "first")
        gg(clone, "push", "-q", "origin", "HEAD:poem")
        h1 = gg(clone, "rev-parse", "HEAD").stdout.strip()
        check("dance: remote_head reads the bare remote's branch", remote_head(gg, clone, "poem") == h1, str(remote_head(gg, clone, "poem")))
        with open(os.path.join(clone, "poem.md"), "a") as fh:
            fh.write("two\n")
        gg(clone, "commit", "-q", "-am", "fix")
        h2 = gg(clone, "rev-parse", "HEAD").stdout.strip()
        check("dance: an unpushed fix commit is not the remote head (wt merge would say push first)",
              h2 != h1 and remote_head(gg, clone, "poem") != h2)
        fp = fix_prompt(clone, "needs a line", "X1")
        check("dance: the fix-beat prompt commits and pushes origin poem, and no longer forbids pushing",
              "git push -q origin poem" in fp and "do not push" not in fp.lower() and fp.index("git commit") < fp.index("git push"), fp)
        gg(clone, "push", "-q", "origin", "HEAD:poem")
        check("dance: after the push the remote head equals the new local HEAD", remote_head(gg, clone, "poem") == h2)
        check("dance: every path the check wrote sits under the temp dir", all(os.path.realpath(x).startswith(tmp) for x in (bare, clone)))
    lines = timeline_lines([{"ts": 2, "from": "xA", "to": "cA", "body": "b " * 80}, {"ts": 1, "from": "cA", "to": "xA", "body": "a"}])
    check("dance: the timeline is in time order, one short line per message",
          len(lines) == 2 and "cA -> xA: a" in lines[0] and all(len(x) <= 86 for x in lines), str(lines))
