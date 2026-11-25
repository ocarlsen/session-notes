#!/usr/bin/env python3
"""handoff_session -- hand work off to a new, named Claude Code or Codex session in tmux.

The new session records which session launched it (see session_lineage.py). Usually it starts
on its task at once (--prompt); in staged mode (--spec) the task waits on the bus until a human
types in the new pane.

    handoff_session.py --name task-b --prompt-file /tmp/p.md
    handoff_session.py --name images --spec "generate today's images ..."
    handoff_session.py --name task-a --spec-file /tmp/spec.md --cwd /path/to/project
    handoff_session.py --cli codex --name task-c --prompt-file /tmp/p.md

WHICH CLI. --cli claude|codex picks the agent in the new pane. Without it: codex when
SESSION_NOTES_AGENT=codex (set by `install.sh --codex` for Codex's shell, so a Codex session
hands off to Codex), otherwise claude; and when no `claude` binary resolves but `codex` does (a
Codex-only machine, typed in your own shell) it launches codex. With neither, the error names
`--cli codex`. The Claude path is unchanged. Codex launches as
`codex --no-daemon` (hooks run in the pane only without the daemon) with the prompt as its
positional argument. Flag mapping for codex:

    --model M      -> -m M
    --bypass       -> --dangerously-bypass-approvals-and-sandbox  (the twin of Claude's
                      --dangerously-skip-permissions: no approval prompts AND no sandbox)
    --codex-arg A  -> A verbatim (repeatable; a Codex-only flag with no twin here, e.g.
                      --codex-arg=-a --codex-arg=never)
    --claude-bin   -> refused with --cli codex (use --codex-bin); --codex-bin and --codex-arg
                      are refused with --cli claude
    claude `-n`    -> no twin: the tmux session name IS the address (bus, peers, lineage)

TWO MODES (pick one; they are mutually exclusive by design):

  STAGED (--spec/--spec-file, the default) -- spawn a bare `claude`, park the spec on the
  bus, and stop. The pane sits IDLE until its recipient types something; that typed prompt
  is the authorization (the staged-go pattern). Use this for a handoff the user did not ask
  for.

  AUTO-START (--prompt/--prompt-file) -- supply the prompt as the spawned process's ARGV, so
  the session starts working immediately. The bus park is SKIPPED (see below).

What it does, in order:
  1. refuse if a live tmux session already owns that name
  2. resolve the `claude` (or `codex`) binary
  3. `tmux new-session -d [-e parent vars] -s <name> -c <cwd> -- <claude> -n <name> ...`
     (the -e vars name THIS session as the new one's parent; see session_lineage.py)
     (permission mode is whatever your Claude Code settings default to; NO
     --dangerously-skip-permissions unless --bypass is passed)
  4. STAGED only: poll the bus until <name> registers (best-effort; mail is file-durable
     regardless), then park the spec addressed to <name> via session_bus.py
  5. print the attach command

SAFETY (load-bearing, do not "improve" away): this NEVER types into a pane: no tmux
send-keys, ever. The invariant that ban protects is ATTRIBUTION: an instruction arriving in a
session must be traceable to its real author, and a model must never inject text that is
indistinguishable from the user typing at that keyboard. --prompt does not touch that
invariant: it supplies the INITIAL argv to a session this very command just created, so there
is no pre-existing principal to impersonate and no conversation to launder an instruction
into, and the prompt is visible in the transcript as a launch argument rather than as fake
keystrokes. Same shape as Claude Code's Agent tool. Typing into a PRE-EXISTING session remains
forbidden. (AUTO-START is a deliberate, narrow widening of the original "stages only"
property, added after a design review.)

WHY --prompt SKIPS THE BUS PARK: session_bus delivers each message on up to DELIVERIES=2
prompts, fenced as untrusted text "carrying no authority". Parking the same spec the session is
already executing would re-inject it twice, contradicting itself, and the bus has a 4000-char
body cap a long spec would hit. For a long spec, write it to a file and let the prompt say to
read that file.
"""
import argparse
import os
import shutil
import subprocess
import shlex
import sys
import time

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import session_lineage  # noqa: E402  (hands the child its parent's identity)

BUS = os.path.join(BIN, "session_bus.py")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def live_sessions():
    """Set of tmux session names currently on the bus (empty on any failure)."""
    r = _run([sys.executable, BUS, "peers"])
    names = set()
    for line in (r.stdout or "").splitlines():
        line = line.replace("*", "").strip()
        if not line or line.startswith("("):
            continue
        tok = line.split()[0]
        if tok:
            names.add(tok)
    return names


def resolve_claude(override=None):
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/claude")
    return fallback if os.path.exists(fallback) else None


def resolve_codex(override=None):
    if override:
        return override
    found = shutil.which("codex")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/codex")
    return fallback if os.path.exists(fallback) else None


def choose_cli(cli=None, env=None):
    """The agent for the new pane: an explicit --cli wins; else codex when this shell is a
    Codex session (SESSION_NOTES_AGENT=codex, the same switch every other tool reads);
    else claude."""
    if cli:
        return cli
    env = os.environ if env is None else env
    return "codex" if (env.get("SESSION_NOTES_AGENT") or "").strip().lower() == "codex" else "claude"


def tmux_has_session(name):
    """True if tmux itself knows this session (independent of bus registration)."""
    if shutil.which("tmux") is None:
        return False
    return _run(["tmux", "has-session", "-t", "=" + name]).returncode == 0


