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
# without this a sandbox session's "yes" would be written under the real ~/.claude. Both use the
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


def write_launcher(path, template, **kw):
    with open(path, "w") as fh:
        fh.write(template % kw)
    os.chmod(path, 0o755)


def patch_consent_hook(settings_path, launcher):
    """Point every merge_consent hook command in a Claude settings file at `launcher`. -> how many."""
    with open(settings_path) as fh:
        doc = json.load(fh)
    n = [0]

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "command" and isinstance(v, str) and "hooks/merge_consent.py" in v:
                    o[k] = re.sub(r"\S*hooks/merge_consent\.py", launcher, v)
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

    def bus_msgs(self):
        out = []
        for f in glob.glob(os.path.join(self.home, "**", "session-bus", "bus.jsonl"), recursive=True):
            try:
                with open(f, encoding="utf-8") as fh:
                    out += [json.loads(x) for x in fh.read().splitlines() if x.strip()]
            except (OSError, ValueError):
                pass
        return sorted(out, key=lambda m: m.get("ts", 0))

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
        n = patch_consent_hook(os.path.join(self.claude_dir, "settings.json"), hook)
        if not n:
            raise sl.Abort("the sandbox Claude settings carry no merge_consent hook to point at the sandbox grants")
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
        rid = "GUARD" + t
        refusal = re.compile(r"checkout_guard: \S*poem\.md is tracked on")
        main_poem = os.path.join(self.proj, "poem.md")
        self.wait_idle(c)
        self.send(c, "Add this poem to %s, one line each, below the title: %s. Use the Edit tool directly on that exact "
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
        view = {"number": 7, "headRefOid": head, "state": "OPEN", "url": "https://example.invalid/o/r/pull/7",
                "title": "Add the poem", "body": "Adds a four line poem to poem.md. A small, correct change."}
        with open(os.path.join(self.ghdir, "scenario.json"), "w") as fh:
            json.dump({"view": view, "login": "tester", "comments": [], "diff": diff}, fh)
        try:
            os.remove(os.path.join(self.ghdir, "posted.jsonl"))
        except OSError:
            pass
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
        r = self.turn("codex", "$review 7 " + rules, "dance-review.txt", 720, "danceRev", cwd=path,
                      until=lambda: bool(self.posted_comments()))
        posted = self.posted_comments()
        self.log("dance-review.txt", "\nposted comments: %s\n" % json.dumps(posted))
        if not posted:
            return FAIL, "the Codex review ended (rc=%s) without a verdict comment" % r.returncode, "dance-review.txt"
        first = (posted[-1].get("body") or "").splitlines()[0] if posted[-1].get("body") else ""
        m = re.fullmatch(r"Review verdict: (APPROVE|CHANGES) ([0-9a-f]{40})", first)
        if not m:
            return FAIL, "the recorded comment does not start with the verdict marker: %r" % first[:80], "dance-review.txt"
        if m.group(2) != head:
            return FAIL, "the verdict names sha %s, not the PR head %s" % (m.group(2)[:10], head[:10]), "dance-review.txt"
        if m.group(1) != "APPROVE":
            return FAIL, "the reviewer returned CHANGES on a small correct change", "dance-review.txt"
        if any(x[:2] == ["pr", "merge"] for x in self.gh_calls()):
            return FAIL, "a merge happened before the user's yes", "dance-review.txt"
        # d) the merge question: Claude asks, the user types yes, the real hook records it
        rid2 = "MERGEB" + t
        self.wait_idle(c)
        self.send(c, "Run exactly this shell command: wt merge poem  If it is refused, do what its message says. When the "
                     "user later answers yes, run `wt merge poem` once more. End the reply in which you ask for the yes "
                     "with exactly this last line: merge #7?  (run id %s)" % rid2)
        asked = lambda: re.search(r"merge #7\?\s*$", self.pane_reply("claude", rid2).strip())
        if not self.wait_for(asked, 240):
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
        return PASS, ("wt merge refused before a review; Codex's APPROVE sits at the PR head; the typed yes was recorded by "
                      "the real hook and the merge went through the stub gh"), "dance-review.txt"

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
        out = os.path.join(self.logs, "nightly.txt")
        self.show_command("claude", "nightly", "python3 %s > %s 2>&1; echo \"nightly_check exit code: $?\"" % (
            shlex.quote(os.path.join(self.repo, "bin", "nightly_check.py")), shlex.quote(out)))
        recs = glob.glob(os.path.join(self.home, "**", "last-check.json"), recursive=True)
        try:
            with open(recs[0]) as fh:
                doc = json.load(fh)
        except (IndexError, OSError, ValueError):
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
    # --scenario argument validation: the real entry point refuses before anything is built
    def rc_of(*args):
        r = subprocess.run([sys.executable, main_py] + list(args), capture_output=True, text=True, timeout=60)
        return r.returncode, r.stderr
    rc, err = rc_of("--scenario", "dance")
    check("dance: --scenario without --watch is refused (exit 2)", rc == 2 and "--watch" in err, "%s %s" % (rc, err))
    for flag in (["--feature", "boot"], ["--only", "claude"], ["--codex-alone"]):
        rc, err = rc_of("--watch", "--scenario", "dance", *flag)
        check("dance: --scenario dance with %s is refused (exit 2)" % flag[0], rc == 2 and flag[0] in err, "%s %s" % (rc, err))
    rc, _err = rc_of("--watch", "--scenario", "nope")
    check("dance: an unknown scenario is refused (exit 2)", rc == 2)
    check("dance: negative control, --watch --scenario dance alone raises no problem",
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
        real = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, *(".claude", "session-notes", "state", "merge-consent", "grants.json"))
        before = os.stat(real).st_mtime_ns if os.path.exists(real) else None
        for _ in (1, 2):  # the first prompt of a session never grants
            subprocess.run([sys.executable, launcher], input=json.dumps({"prompt": "merge #7", "session_id": "dance",
                                                                         "cwd": proj}), env=env, capture_output=True, text=True, timeout=60)
        try:
            gr = json.load(open(grants))["grants"]
        except (OSError, ValueError, KeyError):
            gr = []
        check("dance: the real consent hook, through the launcher, records the grant in the sandbox with repo o/r",
              len(gr) == 1 and gr[0].get("pr") == 7 and gr[0].get("repo") == "o/r" and gr[0].get("prompt") == "merge #7", str(gr))
        after = os.stat(real).st_mtime_ns if os.path.exists(real) else None
        check("dance: the real grants file under the user's home was not touched", before == after, "%s -> %s" % (before, after))
        wt = os.path.join(tmp, "wt")
        write_launcher(wt, WT_LAUNCH, wt=os.path.join(repo, "bin", "wt.py"), grants=grants)
        r = subprocess.run([sys.executable, wt, "--help"], env=env, capture_output=True, text=True, timeout=60)
        check("dance: the wt launcher runs wt", r.returncode == 0 and "merge" in r.stdout, "%s %s" % (r.returncode, r.stderr[-120:]))
    lines = timeline_lines([{"ts": 2, "from": "xA", "to": "cA", "body": "b " * 80}, {"ts": 1, "from": "cA", "to": "xA", "body": "a"}])
    check("dance: the timeline is in time order, one short line per message",
          len(lines) == 2 and "cA -> xA: a" in lines[0] and all(len(x) <= 86 for x in lines), str(lines))
