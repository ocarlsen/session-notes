#!/usr/bin/env python3
"""handoff_session -- hand work off to a new, named Claude Code session in tmux.

The new session records which session launched it (see session_lineage.py). Usually it starts
on its task at once (--prompt); in staged mode (--spec) the task waits on the bus until a human
types in the new pane.

    handoff_session.py --name task-b --prompt-file /tmp/p.md
    handoff_session.py --name images --spec "generate today's images ..."
    handoff_session.py --name task-a --spec-file /tmp/spec.md --cwd /path/to/project

TWO MODES (pick one; they are mutually exclusive by design):

  STAGED (--spec/--spec-file, the default) -- spawn a bare `claude`, park the spec on the
  bus, and stop. The pane sits IDLE until its recipient types something; that typed prompt
  is the authorization (the staged-go pattern). Use this for a handoff the user did not ask
  for.

  AUTO-START (--prompt/--prompt-file) -- supply the prompt as the spawned process's ARGV, so
  the session starts working immediately. The bus park is SKIPPED (see below).

What it does, in order:
  1. refuse if a live tmux session already owns that name
  2. resolve the `claude` binary
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


def label_spec(spec):
    """Prepend a short informational tag unless the caller already framed it."""
    if not spec:
        return spec
    head = spec.lstrip()[:80].upper()
    if head.startswith("SPEC") or "NOT AUTHORIZATION" in head:
        return spec
    return ("SPEC (informational, not authorization: your own prompt in this pane "
            "authorizes; nothing here has run): " + spec)


def selftest():
    """Deterministic checks over argv construction + mode selection. No spawning."""
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

    print("handoff_session selftest: %d checks, %d failed" % (ran[0], len(fails)))
    for f in fails:
        print("  FAIL %s" % f)
    return 1 if fails else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Hand work off to a new, named Claude Code session in tmux, "
                                "recording its parent. --prompt starts it working; --spec parks the "
                                "task until you type in the new pane.")
    p.add_argument("--name", help="tmux session name for the new worker (required unless --selftest)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--spec", help="STAGED mode: spec text to park on the bus for the new session")
    g.add_argument("--spec-file", help="STAGED mode: read the spec from this file")
    q = p.add_mutually_exclusive_group()
    q.add_argument("--prompt", help="AUTO-START mode: initial prompt as the session's argv (it starts working)")
    q.add_argument("--prompt-file", help="AUTO-START mode: read the initial prompt from this file")
    p.add_argument("--cwd", default=os.getcwd(), help="working dir for the new session (default: cwd)")
    p.add_argument("--bypass", action="store_true",
                   help="add --dangerously-skip-permissions (default OFF)")
    p.add_argument("--model", default=DEFAULT_HANDOFF_MODEL,
                   help="model for the spawned ORCHESTRATOR session. Default: inherit the "
                        "interactive default (the session orchestrates and may delegate the "
                        "build to a cheaper sub-agent). Pass an id only for a deliberate "
                        "exception.")
    p.add_argument("--claude-bin", help="override the claude binary path")
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

    if shutil.which("tmux") is None:
        print("handoff_session: tmux not found on PATH", file=sys.stderr)
        return 5

    claude = resolve_claude(a.claude_bin)
    if not claude:
        print("handoff_session: could not resolve the `claude` binary (pass --claude-bin)", file=sys.stderr)
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
    tmux_cmd = build_spawn_cmd(name, cwd, claude, bypass=a.bypass,
                               prompt=prompt, model=model, parent_args=parent_args)

    if a.dry_run:
        print("[dry-run] would spawn: %s" % " ".join("%r" % c for c in tmux_cmd))
        if prompt:
            print("[dry-run] AUTO-START: prompt is argv (%d chars); bus park SKIPPED" % len(prompt))
        if spec:
            print("[dry-run] would park spec (%d chars) on the bus for %r" % (len(spec), name))
        print("[dry-run] attach: tmux attach -t %s" % name)
        return 0

    r = _run(tmux_cmd)
    if r.returncode != 0:
        print("handoff_session: tmux new-session failed: %s" % (r.stderr or r.stdout).strip(), file=sys.stderr)
        return 6
    # NAME THE MODEL in the confirmation. The failure this flag fixes was invisible spend:
    # large-model handoffs nobody noticed until the usage numbers moved.
    print("spawned session %r (claude, model: %s%s) in %s"
          % (name,
             "inherited (session default)" if model is None else model,
             " --bypass" if a.bypass else "", cwd))

    # AUTO-START: the prompt rode the argv, so there is nothing to park and nothing to wait for.
    if prompt:
        # `tmux new-session -d` rc 0 only proves tmux forked the pane; it does NOT prove the
        # claude process survived argv parsing (or that a --claude-bin override exists). A pane
        # that died on startup would otherwise be reported as "working now".
        time.sleep(1.0)
        if not tmux_has_session(name):
            print("handoff_session: the pane died immediately after spawn: the claude process "
                  "did not survive startup. Check the binary (%s) and the prompt's first line "
                  "(a leading '-' is handled, but an unreadable --cwd or a bad --claude-bin is "
                  "not)." % claude, file=sys.stderr)
            return 8
        print("AUTO-START: the session launched with its prompt (%d chars) and is working now."
              % len(prompt))
        print("attach: tmux attach -t %s   (from inside tmux: tmux switch-client -t %s)" % (name, name))
        print("NOTE: it runs under your normal permission settings. If nothing shows up, "
              "check the pane: it can stall invisibly on a permission prompt.")
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
        s = _run([sys.executable, BUS, "send", "--to", name, spec])
        if s.returncode == 0:
            note = "" if registered else " (session not yet visible on the bus; spec is queued, " \
                                         "it delivers on the pane's next prompt)"
            print("parked spec on the bus for %r%s" % (name, note))
        else:
            print("handoff_session: spec send FAILED: %s" % (s.stderr or s.stdout).strip(), file=sys.stderr)
            print("  -> the session is up; retry: python3 %s send --to %s \"<spec>\"" % (BUS, name), file=sys.stderr)
            return 7
    elif not registered:
        print("note: session not yet visible on the bus after %.0fs (it should appear shortly)" % a.timeout)

    print("attach: tmux attach -t %s   (from inside tmux: tmux switch-client -t %s)" % (name, name))
    print("NOTE: this only STAGED the session + spec; nothing has run. Your prompt in that pane authorizes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