# The model for a handed-off worker. The default is INHERIT (None): no --model flag, so the
# session takes your interactive default. The reasoning is about LAYERS: the spawned session
# is usually an ORCHESTRATOR (it reads the spec, checks the premise, decides the shape, judges
# what comes back, and may delegate the build to a cheaper sub-agent), which wants the big
# model. Defaulting this to a cheap model would put it at the wrong layer. --model exists for
# a deliberate exception, and whatever is chosen is PRINTED on spawn: the failure that
# prompted this was invisible spend, and a dispatcher who cannot see which model they just
# paid for cannot correct it.
DEFAULT_HANDOFF_MODEL = None


def build_spawn_cmd(name, cwd, claude, bypass=False, prompt=None,
                    model=DEFAULT_HANDOFF_MODEL, parent_args=None):
    """The full `tmux new-session` argv. LIST form, never a shell string: a spec
    containing an apostrophe would otherwise break the spawn.

    `claude -n` is ALWAYS <name>: that is what SendMessage/ListAgents address, so it
    stays the single, short tmux session name with no separate display alias.

    TWO `--` separators, both load-bearing:
      * before the inner command, so tmux stops parsing its own options;
      * before the PROMPT, so the claude CLI takes it as a positional. A prompt whose
        first line is a markdown bullet ("- cut a worktree ...") is otherwise parsed as
        a CLI flag: verified, `claude --print "-Reply ..."` ->
        "error: unknown option", while `claude --print -- "-Reply ..."` succeeds. In a
        detached tmux pane that error kills the session instantly, so the failure is
        invisible unless this separator is present.

    `parent_args`: the `-e VAR=value` pairs from session_lineage.spawn_env_args(),
    identifying THIS session as the new session's parent. They go right after
    `new-session -d` and before `-s <name>`, tmux's own option position for `-e`;
    everything after the first `--` is the spawned claude's argv, never tmux's.
    None or [] leaves the argv unchanged.
    """
    inner = [claude, "-n", name]
    # --model rides BEFORE the end-of-options separator, for the same reason --bypass
    # does: everything after `--` is a positional and would become part of the prompt.
    # model=None means INHERIT (emit no flag at all), which is why this keys on `is not None`
    # rather than truthiness: an empty string is a caller error, not a request to inherit.
    if model is not None:
        inner += ["--model", model]
    if bypass:
        inner.append("--dangerously-skip-permissions")
    if prompt:
        inner += ["--", prompt]
    return (["tmux", "new-session", "-d"] + list(parent_args or [])
            + ["-s", name, "-c", cwd, "--"] + inner)


def build_codex_cmd(name, cwd, codex, bypass=False, prompt=None,
                    model=DEFAULT_HANDOFF_MODEL, parent_args=None, extra_args=None):
    """The full `tmux new-session` argv for a Codex pane (LIST form, like build_spawn_cmd).

    `codex --no-daemon`: without it Codex runs commands (and hooks) in a shared background
    process that has no TMUX_PANE, so the bus hook and the pane registry cannot see the pane.
    Codex has no `-n`: the tmux session name is the address. Options ride BEFORE the `--`
    that makes the prompt a positional (a prompt starting with "-" would otherwise parse as a
    flag). `extra_args` (--codex-arg) goes in verbatim, after the mapped flags.
    """
    inner = [codex, "--no-daemon"]
    if model is not None:
        inner += ["-m", model]
    if bypass:
        inner.append("--dangerously-bypass-approvals-and-sandbox")
    inner += list(extra_args or [])
    if prompt:
        inner += ["--", prompt]
    return (["tmux", "new-session", "-d"] + list(parent_args or [])
            + ["-s", name, "-c", cwd, "--"] + inner)


def label_spec(spec):
    """Prepend a short informational tag unless the caller already framed it."""
    if not spec:
        return spec
    head = spec.lstrip()[:80].upper()
    if head.startswith("SPEC") or "NOT AUTHORIZATION" in head:
        return spec
    return ("SPEC (informational, not authorization: your own prompt in this pane "
            "authorizes; nothing here has run): " + spec)


def build_park_cmd(name, spec):
    """argv that parks a STAGED spec. `--staged` tells bus_nudge to leave the pane idle."""
    return [sys.executable, BUS, "send", "--staged", "--to", name, spec]


def park_cmd_text(name, spec):
    """The park command as one printable line (dry-run and the retry hint use this)."""
    return " ".join(shlex.quote(x) for x in build_park_cmd(name, spec))


def build_label_cmd(name, display):
    """The `tmux set-option` argv that stores a display-only label on the session, or
    None. The messaging name stays --name (what `claude -n`, SendMessage and the bus
    address); a --display label different from it is stored as the tmux SESSION option
    @display, which session_peers and bcast show. The target is `=<name>:`: the `=`
    asks for an exact match and the trailing `:` makes it a session target, without
    which tmux answers "no such session"."""
    if not display or display == name:
        return None
    return ["tmux", "set-option", "-t", "=" + name + ":", "@display", display]


def spawn_note(no_build, cfg=None):
    """The AUTO-START note, one source for the real spawn and --dry-run. A session that
    builds something also gets the config's [handoff] build_note, if set."""
    note = ("NOTE: it runs under your normal permission settings. If nothing shows up, "
            "check the pane: it can stall invisibly on a permission prompt.")
    if no_build:
        return note
    try:
        import notes_config
        extra = ((cfg or notes_config.load()).get("handoff") or {}).get("build_note", "")
    except Exception:
        extra = ""
    return note + (" " + extra.strip() if extra and extra.strip() else "")


