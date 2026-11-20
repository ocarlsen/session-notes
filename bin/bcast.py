#!/usr/bin/env python3
"""bcast.py -- type one line into EVERY live tmux agent session (Claude Code, Codex) at once.

For a user who keeps N parallel Claude Code sessions as N tmux sessions. Some
inputs are addressed to all of them at once ("/refresh", "check the bus"),
and typing the same thing into eight panes by hand is the chore this removes.

WHY THIS IS A SEPARATE TOOL FROM session_peers.py
-------------------------------------------------
`session_peers.py` is deliberately READ-ONLY: its docstring forbids send-keys
because that tool exists so a MODEL can observe siblings, and a model that can
also drive them can manufacture instructions that look like the user's. That
guardrail stands. This tool is the other half, and the difference is the
actor: bcast is invoked BY THE USER, per invocation, and every delivered line
is attributed and logged. It imports session_peers for pane detection so the
"which pane is a Claude session" rule has exactly one home (that detection is
non-obvious: Claude Code sets its process title to its version string).

PROVENANCE, which is the whole reason this is careful
-----------------------------------------------------
Three mechanisms, because a keystroke in a pane is otherwise indistinguishable
from one the user typed there themselves:
  1. ATTRIBUTION. Prose is prefixed (cfg["bcast"]["prefix"], default
     "[user via bcast]") so the receiving session sees it arrived by broadcast
     and is not a line typed at that keyboard. A slash command CANNOT be
     prefixed (Claude Code parses "/foo" only at the start of the input), so
     those go verbatim and rely on 2 and 3.
  2. AUDIT. Every invocation appends to <state_dir>/bcast.jsonl: timestamp,
     sender, exact text, and the panes it hit. A delivered line is always
     traceable to a bcast.
  3. THE USER'S HANDS. Nothing here fires on a schedule or from a model turn.
     It is a command the user runs. A model may WRITE a bcast command for
     review; it does not get to run one on the user's behalf.

The sender's own pane is always excluded, otherwise a broadcast feeds itself.

USAGE
  bcast /refresh                 every other agent pane runs /refresh
  bcast "check the bus"          prose, prefixed with the attribution marker
  bcast --to 8,12 "g2g"          only those sessions (name, %pane-id, or tmux
                                 session name; same resolution as --peek)
  bcast --idle-only "..."        skip sessions mid-turn (default: hit all,
                                 where Claude Code queues the line for the
                                 end of the current turn)
  bcast -n "..."                 dry run: show targets + exact keystrokes
  bcast --list                   just show what the targets would be
  bcast --clear "..."            Ctrl-U each input box first (only matters
                                 together with --force now: a pane with a
                                 half-typed line is otherwise skipped outright)
  bcast --no-prefix "..."        send prose verbatim, no attribution marker
  bcast --force "..."            type even into a pane showing a menu, prompt
                                 or half-typed line (DANGEROUS: see below)

HONEST LIMITS
  - tmux-hosted agent sessions on THIS machine only, same as session_peers.
    Every other machine needs its own bcast run.
  - Codex panes are recognised through session_peers' harness table. A Codex
    pane qualifies only when that table's tests say it is idle with an EMPTY
    input box (dim placeholder after the › prompt); an approval menu, a
    running turn, typed text or any screen the table does not recognise is
    skipped. A pane of any harness the table does not know is never a
    target. --force bypasses these tests too. A bare slash command
    (`bcast /refresh`) goes to Claude panes only: Codex rejects an unknown
    one and leaves it typed in the box.
  - Delivery is a keystroke, not a protocol: nothing here can confirm the
    receiving session ACTED on the line; it confirms the keystrokes were
    sent.
  - Before typing, bcast peeks each target pane's visible screen ONCE and
    skips it unless that screen ends in Claude Code's EMPTY input box (a rule
    line, a bare prompt glyph or one with only a ghost suggestion, a rule
    line) -- see `input_box_empty()`. That is the same guard `bus_nudge.py`
    uses to decide when it is safe to type. It catches a menu ("❯ 1. Yes"),
    a permission prompt, and a half-typed line -- all of those are reported
    in the skip list with a reason, and `--clear` only ever reaches a pane
    via `--force` now, since a half-typed-line pane is skipped before
    `--clear` would run. `--force` bypasses the check entirely and can still
    land a keystroke in a menu or prompt; use it only after looking at every
    target pane. This is a single peek immediately before typing, not a
    lock: a menu that opens in the gap between that peek and the keystroke
    is a race this cannot close.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import notes_config  # noqa: E402
import session_peers   # noqa: E402  (owns the Claude-pane detection rule)

LOG_MAX = 500
# Pause between the literal text and the Enter that submits it. Claude Code's
# input box can drop a Return that arrives in the same event burst as the text.
ENTER_DELAY = 0.15


# Numeric tmux session names first in numeric order, then named sessions
# alphabetically. One home for the rule: session_peers.
session_sort_key = session_peers.session_sort_key


def input_box_empty(lines):
    """True when the visible screen ends in Claude Code's input box with
    nothing typed: a rule line, a bare prompt glyph (or one with only a
    ghost suggestion), a rule. A permission dialog or menu replaces that
    box, and its prompt rows carry text ("❯ 1. Yes"), so they never match.

    One source of truth: bus_nudge.py calls this (bcast.input_box_empty)
    rather than keeping its own copy; targets() below uses it the same way
    to decide whether a pane is safe to type into."""
    for i in range(len(lines) - 1, 0, -1):
        s = lines[i].replace("\xa0", " ").strip()
        if not s.startswith(session_peers.PROMPT):
            continue
        rest = s[len(session_peers.PROMPT):].strip()
        if rest not in ("", session_peers.GHOST):
            return False
        above = lines[i - 1].strip()
        below = lines[i + 1].strip() if i + 1 < len(lines) else ""
        return above.startswith("─") and below.startswith("─")
    return False


def prefix(cfg=None):
    """The attribution marker typed before broadcast prose."""
    cfg = cfg or notes_config.load()
    return cfg["bcast"]["prefix"]


def log_path(cfg=None):
    cfg = cfg or notes_config.load()
    return os.path.join(cfg["paths"]["shared_state_dir"], "bcast.jsonl")


def is_agent(p):
    """Any pane session_peers recognises as an agent. Falls back to is_claude
    for an older session_peers with no harness table."""
    return p.get("is_agent", p["is_claude"])


def screen_ready(p, peek, peek_raw):
    """(ok, reason). Claude: bcast's own empty ❯ box test. Any other harness:
    session_peers.ready_for_input on the raw -e screen (idle AND empty box),
    which returns False for a harness it has no tests for."""
    if p["is_claude"]:
        screen = peek(p)
        if screen is None:
            return False, "peek failed: screen unreadable"
        if not input_box_empty(screen):
            return False, "no empty input box: menu, prompt or typed text"
        return True, ""
    ready = getattr(session_peers, "ready_for_input", None)
    if ready is None:
        return False, "no screen test for this harness"
    raw = peek_raw(p)
    if raw is None:
        return False, "peek failed: screen unreadable"
    if not ready(p, raw):
        return False, "%s not ready: working, menu, prompt or typed text" % (p.get("harness") or "agent")
    return True, ""


def targets(panes, only=None, idle_only=False, force=False, peek=None, peek_raw=None):
    """Agent panes to broadcast to, minus self. Returns (targets, skipped)
    where skipped is a list of (pane, reason) for the report.

    Unless `force`, a pane only qualifies if its visible screen -- read with
    `peek` (defaults to session_peers.peek) right now, not cached -- ends in
    Claude Code's empty input box (see input_box_empty). A menu, a
    permission prompt, or a half-typed line all fail that check and land in
    skipped with a reason instead of getting a keystroke."""
    if peek is None:
        peek = session_peers.peek
    if peek_raw is None:
        peek_raw = getattr(session_peers, "peek_raw", lambda p: None)
    out, skipped = [], []
    wanted = None
    if only:
        wanted = set()
        for name in only:
            pane, candidates = session_peers.find_pane(panes, name)
            if pane is None:
                hint = ", ".join(p["name"] or p["tmux_session"] for p in candidates or [])
                sys.exit(
                    "bcast: --to %r matched %s"
                    % (name, ("nothing" if not candidates else "several: " + hint))
                )
            wanted.add(pane["pane_id"])

    for p in panes:
        if not is_agent(p):
            continue
        if p["is_self"]:
            skipped.append((p, "self"))
            continue
        if wanted is not None and p["pane_id"] not in wanted:
            continue
        if idle_only and p["state"] == "working":
            skipped.append((p, "working"))
            continue
        if not force:
            ok, why = screen_ready(p, peek, peek_raw)
            if not ok:
                skipped.append((p, why))
                continue
        out.append(p)
    out.sort(key=session_sort_key)
    skipped.sort(key=lambda t: session_sort_key(t[0]))
    return out, skipped


def slash_filter(picked, payload):
    """A slash command is a Claude Code command: split non-Claude panes off as
    skipped. Observed on a Codex 0.170.0 pane: Codex answers "Unrecognized
    command '/refresh'" and LEAVES the text in its input box, so the pane then
    reads as half-typed and every later nudge or broadcast skips it until
    someone clears it."""
    if not payload.startswith("/"):
        return picked, []
    keep = [p for p in picked if p["is_claude"]]
    drop = [(p, "slash command is Claude-only") for p in picked if not p["is_claude"]]
    return keep, drop


def compose(text, no_prefix=False, marker=None):
    """The exact string to type. A slash command must reach the input box
    verbatim or Claude Code will not parse it as a command, so it is never
    prefixed; prose is, unless suppressed. `marker` defaults to the
    configured prefix."""
    text = text.strip()
    if not text:
        sys.exit("bcast: nothing to send")
    if "\n" in text:
        sys.exit("bcast: refusing multi-line text (the newline would submit early)")
    # A slash COMMAND is "/name" as the first word; "/tmp/notes.txt please read" is prose
    # that merely starts with a path, and must still carry the marker.
    first = text.split(None, 1)[0]
    if re.match(r"^/[A-Za-z][A-Za-z0-9_:.-]*$", first) or no_prefix:
        return text
    return "%s %s" % (marker or prefix(), text)


def send(pane_id, payload, clear=False):
    """Type payload into pane_id and press Enter. Literal mode so no character
    is interpreted as a tmux key name."""
    if clear:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-u"], check=True)
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", "--", payload], check=True)
    time.sleep(ENTER_DELAY)
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], check=True)


def log(payload, sent, sender, path=None):
    """Append the audit row. Best effort: a logging failure must not make a
    delivered broadcast look undelivered, but it IS reported."""
    path = path or log_path()
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sender": sender,
        "payload": payload,
        "targets": [
            {"pane_id": p["pane_id"], "session": p["tmux_session"], "name": p["name"]}
            for p in sent
        ],
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines = []
        if os.path.exists(path):
            with open(path) as fh:
                lines = fh.read().splitlines()
        lines.append(json.dumps(row))
        with open(path, "w") as fh:
            fh.write("\n".join(lines[-LOG_MAX:]) + "\n")
    except OSError as exc:
        print("bcast: WARNING could not write audit log (%s)" % exc, file=sys.stderr)


# Session names are the user's /rename strings (often an emoji plus a few
# words), so the column has to be wide enough to tell two report sessions
# apart. Measured in COLUMNS, not bytes or characters: emoji render wide.
NAME_WIDTH = 32

# The tmux session name is usually a number but can be a string (a session
# named for its task), so the key column is padded rather than assumed.
SESSION_WIDTH = 8


def _cells(text):
    """Terminal columns `text` occupies. Session names carry emoji, which are
    east-asian-WIDE and render two columns wide, so a plain len()-based pad
    (what %-32s does) leaves the next column short by one per emoji, and the
    cwd column no longer lines up. Combining marks add no width."""
    n = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _pad(text, width):
    """Clip to `width` COLUMNS, then pad out to it. Clipping is by measured
    width too: a slice at 32 characters can already be 34 columns wide."""
    out = ""
    for ch in text:
        if _cells(out + ch) > width:
            break
        out += ch
    return out + " " * (width - _cells(out))


def describe(p, marker="->"):
    """One row: a marker (target vs skipped), `<session>:`, the raw
    pane_title (busy glyph and all), then the cwd, since that is what tells
    two identically-named sessions apart before you broadcast into them.

    When the pane carries a display label (the tmux session option @display,
    session_peers' `display` field), the label replaces the session name but the
    leading busy glyph is kept; if the session's messaging name differs from
    its tmux session name, " (msg: <name>)" is appended so it stays visible."""
    title = p["pane_title"] or p["name"] or "(unnamed)"
    display = p.get("display")
    if display:
        glyph = title[:1] if title[:1] and (title[:1] == session_peers.IDLE_GLYPH
                                            or session_peers._is_spinner(title[:1])) else ""
        title = ("%s %s" % (glyph, display)) if glyph else display
        if p["name"] != p["tmux_session"]:
            title += " (msg: %s)" % p["name"]
    return "  %-4s %-*s %s %s" % (
        marker,
        SESSION_WIDTH,
        p["tmux_session"] + ":",
        _pad(title, NAME_WIDTH),
        session_peers.short_path(p["cwd"]),
    )


def selftest():
    sep = session_peers._SEP

    def mk(title, pane_id, cmd="2.1.220"):
        fields = [title.split()[-1], "0", pane_id, title, "/tmp", "111", cmd, "opus"]
        # Pad to session_peers' CURRENT field count, so a field added there
        # does not break this fixture.
        fields += [""] * (len(session_peers._FIELDS) - len(fields))
        return sep.join(fields)

    panes = [
        session_peers.parse_pane_line(mk("✳ alpha", "%1")),
        session_peers.parse_pane_line(mk("⠁ beta", "%2")),
        session_peers.parse_pane_line(mk("✳ gamma", "%3")),
    ]
    for p in panes:
        p["is_self"] = p["pane_id"] == "%3"

    # force=True below: these assertions are about self-exclusion, idle_only
    # and sorting, not the input-box guard (which has its own fixtures).
    got, skipped = targets(panes, force=True)
    assert [p["pane_id"] for p in got] == ["%1", "%2"], got
    assert [(p["pane_id"], r) for p, r in skipped] == [("%3", "self")], skipped

    got, skipped = targets(panes, idle_only=True, force=True)
    assert [p["pane_id"] for p in got] == ["%1"], got
    assert sorted(r for _, r in skipped) == ["self", "working"], skipped

    # Numbers numerically, then names alphabetically.
    ordered = [
        session_peers.parse_pane_line(mk("✳ 13", "%11")),
        session_peers.parse_pane_line(mk("✳ delta", "%12")),
        session_peers.parse_pane_line(mk("✳ 9", "%13")),
        session_peers.parse_pane_line(mk("✳ abc", "%14")),
        session_peers.parse_pane_line(mk("✳ 0", "%15")),
        # An emoji-named session: the case a len()-based pad gets wrong.
        session_peers.parse_pane_line(mk("✳ 🎨 rpt", "%16")),
    ]
    for p in ordered:
        p["is_self"] = False
    got, _ = targets(ordered, force=True)
    assert [p["tmux_session"] for p in got] == [
        "0", "9", "13", "abc", "delta", "rpt",
    ], got

    # The cwd column must start at the same offset whether or not the session
    # name carries an emoji -- a len()-based pad is short by one per emoji.
    assert _cells("🎨 Brand") == 8, _cells("🎨 Brand")
    assert _cells("Brand") == 5
    assert _pad("🎨 x", 6) == "🎨 x  ", repr(_pad("🎨 x", 6))
    assert _cells(_pad("🎨 x", 6)) == 6
    assert _cells(_pad("🎨🎨🎨🎨", 7)) == 7, "odd width must clip, not overflow"
    rows = [describe(p) for p in ordered]
    assert len({_cells(r.split(" ~")[0]) for r in rows}) == 1, rows

    # A slash command must survive untouched or the receiver will not parse it.
    assert compose("/refresh") == "/refresh"
    assert compose("/rnr consolidate") == "/rnr consolidate"
    assert compose("/tmp/notes.txt is ready", marker="[m]") == "[m] /tmp/notes.txt is ready"
    assert compose("check the bus", marker="[m]") == "[m] check the bus"
    assert compose("check the bus") == "%s check the bus" % prefix()
    assert prefix() == notes_config.DEFAULTS["bcast"]["prefix"]
    assert "user" in prefix()
    assert compose("check the bus", no_prefix=True) == "check the bus"
    for bad, why in (("", "empty"), ("a\nb", "multiline")):
        try:
            compose(bad)
        except SystemExit:
            pass
        else:  # pragma: no cover
            raise AssertionError("compose accepted %s input" % why)

    # Config: prefix and audit-log location both come from notes_config.
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bcast_")
    try:
        conf = os.path.join(tmp, "c.toml")
        with open(conf, "w") as fh:
            fh.write('[paths]\nstate_dir = "%s"\n[bcast]\nprefix = "[via test]"\n'
                     % os.path.join(tmp, "st"))
        cfg = notes_config.load(conf)
        assert prefix(cfg) == "[via test]"
        assert log_path(cfg) == os.path.join(tmp, "st", "bcast.jsonl")
        log("x", ordered[:1], "%0", path=log_path(cfg))
        with open(log_path(cfg)) as fh:
            row = json.loads(fh.read().splitlines()[-1])
        assert row["payload"] == "x" and row["targets"][0]["pane_id"] == "%11", row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # input_box_empty / targets()'s guard against typing into a menu, a
    # permission prompt, or a half-typed line. These three screens are REAL
    # captures (via session_peers.peek) of live Claude Code panes -- an
    # AskUserQuestion menu, a Bash permission prompt, and an idle empty
    # input box -- trimmed to their tail and with any path/user scrubbed. A
    # hand-typed fixture would not prove peek's actual rendering is caught.
    menu_screen = [
        " ☐ Action ",
        "Commit this?",
        "❯ 1. Yes, commit",
        "     Proceed with the commit",
        "  2. No. Do nothing else.",
        "     Skip the commit",
        "  3. Type something.",
        "────────────────────────────────────────────────────────────────────────────────",
        "  4. Chat about this",
        "Enter to select · ↑/↓ to navigate · Esc to cancel",
    ]
    perm_screen = [
        "⏺ Bash(touch /tmp/x)",
        "  ⎿  Waiting…",
        "────────────────────────────────────────────────────────────────────────────────",
        " Bash command",
        "   touch /tmp/x",
        "   Create an empty file at /tmp/x",
        " Do you want to proceed?",
        " ❯ 1. Yes",
        "   2. Yes, and always allow access to /tmp from this project",
        "   3. No",
        " Esc to cancel · Tab to amend",
    ]
    empty_screen = [
        "⚠ /tmp/x/MEMORY.md is over the",
        "  40.0k-char limit (51.2k chars) · /memory to free up context",
        "────────────────────────────────────────────────────────────────────────────────",
        "❯ ",
        "────────────────────────────────────────────────────────────────────────────────",
        "  /tmp/x                            auto mode unavailable for this model",
        "  ⏸ manual mode on · ← for agents",
    ]
    assert not input_box_empty(menu_screen), "menu screen must be refused"
    assert not input_box_empty(perm_screen), "permission screen must be refused"
    assert input_box_empty(empty_screen), "real empty screen must be accepted (positive control)"

    guard_panes = [
        session_peers.parse_pane_line(mk("✳ menu", "%21")),
        session_peers.parse_pane_line(mk("✳ perm", "%22")),
        session_peers.parse_pane_line(mk("✳ empty", "%23")),
    ]
    for p in guard_panes:
        p["is_self"] = False
    screens = {"%21": menu_screen, "%22": perm_screen, "%23": empty_screen}
    fake_peek = lambda p: screens[p["pane_id"]]  # noqa: E731

    got, skipped = targets(guard_panes, peek=fake_peek)
    assert [p["pane_id"] for p in got] == ["%23"], got
    assert sorted((p["pane_id"], r.startswith("no empty input box")) for p, r in skipped) == [
        ("%21", True), ("%22", True),
    ], skipped

    got, skipped = targets(guard_panes, peek=lambda p: None)
    assert got == [] and {r for _, r in skipped} == {"peek failed: screen unreadable"}, skipped

    got, skipped = targets(guard_panes, peek=fake_peek, force=True)
    assert sorted(p["pane_id"] for p in got) == ["%21", "%22", "%23"], got
    assert skipped == [], skipped

    # Codex panes. Raw capture-pane -e rows from a live Codex CLI 0.170.0
    # scratch pane (the same captures session_peers' selftest uses). Needs a
    # session_peers with the harness table; skipped (and said so) without one.
    if hasattr(session_peers, "ready_for_input"):
        cx_empty = "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m"
        cx_typed = "\x1b[1m›\x1b[0m half a thought"
        cx_working = "• \x1b[1mWorking\x1b[0m \x1b[2m(6s • esc to interrupt)\x1b[0m"
        footer = "  \x1b[38;2;246;226;183mGPT-6-Luna default\x1b[39m · /tmp"
        cx_menu = ["  Would you like to run the following command?",
                   "  $ touch ~/.busprobe_approval_probe",
                   "\x1b[1m› 1. Yes, proceed (y)\x1b[0m",
                   "  2. Yes, and don't ask again (p)",
                   "  3. No, and tell Codex what to do differently (esc)",
                   "  Press enter to confirm or esc to cancel"]
        raws = {
            "%31": ["", cx_empty, "", footer],
            "%32": ["", cx_typed, "", footer],
            "%33": [cx_working, "", cx_empty, "", footer],
            "%34": cx_menu,
        }

        def mkcx(sess, pane_id):
            fields = [sess, "0", pane_id, "Show tmux | " + sess, "/tmp", "1", "codex", ""]
            fields += [""] * (len(session_peers._FIELDS) - len(fields))
            p = session_peers.parse_pane_line(sep.join(fields))
            p["is_self"] = False
            return p

        cx_panes = [mkcx("cxe", "%31"), mkcx("cxt", "%32"), mkcx("cxw", "%33"), mkcx("cxm", "%34")]
        assert all(p["harness"] == "codex" and not p["is_claude"] for p in cx_panes), cx_panes
        # Claude panes must never be judged by the Codex test, nor Codex by the Claude one.
        def no_peek(p):
            raise AssertionError("Claude peek used on a codex pane")
        got, skipped = targets(cx_panes, peek=no_peek, peek_raw=lambda p: raws[p["pane_id"]])
        assert [p["pane_id"] for p in got] == ["%31"], got
        assert sorted(p["pane_id"] for p, _ in skipped) == ["%32", "%33", "%34"], skipped
        assert all(r.startswith("codex not ready") for _, r in skipped), skipped
        got, skipped = targets(cx_panes, peek=no_peek, peek_raw=lambda p: None)
        assert got == [] and {r for _, r in skipped} == {"peek failed: screen unreadable"}, skipped
        # --to resolves a Codex pane by tmux session name (was "matched nothing").
        got, _ = targets(cx_panes + guard_panes, only=["cxe"], peek=fake_peek,
                         peek_raw=lambda p: raws[p["pane_id"]])
        assert [p["pane_id"] for p in got] == ["%31"], got
        # Mixed fleet: each pane judged by its own harness's test.
        got, _ = targets(cx_panes + guard_panes, peek=fake_peek,
                         peek_raw=lambda p: raws[p["pane_id"]])
        assert sorted(p["pane_id"] for p in got) == ["%23", "%31"], got
        # A plain shell pane is never a target, even with --force.
        sh = session_peers.parse_pane_line(sep.join(
            ["sh", "0", "%35", "zsh", "/tmp", "1", "zsh", ""]
            + [""] * (len(session_peers._FIELDS) - 8)))
        sh["is_self"] = False
        got, _ = targets([sh], force=True, peek=fake_peek, peek_raw=lambda p: [])
        assert got == [], got
        # A slash command never goes to a Codex pane; prose does.
        mixed = cx_panes[:1] + guard_panes[2:]
        keep, drop = slash_filter(mixed, "/refresh")
        assert [p["pane_id"] for p in keep] == ["%23"], keep
        assert [(p["pane_id"], r) for p, r in drop] == [("%31", "slash command is Claude-only")], drop
        keep, drop = slash_filter(mixed, prefix() + " check the bus")
        assert len(keep) == 2 and drop == [], (keep, drop)
    else:
        print("bcast selftest: SKIPPED Codex cases (session_peers has no harness table)")

    # A display label replaces the name in describe(), keeping the busy glyph.
    lab = session_peers.parse_pane_line(mk("⠁ worker", "%40"))
    lab["display"] = "range parsing"
    lab["tmux_session"] = "7"  # messaging name "worker" now differs from it
    row = describe(lab)
    assert "⠁ range parsing" in row and "(msg: worker)" in row, row
    plain = session_peers.parse_pane_line(mk("✳ worker", "%41"))
    assert "✳ worker" in describe(plain) and "msg:" not in describe(plain), describe(plain)

    print("bcast selftest: OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("text", nargs="*", help="the line to type into each session")
    ap.add_argument("--to", help="comma-separated session names / %%pane-ids")
    ap.add_argument("--idle-only", action="store_true", help="skip sessions mid-turn")
    ap.add_argument("-n", "--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="show targets and exit")
    ap.add_argument(
        "--clear", action="store_true",
        help="Ctrl-U each input first (only matters with --force: a pane "
             "with a half-typed line is otherwise skipped before --clear "
             "would ever run)",
    )
    ap.add_argument("--no-prefix", action="store_true")
    ap.add_argument(
        "--force", action="store_true",
        help="type even into a pane showing a menu, prompt or half-typed "
             "line (bypasses the empty-input-box check entirely)",
    )
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.clear and not args.force:
        ap.error("--clear needs --force (a pane with a half-typed line is skipped, not cleared)")

    panes = session_peers.list_peers()
    if panes is None:
        sys.exit("bcast: no tmux server (nothing to broadcast to)")

    only = [t.strip() for t in args.to.split(",")] if args.to else None
    picked, skipped = targets(panes, only=only, idle_only=args.idle_only, force=args.force)

    if args.list:
        for p in picked:
            print(describe(p))
        for p, why in skipped:
            print(describe(p, "skip"), "(%s)" % why)
        return

    if not args.text:
        ap.error("nothing to send (pass the text, or --list)")
    payload = compose(" ".join(args.text), no_prefix=args.no_prefix)
    picked, dropped = slash_filter(picked, payload)
    skipped = skipped + dropped

    if not picked:
        print("bcast: no target sessions")
        for p, why in skipped:
            if why != "self":
                print(describe(p, "skip"), "(%s)" % why)
        return

    verb = "Would send to" if args.dry_run else "Sending to"
    print("%s %d session(s): %r" % (verb, len(picked), payload))
    for p in picked:
        print(describe(p))
    for p, why in skipped:
        if why != "self":
            print(describe(p, "skip"), "(%s)" % why)
    if args.dry_run:
        return

    sent, failed = [], []
    for p in picked:
        try:
            send(p["pane_id"], payload, clear=args.clear)
            sent.append(p)
        except (subprocess.CalledProcessError, OSError) as exc:
            failed.append((p, exc))

    log(payload, sent, os.environ.get("TMUX_PANE", "?"))
    print("bcast: delivered to %d/%d" % (len(sent), len(picked)))
    for p, exc in failed:
        print("%s -- FAILED: %s" % (describe(p, "FAIL"), exc), file=sys.stderr)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