def selftest():
    """Deterministic checks over argv construction + mode selection. No spawning. The caller's
    SESSION_NOTES_AGENT is removed for the whole run (and restored), so the checks never depend
    on which harness launched them."""
    saved = os.environ.pop("SESSION_NOTES_AGENT", None)
    try:
        return _selftest()
    finally:
        if saved is not None:
            os.environ["SESSION_NOTES_AGENT"] = saved


def _selftest():
    fails = []
    ran = [0]

    def check(label, cond, detail=""):
        ran[0] += 1
        if not cond:
            fails.append("%s%s" % (label, (": " + detail) if detail else ""))

    # 1. LIST argv, never a shell string: an apostrophe in the prompt must survive verbatim.
    #    (Old shape joined the claude command into one shell string, which dropped it entirely.)
    tricky = "read /tmp/x.md; it's the spec for task-alpha (all of it), execute it."
    cmd = build_spawn_cmd("alpha", "/tmp", "/usr/bin/claude", prompt=tricky)
    check("argv is a list of str", all(isinstance(c, str) for c in cmd))
    check("prompt survives verbatim as one argv element", cmd[-1] == tricky,
          "got %r" % cmd[-1])
    check("-n is always --name", cmd[cmd.index("-n") + 1] == "alpha",
          "got %r" % cmd[cmd.index("-n") + 1])

    # 2. A prompt whose first line is a markdown bullet must reach claude as a POSITIONAL.
    #    Verified: without the `--`, `claude` exits "unknown option" and the
    #    detached pane dies instantly while the caller prints "working now".
    dashed = "- cut a worktree\n- execute the spec.\nDo it now."
    c_dash = build_spawn_cmd("delta", "/tmp", "c", prompt=dashed)
    check("leading-dash prompt is preceded by an end-of-options --",
          c_dash[-2] == "--" and c_dash[-1] == dashed, " ".join("%r" % x for x in c_dash))

    # 3. -n is always --name; bypass flag lands BEFORE the end-of-options separator.
    c2 = build_spawn_cmd("beta", "/tmp", "c", bypass=True, prompt="go.")
    check("-n is --name", c2[c2.index("-n") + 1] == "beta")
    check("bypass flag is its own argv element (not shell-joined)",
          "--dangerously-skip-permissions" in c2)
    check("bypass flag precedes the -- separator",
          c2.index("--dangerously-skip-permissions") < len(c2) - 2, " ".join(c2))
    check("prompt stays last under bypass", c2[-1] == "go.")

    # 4. No prompt -> a bare claude, asserted as the FULL expected argv (not a length delta,
    #    which a bug adding one element to every build would satisfy).
    c3 = build_spawn_cmd("gamma", "/tmp", "c")
    check("staged spawn is exactly a bare claude",
          c3 == ["tmux", "new-session", "-d", "-s", "gamma", "-c", "/tmp", "--", "c", "-n", "gamma"],
          " ".join("%r" % x for x in c3))

    # 4a. Lineage: parent_args (session_lineage.spawn_env_args() output) land right after
    #     `new-session -d` and before `-s <name>`: tmux's own option position for `-e`,
    #     distinct from the inner claude argv after `--`.
    pargs = ["-e", "SESSION_NOTES_PARENT_SESSION=work", "-e",
             "SESSION_NOTES_PARENT_CREATED=1700000000", "-e", "SESSION_NOTES_PARENT_PANE=%2"]
    c7 = build_spawn_cmd("eta", "/tmp", "c", prompt="go.", parent_args=pargs)
    check("parent_args ride immediately after `new-session -d`",
          c7[:3 + len(pargs)] == ["tmux", "new-session", "-d"] + pargs,
          " ".join("%r" % x for x in c7))
    check("parent_args precede -s <name>",
          c7[3 + len(pargs):5 + len(pargs)] == ["-s", "eta"], " ".join("%r" % x for x in c7))
    check("prompt still lands last with parent_args set", c7[-1] == "go.")
    check("omitting parent_args adds no -e at all",
          "-e" not in build_spawn_cmd("theta", "/tmp", "c", prompt="go."))
    check("an empty parent_args list is also a no-op",
          "-e" not in build_spawn_cmd("iota", "/tmp", "c", prompt="go.", parent_args=[]))
    #     main() wires spawn_env_args() in: a dry run inside a (stubbed) tmux pane prints the
    #     parent -e args in the would-spawn line. Every tmux/bus probe main() makes is stubbed,
    #     so this never reaches a live tmux server.
    import io
    import contextlib
    g = globals()
    saved = (session_lineage.spawn_env_args, g["live_sessions"], g["tmux_has_session"],
             shutil.which)
    try:
        session_lineage.spawn_env_args = lambda *a, **k: list(pargs)
        g["live_sessions"] = lambda: set()
        g["tmux_has_session"] = lambda name: False
        shutil.which = lambda n: "/usr/bin/" + n
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--name", "kappa", "--prompt", "go.", "--dry-run", "--claude-bin", "c",
                       "--cwd", "/tmp"])
        check("dry run passes spawn_env_args() through to tmux",
              rc == 0 and "'SESSION_NOTES_PARENT_SESSION=work'" in buf.getvalue(),
              "rc=%r out=%r" % (rc, buf.getvalue()[:300]))
        # The STAGED park carries --staged (bus_nudge keeps the pane idle). Stubs above.
        pc = build_park_cmd("gamma", "the spec")
        check("staged park argv includes --staged before --to",
              pc[-5:] == ["send", "--staged", "--to", "gamma", "the spec"], " ".join(pc))
        for flag, val, want in (("--spec", "do it", True), ("--prompt", "go.", False)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--name", "t", flag, val, "--cwd", "/tmp", "--dry-run",
                           "--claude-bin", "c"])
            out = buf.getvalue()
            check("dry-run %s: park line from build_park_cmd %s"
                  % (flag, "printed" if want else "absent"),
                  rc == 0 and (park_cmd_text("t", "<spec>") in out) == want, out[-300:])
        import inspect
        check("the real park call site runs build_park_cmd",
              "_run(build_park_cmd(" in inspect.getsource(main))
        check("the failed-send retry hint says --staged and is built from build_park_cmd",
              "--staged" in park_cmd_text("w", "<spec>")
              and "park_cmd_text(name" in inspect.getsource(main)
              and 'send --to %s' not in inspect.getsource(main))
    finally:
        (session_lineage.spawn_env_args, g["live_sessions"], g["tmux_has_session"],
         shutil.which) = saved

    # 4b. The MODEL, and THE LAYER is what these assert. The spawned session orchestrates,
    #     so the default is INHERIT: no --model at all.
    c_m = build_spawn_cmd("epsilon", "/tmp", "c", prompt="go.")
    check("default handoff emits NO --model (the session inherits and orchestrates)",
          "--model" not in c_m, " ".join("%r" % x for x in c_m))
    check("default handoff still keeps the prompt last", c_m[-1] == "go.")
    #     ASSERT THE LITERAL, never the constant: comparing argv to DEFAULT_HANDOFF_MODEL is
    #     TAUTOLOGICAL, both sides move together, and that mistake once read GREEN under
    #     mutation before it was caught.
    c_mo = build_spawn_cmd("zeta", "/tmp", "c", prompt="go.", model="example-model-1")
    check("an explicit --model is passed through verbatim",
          c_mo[c_mo.index("--model") + 1] == "example-model-1",
          " ".join("%r" % x for x in c_mo))
    check("--model rides BEFORE the end-of-options separator (else it joins the prompt)",
          c_mo.index("--model") < c_mo.index("--", 8), " ".join("%r" % x for x in c_mo))
    check("prompt still lands last with a model set", c_mo[-1] == "go.")
    #     An empty --model is a caller error (an unexpanded shell variable), never a silent
    #     inherit: the two look identical on a command line and mean different things.
    rc = main(["--name", "x", "--prompt", "go.", "--model", "  ", "--dry-run"])
    check("empty --model is refused with rc 2 (not degraded to inherit)", rc == 2,
          "got rc=%r" % rc)

    # 5. Mode selection is by flag PRESENCE, not payload truthiness.
    rc = main(["--name", "x", "--prompt", "go.", "--spec", "s", "--dry-run"])
    check("--prompt + --spec is refused with rc 2", rc == 2, "got rc=%r" % rc)
    rc = main(["--name", "x", "--dry-run"])
    check("neither --prompt nor --spec is refused with rc 2", rc == 2, "got rc=%r" % rc)
    rc = main(["--name", "x", "--prompt", "", "--dry-run"])
    check("empty --prompt is refused with rc 2 (not degraded to STAGED)", rc == 2,
          "got rc=%r" % rc)
    rc = main(["--name", "x", "--spec", "   ", "--dry-run"])
    check("whitespace-only --spec is refused with rc 2", rc == 2, "got rc=%r" % rc)
    # An empty $VAR interpolated inside quotes arrives as whitespace, not "": the auto-mode
    # refusal must strip the same way the staged one does.
    rc = main(["--name", "x", "--prompt", "   ", "--dry-run"])
    check("whitespace-only --prompt is refused with rc 2", rc == 2, "got rc=%r" % rc)

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        empty_path = f.name          # written empty on purpose
    try:
        rc = main(["--name", "x", "--prompt-file", empty_path, "--dry-run"])
        check("empty --prompt-file is refused with rc 2 (the truncated-file case)", rc == 2,
              "got rc=%r" % rc)
        rc = main(["--name", "x", "--prompt-file", empty_path, "--spec", "s", "--dry-run"])
        check("empty --prompt-file still trips mode exclusivity", rc == 2, "got rc=%r" % rc)
    finally:
        os.unlink(empty_path)

    # --display: a separate, post-spawn set-option on the exact session target.
    check("build_label_cmd: exact-match session target with the trailing ':'",
          build_label_cmd("dr10", "release notes") ==
          ["tmux", "set-option", "-t", "=dr10:", "@display", "release notes"])
    check("build_label_cmd: no label, an empty one, or one equal to --name is a no-op",
          [build_label_cmd("a", x) for x in (None, "", "a")] == [None, None, None])
    c_l = build_spawn_cmd("dr10", "/tmp", "c", prompt="go.")
    check("-n stays --name when a label is in play (the label never rides argv)",
          c_l[c_l.index("-n") + 1] == "dr10" and "release notes" not in c_l)
    # The build note: config text for a building session, none with --no-build.
    cfg_n = {"handoff": {"build_note": "It runs under the normal gates."}}
    check("spawn_note: a building session gets the config's build_note",
          spawn_note(False, cfg_n).endswith("It runs under the normal gates."))
    check("spawn_note: --no-build leaves it out",
          "normal gates" not in spawn_note(True, cfg_n))
    check("spawn_note: no build_note configured -> the plain note",
          spawn_note(False, {"handoff": {"build_note": ""}}) == spawn_note(True))
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["--name", "t", "--display", "L", "--prompt", "go.", "--cwd", "/tmp",
                   "--dry-run", "--claude-bin", sys.executable])
    check("--dry-run with --display shows the @display set-option",
          rc == 0 and "'@display' 'L'" in buf.getvalue(), "rc=%r out=%r" % (rc, buf.getvalue()[-300:]))

    # rc 5 gate: no tmux on PATH -> a real launch refuses; a dry run still exits 0 with a note.
    saved_which = shutil.which
    try:
        shutil.which = lambda n: None if n == "tmux" else saved_which(n)
        argv = ["--name", "t", "--prompt", "go.", "--cwd", "/tmp", "--claude-bin", sys.executable]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc_real = main(argv)
        check("no tmux on PATH: a real launch returns 5", rc_real == 5, "rc=%r" % rc_real)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc_dry = main(argv + ["--dry-run"])
        check("no tmux on PATH: --dry-run returns 0 and notes a real launch would fail",
              rc_dry == 0 and "tmux not found on PATH" in err.getvalue()
              and "would fail" in err.getvalue(), "rc=%r err=%r" % (rc_dry, err.getvalue()))
    finally:
        shutil.which = saved_which

    # 6. --cli codex: argv, selection, flag mapping and refusals.
    cx = build_codex_cmd("cx", "/tmp", "/usr/bin/codex", prompt="go.")
    check("codex AUTO-START argv is exactly `codex --no-daemon -- <prompt>`",
          cx == ["tmux", "new-session", "-d", "-s", "cx", "-c", "/tmp", "--", "/usr/bin/codex",
                 "--no-daemon", "--", "go."], " ".join("%r" % x for x in cx))
    cx_s = build_codex_cmd("cx", "/tmp", "c")
    check("codex STAGED argv is a bare `codex --no-daemon`",
          cx_s == ["tmux", "new-session", "-d", "-s", "cx", "-c", "/tmp", "--", "c", "--no-daemon"],
          " ".join("%r" % x for x in cx_s))
    cx_d = build_codex_cmd("cx", "/tmp", "c", prompt=dashed)
    check("codex leading-dash prompt follows an end-of-options --, verbatim and last",
          cx_d[-2] == "--" and cx_d[-1] == dashed)
    cx_m = build_codex_cmd("cx", "/tmp", "c", bypass=True, prompt="go.", model="m1",
                           parent_args=pargs, extra_args=["-a", "never"])
    check("codex --model maps to -m and --bypass to the bypass flag, before the --",
          "-m" in cx_m and cx_m[cx_m.index("-m") + 1] == "m1"
          and "--dangerously-bypass-approvals-and-sandbox" in cx_m
          and cx_m.index("-m") < len(cx_m) - 2
          and cx_m.index("--dangerously-bypass-approvals-and-sandbox") < len(cx_m) - 2,
          " ".join(cx_m))
    check("codex --codex-arg lands verbatim before the prompt separator",
          cx_m[-4:] == ["-a", "never", "--", "go."], " ".join(cx_m))
    check("codex parent_args ride right after `new-session -d`",
          cx_m[:3 + len(pargs)] == ["tmux", "new-session", "-d"] + pargs)
    check("codex argv never carries claude-only flags",
          not any(x in cx_m for x in ("-n", "--model", "--dangerously-skip-permissions")))
    check("choose_cli: explicit wins over the environment",
          choose_cli("claude", {"SESSION_NOTES_AGENT": "codex"}) == "claude"
          and choose_cli("codex", {}) == "codex")
    check("the selftest runs with SESSION_NOTES_AGENT removed (a Codex caller's env cannot leak in)",
          "SESSION_NOTES_AGENT" not in os.environ)
    check("choose_cli: SESSION_NOTES_AGENT=codex selects codex, else claude",
          choose_cli(None, {"SESSION_NOTES_AGENT": "codex"}) == "codex"
          and choose_cli(None, {"SESSION_NOTES_AGENT": " Codex "}) == "codex"
          and choose_cli(None, {}) == "claude"
          and choose_cli(None, {"SESSION_NOTES_AGENT": "claude"}) == "claude")
    saved_which = shutil.which
    saved_probes = (g["live_sessions"], g["tmux_has_session"], session_lineage.spawn_env_args)
    try:
        shutil.which = lambda n: "/usr/bin/" + n
        g["live_sessions"] = lambda: set()
        g["tmux_has_session"] = lambda name: False
        session_lineage.spawn_env_args = lambda *a, **k: []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--cli", "codex", "--name", "x", "--prompt", "go.", "--dry-run", "--cwd", "/tmp",
                       "--codex-bin", "cbin", "--model", "m1", "--bypass", "--codex-arg=-a",
                       "--codex-arg=never"])
        out = buf.getvalue()
        check("codex --dry-run prints the exact argv with the mapped flags",
              rc == 0 and "'cbin' '--no-daemon' '-m' 'm1' '--dangerously-bypass-approvals-and-sandbox' "
              "'-a' 'never' '--' 'go.'" in out, "rc=%r out=%r" % (rc, out[:400]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--cli", "codex", "--name", "x", "--spec", "s", "--dry-run", "--cwd", "/tmp",
                       "--codex-bin", "cbin"])
        line = [l for l in buf.getvalue().splitlines() if "would spawn:" in l]
        check("codex staged --dry-run: bare codex argv and the bus park announced",
              rc == 0 and line and line[0].endswith("'cbin' '--no-daemon'")
              and "would park spec" in buf.getvalue(), buf.getvalue()[:400])
        saved_env = os.environ.get("SESSION_NOTES_AGENT")
        os.environ["SESSION_NOTES_AGENT"] = "codex"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--name", "x", "--prompt", "go.", "--dry-run", "--cwd", "/tmp",
                           "--codex-bin", "cbin"])
            check("SESSION_NOTES_AGENT=codex with no --cli launches codex",
                  rc == 0 and "'cbin' '--no-daemon'" in buf.getvalue(), buf.getvalue()[:300])
        finally:
            if saved_env is None:
                os.environ.pop("SESSION_NOTES_AGENT", None)
            else:
                os.environ["SESSION_NOTES_AGENT"] = saved_env
        # A Codex-only machine (no claude on PATH or in ~/.local/bin), typed in the user's own shell:
        # no --cli means codex when codex resolves; with neither binary the error names --cli codex.
        saved = {k: os.environ.get(k) for k in ("SESSION_NOTES_AGENT", "PATH", "HOME")}
        stub_which = shutil.which
        with tempfile.TemporaryDirectory() as nohome:
            try:
                shutil.which = saved_which  # the real lookup, over the empty PATH below
                os.environ.pop("SESSION_NOTES_AGENT", None)
                os.environ["PATH"], os.environ["HOME"] = nohome, nohome
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                    rc = main(["--name", "x", "--prompt", "go.", "--dry-run", "--cwd", "/tmp",
                               "--codex-bin", "cbin"])
                check("no claude binary, codex resolves, no --cli: launches codex",
                      rc == 0 and "'cbin' '--no-daemon'" in buf.getvalue(), "rc=%s %s" % (rc, buf.getvalue()[:300]))
                errbuf = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errbuf):
                    rc = main(["--name", "x", "--prompt", "go.", "--dry-run", "--cwd", "/tmp"])
                check("no claude and no codex binary: rc 4, the error names --cli codex",
                      rc == 4 and "--cli codex" in errbuf.getvalue(), errbuf.getvalue()[:300])
            finally:
                shutil.which = stub_which
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        errbuf = io.StringIO()
        with contextlib.redirect_stderr(errbuf):
            rc1 = main(["--cli", "codex", "--name", "x", "--prompt", "go.", "--claude-bin", "c",
                        "--dry-run"])
            e1 = errbuf.getvalue()
            rc2 = main(["--cli", "claude", "--name", "x", "--prompt", "go.", "--codex-bin", "c",
                        "--dry-run"])
            rc3 = main(["--cli", "claude", "--name", "x", "--prompt", "go.", "--codex-arg=-a",
                        "--dry-run"])
        check("--claude-bin with --cli codex is refused (rc 2), naming --codex-bin",
              rc1 == 2 and "--codex-bin" in e1, "rc=%r err=%r" % (rc1, e1))
        check("--codex-bin / --codex-arg with --cli claude are refused (rc 2)", rc2 == 2 and rc3 == 2,
              "rc=%r,%r" % (rc2, rc3))
    finally:
        shutil.which = saved_which
        (g["live_sessions"], g["tmux_has_session"], session_lineage.spawn_env_args) = saved_probes

    print("handoff_session selftest: %d checks, %d failed" % (ran[0], len(fails)))
    for f in fails:
        print("  FAIL %s" % f)
    return 1 if fails else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Hand work off to a new, named Claude Code session in tmux, "
                                "recording its parent. --prompt starts it working; --spec parks the "
                                "task until you type in the new pane.")
    p.add_argument("--cli", choices=("claude", "codex"),
                   help="agent to launch in the new pane. Default: codex when "
                        "SESSION_NOTES_AGENT=codex, else claude")
    p.add_argument("--name", help="tmux session name for the new worker (required unless --selftest)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--spec", help="STAGED mode: spec text to park on the bus for the new session")
    g.add_argument("--spec-file", help="STAGED mode: read the spec from this file")
    q = p.add_mutually_exclusive_group()
    q.add_argument("--prompt", help="AUTO-START mode: initial prompt as the session's argv (it starts working)")
    q.add_argument("--prompt-file", help="AUTO-START mode: read the initial prompt from this file")
    p.add_argument("--display",
                   help="display-only label, stored as the tmux session option @display and "
                        "shown by session_peers and bcast; the messaging name stays --name")
    p.add_argument("--no-build", action="store_true",
                   help="AUTO-START: the session builds nothing, so leave the config's "
                        "[handoff] build_note out of the spawn note")
    p.add_argument("--cwd", default=os.getcwd(), help="working dir for the new session (default: cwd)")
    p.add_argument("--bypass", action="store_true",
                   help="add --dangerously-skip-permissions (default OFF); with --cli codex, "
                        "--dangerously-bypass-approvals-and-sandbox")
    p.add_argument("--model", default=DEFAULT_HANDOFF_MODEL,
                   help="model for the spawned ORCHESTRATOR session. Default: inherit the "
                        "interactive default (the session orchestrates and may delegate the "
                        "build to a cheaper sub-agent). Pass an id only for a deliberate "
                        "exception.")
    p.add_argument("--claude-bin", help="override the claude binary path (--cli claude only)")
    p.add_argument("--codex-bin", help="override the codex binary path (--cli codex only)")
    p.add_argument("--codex-arg", action="append", default=[], metavar="ARG",
                   help="extra argv for codex, verbatim, before the prompt (repeatable; write "
                        "--codex-arg=-a --codex-arg=never so argparse keeps the dash). --cli codex only")
    p.add_argument("--timeout", type=float, default=12.0, help="seconds to wait for bus registration (default 12)")
    p.add_argument("--raw", action="store_true", help="park the spec verbatim, no informational prefix")
    p.add_argument("--dry-run", action="store_true", help="print what would happen; do not spawn or send")
    p.add_argument("--selftest", action="store_true", help="run internal checks and exit")
    a = p.parse_args(argv)

    if a.selftest:
        return selftest()

    name = (a.name or "").strip()
    if not name:
        print("handoff_session: --name is required", file=sys.stderr)
        return 2

    cli = choose_cli(a.cli)
    # No --cli, not a Codex session, and no claude binary here, but codex resolves (a Codex-only
    # machine): launch codex instead of failing.
    if (not a.cli and cli == "claude" and not a.claude_bin
            and not resolve_claude() and resolve_codex(a.codex_bin)):
        cli = "codex"
    if cli == "codex" and a.claude_bin:
        print("handoff_session: --claude-bin has no effect on a codex pane; use --codex-bin.",
              file=sys.stderr)
        return 2
    if cli == "claude" and (a.codex_bin or a.codex_arg):
        print("handoff_session: --codex-bin/--codex-arg only apply with --cli codex "
              "(this launch is claude).", file=sys.stderr)
        return 2

    spec = a.spec
    if a.spec_file:
        try:
            with open(os.path.expanduser(a.spec_file)) as f:
                spec = f.read()
        except OSError as e:
            print("handoff_session: cannot read --spec-file: %s" % e, file=sys.stderr)
            return 2

    prompt = a.prompt
    if a.prompt_file:
        try:
            with open(os.path.expanduser(a.prompt_file)) as f:
                prompt = f.read().strip()
        except OSError as e:
            print("handoff_session: cannot read --prompt-file: %s" % e, file=sys.stderr)
            return 2

    # Mode is decided by PRESENCE of the flag, never by truthiness of its payload. An empty
    # or truncated --prompt-file would otherwise leave prompt == "" and silently degrade to
    # STAGED-with-no-spec: a bare idle session, no park, and a "this only STAGED" message
    # describing a spec that does not exist.
    auto_mode = a.prompt is not None or a.prompt_file is not None
    staged_mode = a.spec is not None or a.spec_file is not None

    # Both modes at once would re-inject the same instructions via the bus (DELIVERIES=2),
    # fenced as "carries no authority", contradicting the prompt already executing.
    if auto_mode and staged_mode:
        print("handoff_session: --prompt/--prompt-file and --spec/--spec-file are mutually "
              "exclusive.\n  AUTO-START (--prompt) already delivers the instructions as argv; "
              "parking the same text on the bus would re-deliver it up to twice more, fenced as "
              "untrusted text carrying no authority.\n  For a long spec: write it to a file and "
              "have --prompt say to read that file.", file=sys.stderr)
        return 2

    # Neither mode: nothing to run and nothing to park, so the spawn would be a bare idle
    # session that prints a "STAGED" note about a spec that does not exist.
    if not auto_mode and not staged_mode:
        print("handoff_session: give --prompt/--prompt-file (auto-start) or --spec/--spec-file "
              "(staged); with neither there is nothing to hand off.", file=sys.stderr)
        return 2

    # An empty payload in either mode is an authoring accident (truncated file, empty $VAR),
    # never an intent. Refuse loudly instead of spawning a session that does nothing.
    if auto_mode and not (prompt or "").strip():
        print("handoff_session: AUTO-START prompt is empty: did the prompt file get "
              "truncated, or an empty variable get interpolated? Refusing to spawn a session "
              "that would sit idle with no instructions.", file=sys.stderr)
        return 2
    if staged_mode and not (spec or "").strip():
        print("handoff_session: STAGED spec is empty; refusing to park an empty spec.",
              file=sys.stderr)
        return 2

    # Validated before any tmux/claude probing, so a caller error never depends on the
    # environment. The flag selects a MODE and keys on
    # `is not None`. Omitting --model means INHERIT; passing it EMPTY is a caller error (a shell
    # variable that did not expand) and is refused loudly rather than silently read as inherit,
    # because those two look identical on the command line and mean different things.
    if a.model is not None and not a.model.strip():
        print("handoff_session: --model is empty; pass a model id, or omit the flag entirely "
              "to inherit the session default.", file=sys.stderr)
        return 2
    if spec and not a.raw:
        spec = label_spec(spec)

    # A prompt-launched session executes what it is handed; a trailing question makes it
    # answer instead of act (the headless non-execution gotcha).
    if prompt and prompt.rstrip().endswith("?"):
        print("handoff_session: WARNING, the prompt ends on a question mark; a launch prompt "
              "should end on an IMPERATIVE or the session may reply instead of execute.",
              file=sys.stderr)

    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    if not os.path.isdir(cwd):
        print("handoff_session: --cwd is not a directory: %s" % cwd, file=sys.stderr)
        return 2

    # --dry-run spawns nothing, so it must not need tmux (a CI container has none).
    if shutil.which("tmux") is None:
        if not a.dry_run:
            print("handoff_session: tmux not found on PATH", file=sys.stderr)
            return 5
        print("handoff_session: note: tmux not found on PATH; a real launch would fail (rc 5)",
              file=sys.stderr)

    if cli == "codex":
        claude = resolve_codex(a.codex_bin)
        if not claude:
            print("handoff_session: could not resolve the `codex` binary (pass --codex-bin)", file=sys.stderr)
            return 4
    else:
        claude = resolve_claude(a.claude_bin)
        if not claude:
            print("handoff_session: could not resolve the `claude` binary (pass --claude-bin, or --cli codex "
                  "to launch Codex)", file=sys.stderr)
            return 4

    # A live session already owns this name -> refuse (never clobber / re-address).
    # Checked against BOTH the bus and tmux itself: a session that has not registered on the
    # bus yet is still a real name collision.
    if name in live_sessions() or tmux_has_session(name):
        print("handoff_session: a live session named %r already exists; pick another name "
              "or attach to it (tmux attach -t %s)" % (name, name), file=sys.stderr)
        return 3

    model = a.model
    # Lineage: tell the child who launched it. spawn_env_args() never raises and is []
    # outside tmux, so a launch never fails over the record of its own parentage.
    parent_args = session_lineage.spawn_env_args()
    if cli == "codex":
        tmux_cmd = build_codex_cmd(name, cwd, claude, bypass=a.bypass, prompt=prompt, model=model,
                                   parent_args=parent_args, extra_args=a.codex_arg)
    else:
        tmux_cmd = build_spawn_cmd(name, cwd, claude, bypass=a.bypass,
                                   prompt=prompt, model=model, parent_args=parent_args)
    label_cmd = build_label_cmd(name, a.display)

    if a.dry_run:
        print("[dry-run] would spawn: %s" % " ".join("%r" % c for c in tmux_cmd))
        if label_cmd:
            print("[dry-run] would run: %s" % " ".join("%r" % c for c in label_cmd))
        if prompt:
            print("[dry-run] AUTO-START: prompt is argv (%d chars); bus park SKIPPED" % len(prompt))
            print("[dry-run] " + spawn_note(a.no_build))
        if spec:
            print("[dry-run] would park spec (%d chars) on the bus for %r (bus_nudge leaves "
                  "the pane idle until you type): %s"
                  % (len(spec), name, park_cmd_text(name, "<spec>")))
        print("[dry-run] attach: tmux attach -t %s" % name)
        return 0

    r = _run(tmux_cmd)
    if r.returncode != 0:
        print("handoff_session: tmux new-session failed: %s" % (r.stderr or r.stdout).strip(), file=sys.stderr)
        return 6
    # The label is display-only and set after the session is up. Its failure is a
    # warning, never a spawn failure: the session is live and addressable by --name.
    if label_cmd:
        lr = _run(label_cmd)
        if lr.returncode != 0:
            print("handoff_session: warning: could not set the @display label: %s"
                  % (lr.stderr or lr.stdout).strip(), file=sys.stderr)
    # NAME THE MODEL in the confirmation. The failure this flag fixes was invisible spend:
    # large-model handoffs nobody noticed until the usage numbers moved.
    print("spawned session %r (%s, model: %s%s) in %s"
          % (name, cli,
             "inherited (session default)" if model is None else model,
             " --bypass" if a.bypass else "", cwd))

    # AUTO-START: the prompt rode the argv, so there is nothing to park and nothing to wait for.
    if prompt:
        # `tmux new-session -d` rc 0 only proves tmux forked the pane; it does NOT prove the
        # claude process survived argv parsing (or that a --claude-bin override exists). A pane
        # that died on startup would otherwise be reported as "working now".
        time.sleep(1.0)
        if not tmux_has_session(name):
            print("handoff_session: the pane died immediately after spawn: the %s process "
                  "did not survive startup. Check the binary (%s) and the prompt's first line "
                  "(a leading '-' is handled, but an unreadable --cwd or a bad --%s-bin is "
                  "not)." % (cli, claude, cli), file=sys.stderr)
            return 8
        print("AUTO-START: the session launched with its prompt (%d chars) and is working now."
              % len(prompt))
        print("attach: tmux attach -t %s   (from inside tmux: tmux switch-client -t %s)" % (name, name))
        print(spawn_note(a.no_build))
        return 0

    # Best-effort wait for the new session to register on the bus. The mail is file-durable,
    # so a send still lands even if registration lags; the wait just makes delivery prompt.
    registered = False
    deadline = time.monotonic() + max(0.0, a.timeout)
    while time.monotonic() < deadline:
        if name in live_sessions():
            registered = True
            break
        time.sleep(0.5)

    if spec:
        s = _run(build_park_cmd(name, spec))
        if s.returncode == 0:
            note = "" if registered else " (session not yet visible on the bus; spec is queued, " \
                                         "it delivers on the pane's next prompt)"
            print("parked spec on the bus for %r%s" % (name, note))
        else:
            print("handoff_session: spec send FAILED: %s" % (s.stderr or s.stdout).strip(), file=sys.stderr)
            print("  -> the session is up; retry (keep --staged, or the spec runs unprompted): %s"
                  % park_cmd_text(name, "<spec>"), file=sys.stderr)
            return 7
    elif not registered:
        print("note: session not yet visible on the bus after %.0fs (it should appear shortly)" % a.timeout)

    print("attach: tmux attach -t %s   (from inside tmux: tmux switch-client -t %s)" % (name, name))
    print("NOTE: this only STAGED the session + spec; nothing has run. Your prompt in that pane authorizes.")
    if spec:
        print("NOTE: the spec is delivered by the bus hook on that first prompt, and expires "
              "after 24h if it is not." + (
                  " A Codex pane: trust the hooks in /hooks before typing, or the spec does "
                  "not arrive." if cli == "codex" else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
