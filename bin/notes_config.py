#!/usr/bin/env python3
"""notes_config.py -- the one place session-notes reads its settings from.

Every path, scope list, budget and time zone the tools need comes from a
single TOML file, so no script carries a hardcoded home directory or project
list. Lookup order:

  1. $SESSION_NOTES_CONFIG, if set
  2. ~/.config/session-notes/config.toml
  3. built-in defaults (everything below DEFAULTS), which are enough to run
     the bus, the peers tool and the self-tests with no file at all

See config.example.toml at the repo root for every key with a comment.

TWO AGENTS, TWO MEMORIES. The same tools serve Claude Code and OpenAI Codex. Which
one a call is for comes from $SESSION_NOTES_AGENT ("claude" or "codex"), else
`agent.kind` in the config, else "claude". Codex sets the variable itself: install.sh
adds it to Codex's shell_environment_policy, and the Codex hooks set it inline. In
codex mode every store and the state dir live under `agent.codex_home`
(~/.codex/session-notes/...), and a resolved path under claude_home is refused, so
one agent never reads or writes the other's memory. `paths.global_store`,
`paths.state_dir` and a scope's `store` describe the Claude side and are ignored in
codex mode; scope ROOTS are shared, since they name folders, not memory. One
exception holds no memory: `paths.shared_state_dir`, the configured state_dir NOT
moved by codex mode, keeps tmux-wide state every agent must agree on (the bus
mailbox, bcast's log, pane state, the pane registry).

Python 3.11+ ships tomllib; on older interpreters (macOS still ships 3.9) a
small parser below handles the subset the example config uses: tables,
arrays of tables, strings, integers, booleans and flat arrays. A config that
uses anything fancier fails loudly instead of being half-read.

    python3 notes_config.py            # print the resolved config as JSON
    python3 notes_config.py --stores [--cwd DIR]   # this folder's stores, as JSON
    python3 notes_config.py --live-path           # the live-context file's path
    python3 notes_config.py --pin-state-dir [--dry-run]   # Codex-only machine: pin state_dir
    python3 notes_config.py --selftest
"""
import copy
import json
import os
import re
import sys

try:  # pragma: no cover - depends on interpreter
    import tomllib as _tomllib
except ImportError:  # Python < 3.11
    _tomllib = None


def encode_project_dir(path):
    """Claude Code's on-disk name for a project folder: the absolute path with
    every '/' and '.' replaced by '-'. ~/.claude/projects/<this>/ holds that
    folder's transcripts and, by convention, its memory/ store."""
    return re.sub(r"[/.]", "-", os.path.abspath(os.path.expanduser(path)))


AGENTS = ("claude", "codex")


DEFAULTS = {
    "agent": {
        # Which agent these tools serve: "claude" or "codex".
        # $SESSION_NOTES_AGENT overrides it per process.
        "kind": "claude",
        # Codex's own directory. In codex mode the memory stores and state
        # live under <codex_home>/session-notes/.
        "codex_home": "~/.codex",
        # Most characters the Codex SessionStart hook injects. Codex spills hook
        # output over its additionalContextLimit (install.sh sets 4000 tokens) to a
        # file with only a preview, so the hook caps itself below that and names
        # any file that does not fit instead of letting the middle vanish.
        "codex_boot_bytes": 12000,
    },
    "paths": {
        # Where Claude Code itself lives (transcripts under projects/).
        "claude_home": "~/.claude",
        # Where session-notes keeps its own runtime state: bus mailbox, locks,
        # audit logs, the pane registry. Never memory content.
        "state_dir": "~/.claude/session-notes/state",
        # The global memory store. Empty means "derive it": the projects/
        # folder Claude Code uses for your home directory, plus /memory.
        "global_store": "",
        # Where the shared per-scope stores live (scopes with `shared_store = true`): one
        # folder per scope slug, outside every agent's home. Data, not state.
        "shared_stores_dir": "~/.local/share/session-notes/stores",
        # Where nightly_check records its last run (read by `nightly_check.py status`).
        # Empty = <shared_state_dir>/last-check.json, the same file for every agent.
        "last_check": "",
    },
    "time": {
        # IANA zone for the clock anchor. Empty = the system's local zone.
        "timezone": "",
        # Minutes of idle after which time_sense says "your context is stale".
        "stale_after_minutes": 120,
    },
    "bcast": {
        # Attribution marker typed in front of broadcast prose.
        "prefix": "[user via bcast]",
    },
    "bus": {
        # tmux session name suggested when the bus_nudge loop is down. Only a
        # hint in the restart line; detection is by process.
        "nudge_session": "bus-nudge",
    },
    "handoff": {
        # A sentence handoff_session adds to the AUTO-START note for a session that
        # builds something, e.g. your own review routine. Empty = no sentence.
        # `--no-build` omits it.
        "build_note": "",
    },
    "rules": {
        # A private addendum to rules/core.md. rules_block.py appends it to the Codex
        # AGENTS.md block and adds an @import for it to CLAUDE.md. A missing file is
        # normal; an unreadable one is an error. Empty = no addendum.
        "local": "~/.config/session-notes/rules.local.md",
    },
    "budgets": {
        # Boot-prefix budgets, in bytes, checked by boot_budget.py.
        "global_index_bytes": 45000,
        "scope_index_bytes": 30000,
        "resume_bytes": 12000,
        "claude_md_bytes": 20000,
        # Ceiling for a CLAUDE.md found outside every configured scope.
        "unlisted_claude_md_bytes": 40000,
        # Folders walked (depth 3) for CLAUDE.md files outside any scope.
        "sweep_roots": ["~/Projects"],
    },
    # bin/wt.py: worktree + PR lifecycle for any git repo.
    "wt": {
        # Shell command whose stdout is the GitHub token wt exports as GH_TOKEN.
        "token_command": "gh auth token",
        # Global worktree root override; empty = the default location rule.
        "root": "",
        # Repo roots that are public: the pre-commit adds the private-term leak scan.
        "public_repos": [],
        # The pre-commit refuses a staged file above this many KB.
        "size_cap_kb": 1024,
        # Private terms file for public repos (format: tests/leak_scan.py docstring).
        "leak_terms": "~/.config/session-notes/leak-terms.txt",
    },
    # hooks/checkout_guard.py: refuse edits to tracked files on the default branch of a
    # main checkout, raw `gh pr merge`, and destructive git on a dirty tree.
    "checkout_guard": {
        # Repo roots where the guard does nothing (a repo with its own guards). Paths.
        "exempt_repos": [],
        # Extra glob patterns (fnmatch on the absolute real path, ~ expanded) that edits
        # may touch on the default branch. Each scope's memory store is always exempt.
        "exempt_paths": [],
    },
    # bin/environment_render.py: one private Markdown file describing your environment,
    # rendered into both agents' auto-approval reviewer settings.
    "environment": {
        # The source file. Empty = environment.md beside the config file
        # (~/.config/session-notes/environment.md by default).
        "source": "",
    },
    # bin/live.py: one machine-wide rolling file of what just happened across scopes.
    "live": {
        # Empty = ~/.local/state/session-notes/live-context.md. The same in both agent modes and
        # outside both agent homes: both agents write it, Codex must never touch ~/.claude.
        "path": "",
        # Entries kept, newest first.
        "max_lines": 30,
        # Most characters in one entry's text.
        "max_chars": 240,
    },
    # bin/test_edit_check.py and bin/review.py.
    "review": {
        # Extra fnmatch globs for files the test-edit check treats as tests/fixtures/guards.
        "test_globs": [],
    },
    # Each scope is a project folder with its own memory store. `store` may be
    # empty, meaning ~/.claude/projects/<encoded root>/memory. `shared_store = true` opts the
    # scope into ONE store for both agents under paths.shared_stores_dir (see store_for).
    "scopes": [],
    # Per-file boot budgets that win over the per-kind ones in [budgets]: one
    # [[budget_file]] block per file, with `path` and `bytes`.
    "budget_file": [],
}


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------- mini TOML
_KEY = r"[A-Za-z0-9_-]+"
_TABLE_RE = re.compile(r"^\[(%s)\]$" % _KEY)
_ARRAY_TABLE_RE = re.compile(r"^\[\[(%s)\]\]$" % _KEY)
_PAIR_RE = re.compile(r"^(%s)\s*=\s*(.+)$" % _KEY)


def _strip_comment(line):
    out, q = [], None
    for ch in line:
        if q:
            out.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out).strip()


def _scalar(text, lineno):
    text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        try:
            return json.loads(text)
        except ValueError:
            raise ConfigError("line %d: bad string %r" % (lineno, text)) from None
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items, buf, q = [], "", None
        for ch in inner:
            if q:
                buf += ch
                if ch == q:
                    q = None
            elif ch in "\"'":
                q = ch
                buf += ch
            elif ch == ",":
                items.append(buf)
                buf = ""
            else:
                buf += ch
        if buf.strip():
            items.append(buf)
        return [_scalar(i, lineno) for i in items]
    raise ConfigError("line %d: unsupported TOML value %r" % (lineno, text))


def parse_toml(text):
    """Parse the TOML subset described in the module docstring."""
    if _tomllib is not None:
        # One error type on every interpreter, so callers catch ConfigError only.
        try:
            return _tomllib.loads(text)
        except _tomllib.TOMLDecodeError as exc:
            raise ConfigError(str(exc)) from None
    root, cur = {}, None
    cur = root
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        m = _ARRAY_TABLE_RE.match(line)
        if m:
            cur = {}
            root.setdefault(m.group(1), []).append(cur)
            continue
        m = _TABLE_RE.match(line)
        if m:
            cur = root.setdefault(m.group(1), {})
            continue
        m = _PAIR_RE.match(line)
        if m:
            cur[m.group(1)] = _scalar(m.group(2), n)
            continue
        raise ConfigError("line %d: cannot parse %r" % (n, raw))
    return root


# ---------------------------------------------------------------- loading
def config_path(env=None):
    env = os.environ if env is None else env
    p = env.get("SESSION_NOTES_CONFIG")
    if p:
        return os.path.expanduser(p)
    return os.path.expanduser("~/.config/session-notes/config.toml")


def pin_state_dir(env=None, dry_run=False):
    """For a machine with no Claude home (a Codex-only install): write `state_dir` under
    $XDG_STATE_HOME (default ~/.local/state) into the config file, so the runtime state never
    lands in a ~/.claude that does not exist. Never overwrites an explicit state_dir, never
    runs on a machine that has a Claude home, and the default of an unpinned config stays
    ~/.claude/session-notes/state (no existence check at runtime). Returns a one-line report
    starting `pinned`, `would pin` or `unchanged`."""
    env = os.environ if env is None else env
    path = config_path(env)
    cfg = load(path, env) if os.path.exists(path) or not env.get("SESSION_NOTES_CONFIG") else None
    if cfg is None:  # SESSION_NOTES_CONFIG names a file that does not exist: create it
        cfg = load(None, {k: v for k, v in env.items() if k != "SESSION_NOTES_CONFIG"})
    home = cfg["paths"]["claude_home"]
    if os.path.lexists(home):
        return "unchanged: %s exists (a Claude Code machine keeps the default state_dir)" % home
    text = ""
    if os.path.islink(path) and not os.path.exists(path):
        raise ConfigError("%s: symlink to missing file %s" % (path, os.readlink(path)))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if "state_dir" in (parse_toml(text).get("paths") or {}):
            return "unchanged: %s already sets paths.state_dir" % path
    xdg = (env.get("XDG_STATE_HOME") or "").strip()
    if not xdg or not os.path.isabs(xdg):
        xdg = os.path.expanduser("~/.local/state")
    value = json.dumps(os.path.join(xdg, "session-notes", "state"))
    line = "state_dir = %s" % value
    lines = text.splitlines(True)
    for i, ln in enumerate(lines):
        if re.match(r"^\s*\[paths\]\s*(#.*)?$", ln):
            if not ln.endswith("\n"):
                lines[i] = ln + "\n"
            lines.insert(i + 1, line + "\n")
            new = "".join(lines)
            break
    else:
        new = text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") \
            + "[paths]\n" + line + "\n"
    parse_toml(new)  # never write a file the loader would refuse
    if dry_run:
        return "would pin %s in %s" % (line, path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if text:
        with open(path + ".bak-session-notes", "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(path, "w", encoding="utf-8") as fh:  # through a symlink, never replacing it
        fh.write(new)
    return "pinned %s in %s" % (line, path)


def _merge(base, over, where=""):
    for k, v in over.items():
        if k not in base:
            raise ConfigError("unknown config key %r" % (where + k))
        if isinstance(base[k], dict):
            if not isinstance(v, dict):
                raise ConfigError("%s must be a table" % (where + k))
            _merge(base[k], v, where + k + ".")
        else:
            if base[k] is not None and type(v) is not type(base[k]) and not (
                    isinstance(base[k], list) and isinstance(v, list)):
                raise ConfigError("%s: expected %s, got %r"
                                  % (where + k, type(base[k]).__name__, v))
            base[k] = v


def _abs(path, base):
    """~ expanded; a relative path is taken relative to the config file's
    directory, so a config can sit next to the store it describes."""
    path = os.path.expanduser(path)
    if base and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(base, path))
    return path


def _under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def codex_project_store(root, codex_home):
    """The Codex store for a project folder: same encoding as Claude's projects/."""
    return os.path.join(codex_home, "session-notes", "projects",
                        encode_project_dir(root))


def store_for(scope, agent, cfg):
    """THE resolver for a scope's project store, for `agent` ("claude" or "codex").

    A scope that opted in with `shared_store = true` has one store, the same for both agents:
    <paths.shared_stores_dir>/<slug>. Every other scope keeps its per-agent store: Claude's
    `store` key or ~/.claude/projects/<slug>/memory, Codex's <codex_home>/session-notes/
    projects/<slug>. `scope` is a dict with `root`, plus `shared_store` and (optionally, for a
    custom Claude store) `claude_store`; a resolved scope has no `claude_store` and so gets the
    default Claude store, which is why shared_store.py takes --claude-store. Every reader and writer of a
    project store goes through here (via the resolved scope's `store`)."""
    if agent not in AGENTS:
        raise ConfigError("store_for: agent must be one of %s, got %r" % ("/".join(AGENTS), agent))
    p = cfg["paths"]
    root = scope["root"]
    slug = encode_project_dir(root)
    if scope.get("shared_store"):
        return os.path.join(p["shared_stores_dir"], slug)
    if agent == "codex":
        return codex_project_store(root, cfg["agent"]["codex_home"])
    return scope.get("claude_store") or os.path.join(p["claude_home"], "projects", slug, "memory")


def _resolve(cfg, base=None, env=None):
    env = os.environ if env is None else env
    a = cfg["agent"]
    kind = (env.get("SESSION_NOTES_AGENT") or a["kind"]).strip()
    if kind not in AGENTS:
        raise ConfigError("agent kind must be one of %s, got %r (from %s)" % (
            "/".join(AGENTS), kind,
            "$SESSION_NOTES_AGENT" if env.get("SESSION_NOTES_AGENT") else "agent.kind"))
    a["kind"] = kind
    a["codex_home"] = _abs(a["codex_home"], base)
    if a["codex_boot_bytes"] <= 0:
        raise ConfigError("agent.codex_boot_bytes must be positive, got %r" % a["codex_boot_bytes"])
    p = cfg["paths"]
    for k in ("claude_home", "state_dir"):
        p[k] = _abs(p[k], base)
    # Tmux-wide state that every agent on the machine must agree on (the bus
    # mailbox, bcast's log, pane state, the pane registry): the configured
    # state_dir, NOT moved by codex mode. It holds no memory, so the
    # no-piggyback rule below does not apply to it.
    p["shared_state_dir"] = p["state_dir"]
    p["last_check"] = (_abs(p["last_check"], base) if p["last_check"].strip()
                       else os.path.join(p["shared_state_dir"], "last-check.json"))
    # A derived store (not named in the config) is created on the first save, so
    # "missing" is normal for it until then; a named one that is missing is a typo.
    p["global_store_derived"] = kind == "codex" or not p["global_store"]
    if kind == "codex":
        notes = os.path.join(a["codex_home"], "session-notes")
        p["state_dir"] = os.path.join(notes, "state")
        p["global_store"] = os.path.join(notes, "global")
    elif not p["global_store"]:
        p["global_store"] = os.path.join(
            p["claude_home"], "projects", encode_project_dir("~"), "memory")
    p["global_store"] = _abs(p["global_store"], base)
    if not isinstance(p["shared_stores_dir"], str) or not p["shared_stores_dir"].strip():
        raise ConfigError("paths.shared_stores_dir must be a non-empty path string, got %r"
                          % (p["shared_stores_dir"],))
    p["shared_stores_dir"] = _abs(p["shared_stores_dir"], base)
    # The home of the running agent: skills/ and skill-feedback/ live here.
    p["agent_home"] = a["codex_home"] if kind == "codex" else p["claude_home"]
    lv = cfg["live"]
    if not isinstance(lv["path"], str):
        raise ConfigError("live.path must be a path string, got %r" % (lv["path"],))
    if lv["path"].strip():
        lv["path"] = _abs(lv["path"], base)
    else:
        lv["path"] = os.path.join(os.path.expanduser("~/.local/state"),
                                  "session-notes", "live-context.md")
    for k in ("max_lines", "max_chars"):
        if type(lv[k]) is not int or lv[k] <= 0:
            raise ConfigError("live.%s must be a positive integer, got %r" % (k, lv[k]))
    scopes = []
    for i, s in enumerate(cfg["scopes"]):
        if not isinstance(s, dict) or not s.get("name") or not s.get("root"):
            raise ConfigError("scopes[%d] needs a name and a root" % i)
        unknown = set(s) - {"name", "root", "store", "manifest", "worktree_root", "live", "live_file",
                              "shared_store"}
        if unknown:
            raise ConfigError("scopes[%d]: unknown key(s) %s" % (i, sorted(unknown)))
        root = _abs(s["root"], base)
        shared = s.get("shared_store", False)
        if type(shared) is not bool:
            raise ConfigError("scopes[%d] (%s): shared_store must be true or false, got %r"
                              % (i, s["name"], shared))
        if shared and s.get("store"):
            raise ConfigError("scopes[%d] (%s): shared_store = true and store = %r are exclusive:"
                              " the shared store IS the scope's store" % (i, s["name"], s["store"]))
        # The resolved scope the rest of the code sees: `store` is this agent's store, from
        # the one resolver below.
        store = store_for({"root": root, "shared_store": shared,
                           "claude_store": _abs(s["store"], base) if s.get("store") else ""},
                          kind, cfg)
        manifest = s.get("manifest", "")
        if not isinstance(manifest, str):
            raise ConfigError("scopes[%d] (%s): manifest must be a path string" % (i, s["name"]))
        wtr = s.get("worktree_root", "")
        if not isinstance(wtr, str):
            raise ConfigError("scopes[%d] (%s): worktree_root must be a path string" % (i, s["name"]))
        live = s.get("live", "shared")
        if live not in ("shared", "own", "none"):
            raise ConfigError("scopes[%d] (%s): live must be \"shared\", \"own\" or \"none\", got %r"
                              % (i, s["name"], live))
        lf = s.get("live_file", "")
        if not isinstance(lf, str):
            raise ConfigError("scopes[%d] (%s): live_file must be a path string, got %r"
                              % (i, s["name"], lf))
        if live == "own" and not lf.strip():
            raise ConfigError("scopes[%d] (%s): live = \"own\" needs a non-empty live_file"
                              % (i, s["name"]))
        if live != "own" and lf.strip():
            raise ConfigError("scopes[%d] (%s): live_file = %r is only allowed with live = \"own\""
                              " (live is %r)" % (i, s["name"], lf, live))
        if shared and live != "shared":
            raise ConfigError("scopes[%d] (%s): shared_store = true is refused for an isolated "
                              "scope (live = %r): an isolated scope never shares memory across "
                              "agents" % (i, s["name"], live))
        # A relative manifest is relative to the scope ROOT, which both agents share.
        scopes.append({"name": s["name"], "root": root, "shared_store": shared,
                       "worktree_root": _abs(wtr, root) if wtr.strip() else "",
                       "store": _abs(store, base),
                       "live": live,
                       "live_file": _abs(lf, root) if lf.strip() else "",
                       "store_derived": shared or kind == "codex" or not s.get("store"),
                       "manifest": _abs(manifest, root) if manifest.strip() else ""})
    cfg["scopes"] = scopes
    seen = {}
    for s in scopes:
        if s["live"] != "own":
            continue
        real = os.path.realpath(s["live_file"])
        if real == os.path.realpath(lv["path"]):
            raise ConfigError("scopes (%s): live_file %s is the shared live-context file; "
                              "an own file must be a different file" % (s["name"], s["live_file"]))
        if real in seen:
            raise ConfigError("scopes (%s) and (%s) resolve live_file to the same file %s; "
                              "each own scope needs its own" % (seen[real], s["name"], real))
        seen[real] = s["name"]
    w = cfg["wt"]
    if type(w["size_cap_kb"]) is not int or w["size_cap_kb"] <= 0:
        raise ConfigError("wt.size_cap_kb must be a positive integer, got %r" % (w["size_cap_kb"],))
    if not all(isinstance(x, str) and x.strip() for x in w["public_repos"]):
        raise ConfigError("wt.public_repos must be a list of path strings")
    tg = cfg["review"]["test_globs"]
    if not isinstance(tg, list) or not all(isinstance(x, str) and x.strip() for x in tg):
        raise ConfigError("review.test_globs must be a list of glob strings, got %r" % (tg,))
    w["root"] = _abs(w["root"], base) if w["root"].strip() else ""
    w["public_repos"] = [_abs(x, base) for x in w["public_repos"]]
    w["leak_terms"] = _abs(w["leak_terms"], base)
    g = cfg["checkout_guard"]
    for k in ("exempt_repos", "exempt_paths"):
        if not isinstance(g[k], list) or not all(isinstance(x, str) and x.strip() for x in g[k]):
            raise ConfigError("checkout_guard.%s must be a list of non-empty strings, got %r"
                              % (k, g[k]))
    g["exempt_repos"] = [_abs(x, base) for x in g["exempt_repos"]]
    g["exempt_paths"] = [os.path.expanduser(x) for x in g["exempt_paths"]]
    src = cfg["environment"]["source"]
    if not isinstance(src, str):
        raise ConfigError("environment.source must be a path string, got %r" % (src,))
    cfg["environment"]["source"] = (
        _abs(src, base) if src.strip()
        else os.path.join(base or os.path.dirname(config_path(env)), "environment.md"))
    # Empty means no addendum, like handoff.build_note.
    local = cfg["rules"]["local"]
    cfg["rules"]["local"] = _abs(local, base) if local.strip() else ""
    files = []
    for i, b in enumerate(cfg["budget_file"]):
        if not isinstance(b, dict) or set(b) != {"path", "bytes"}:
            raise ConfigError("budget_file[%d] needs exactly `path` and `bytes`, got %r" % (i, b))
        if not isinstance(b["path"], str) or not b["path"].strip():
            raise ConfigError("budget_file[%d]: path must be a non-empty string" % i)
        if type(b["bytes"]) is not int or b["bytes"] <= 0:
            raise ConfigError("budget_file[%d] (%s): bytes must be a positive integer, got %r"
                              % (i, b["path"], b["bytes"]))
        files.append({"path": _abs(b["path"], base), "bytes": b["bytes"]})
    cfg["budget_file"] = files
    if kind == "codex":
        # The no-piggyback rule as a mechanism: a codex_home inside claude_home
        # (or a symlink into it) would make "separate memories" false.
        for where in ([p["state_dir"], p["global_store"], lv["path"]]
                      + [s["store"] for s in scopes]
                      + [s["live_file"] for s in scopes if s["live_file"]]):
            if _under(where, p["claude_home"]):
                raise ConfigError("codex mode resolved %s, which is under claude_home %s;"
                                  " point agent.codex_home elsewhere" % (where, p["claude_home"]))
    if cfg["time"]["stale_after_minutes"] <= 0:
        raise ConfigError("time.stale_after_minutes must be positive, got %r"
                          % cfg["time"]["stale_after_minutes"])
    return cfg


def load(path=None, env=None):
    """The resolved config dict. A missing file means defaults; a malformed
    one raises ConfigError naming the problem (never a silent fallback)."""
    cfg = copy.deepcopy(DEFAULTS)
    env = os.environ if env is None else env
    if path is None and env.get("SESSION_NOTES_CONFIG"):
        path = config_path(env)
        if not os.path.exists(path):
            # Naming a config explicitly and having it ignored is the silent
            # fallback this module refuses; only the default location may be absent.
            raise ConfigError("SESSION_NOTES_CONFIG=%s: no such file" % path)
    path = path or config_path(env)
    if os.path.islink(path) and not os.path.exists(path):
        # A config kept in another repo and linked here: a dangling link means that repo
        # moved or lost the file. Running on defaults would silently move the state dir.
        raise ConfigError("%s: symlink to missing file %s" % (path, os.readlink(path)))
    base = None
    if os.path.exists(path):
        base = os.path.dirname(os.path.abspath(path))
        with open(path, encoding="utf-8") as fh:
            try:
                data = parse_toml(fh.read())
            except (ConfigError, UnicodeDecodeError) as exc:
                raise ConfigError("%s: %s" % (path, exc)) from None
        _merge(cfg, data)
    return _resolve(cfg, base, env)


def _git_root(cwd):
    import subprocess
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def stores_for(cwd, cfg=None):
    """{"agent", "global", "scope", "root", "project"} for a working directory.

    The configured scope whose root holds cwd wins (deepest root). With none, codex
    mode falls back to the git root of cwd, keyed like a scope, so every repo gets
    its own Codex store with no config. Claude mode has no fallback: Claude Code's
    own per-project folder already keys on the launch folder. `project` is None when no
    project applies (a bare folder outside any scope or repo)."""
    cfg = cfg or load()
    cwd = os.path.realpath(os.path.expanduser(cwd))
    out = {"agent": cfg["agent"]["kind"], "global": cfg["paths"]["global_store"],
           "scope": None, "root": None, "project": None}
    best = None
    for s in cfg["scopes"]:
        if _under(cwd, s["root"]) and (best is None or len(s["root"]) > len(best["root"])):
            best = s
    if best:
        out.update(scope=best["name"], root=best["root"], project=best["store"])
    elif cfg["agent"]["kind"] == "codex":
        root = _git_root(cwd)
        if root and os.path.realpath(root) != os.path.realpath(os.path.expanduser("~")):
            out.update(root=root, project=codex_project_store(root, cfg["agent"]["codex_home"]))
    return out


def skip_in_codex(tool, why, cfg=None):
    """True (after one line on stdout) when this is a Claude-only check running in
    codex mode. The caller then exits 0: a planned skip, not a failure, so a model
    chaining it after a write does not read the write as failed."""
    try:
        kind = (cfg or load())["agent"]["kind"]
    except ConfigError:
        return False  # the caller's own load() reports the bad config
    if kind != "codex":
        return False
    print("%s: skipped in codex mode (%s)" % (tool, why))
    return True


def state_path(*parts, cfg=None):
    """A path under the state dir (created on demand by callers)."""
    cfg = cfg or load()
    return os.path.join(cfg["paths"]["state_dir"], *parts)


def shared_state_path(*parts, cfg=None):
    """A path under the tmux-wide state dir, the same for every agent (see
    paths.shared_state_dir): bus mailbox, bcast log, pane state, pane registry."""
    cfg = cfg or load()
    return os.path.join(cfg["paths"]["shared_state_dir"], *parts)


def selftest():
    import tempfile
    fails = []

    def check(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
        if not cond:
            fails.append(name)

    sample = '''
# comment
[paths]
state_dir = "/tmp/x"   # trailing comment
[time]
timezone = "Europe/Paris"
stale_after_minutes = 30
[[scopes]]
name = "alpha"
root = "/tmp/alpha"
[[scopes]]
name = 'beta'
root = "/tmp/beta"
store = "/tmp/beta-store"
'''
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "c.toml")
        with open(f, "w") as fh:
            fh.write(sample)
        cfg = load(f)
        check("table value", cfg["paths"]["state_dir"] == "/tmp/x")
        check("int value", cfg["time"]["stale_after_minutes"] == 30)
        check("two scopes", [s["name"] for s in cfg["scopes"]] == ["alpha", "beta"])
        check("derived store", cfg["scopes"][0]["store"].endswith(
            "-tmp-alpha/memory"))
        check("explicit store", cfg["scopes"][1]["store"] == "/tmp/beta-store")
        check("defaults kept", cfg["bcast"]["prefix"] == DEFAULTS["bcast"]["prefix"])
        check("last_check derives from shared_state_dir",
              cfg["paths"]["last_check"] == "/tmp/x/last-check.json")
        cx = load(f, env={"SESSION_NOTES_AGENT": "codex"})
        check("last_check stays on the shared state dir in codex mode",
              cx["paths"]["last_check"] == "/tmp/x/last-check.json")
        with open(f, "w") as fh:
            fh.write('[paths]\nlast_check = "sub/lc.json"\n')
        check("explicit last_check is resolved against the config dir",
              load(f)["paths"]["last_check"] == os.path.join(d, "sub", "lc.json"))
        with open(f, "w") as fh:
            fh.write(sample)

        with open(f, "w") as fh:
            fh.write("[paths]\nstat_dir = \"/x\"\n")
        try:
            load(f)
            check("typo key refused", False)
        except ConfigError as exc:
            check("typo key refused", "stat_dir" in str(exc))

        with open(f, "w") as fh:
            fh.write("[time]\nstale_after_minutes = \"soon\"\n")
        try:
            load(f)
            check("wrong type refused", False)
        except ConfigError:
            check("wrong type refused", True)

        with open(f, "w") as fh:
            fh.write('[paths]\nglobal_store = "store/global"\n')
        check("relative to config dir",
              load(f)["paths"]["global_store"] == os.path.join(os.path.realpath(d), "store/global")
              or load(f)["paths"]["global_store"] == os.path.join(d, "store/global"))
        try:
            load(env={"SESSION_NOTES_CONFIG": os.path.join(d, "typo.toml")})
            check("named missing config refused", False)
        except ConfigError as exc:
            check("named missing config refused", "typo.toml" in str(exc))
        with open(f, "w") as fh:
            fh.write('[[budget_file]]\npath = "~/x/CLAUDE.md"\nbytes = 8000\n'
                     '[[budget_file]]\npath = "rel/MEMORY.md"\nbytes = 58000\n')
        bf = load(f)["budget_file"]
        check("budget_file parsed", [b["bytes"] for b in bf] == [8000, 58000]
              and bf[0]["path"] == os.path.expanduser("~/x/CLAUDE.md")
              and bf[1]["path"].endswith(os.path.join("rel", "MEMORY.md")) and os.path.isabs(bf[1]["path"]))
        global _tomllib
        saved, _tomllib = _tomllib, None
        try:
            bf3 = load(f)["budget_file"]
        finally:
            _tomllib = saved
        check("budget_file parsed by the 3.9 fallback parser too", bf3 == bf)
        for bad in ('[[budget_file]]\npath = "a"\n',
                    '[[budget_file]]\npath = "a"\nbytes = 0\n',
                    '[[budget_file]]\npath = "a"\nbytes = "8000"\n',
                    '[[budget_file]]\npath = ""\nbytes = 10\n',
                    '[[budget_file]]\npath = "a"\nbytes = 10\nextra = 1\n'):
            with open(f, "w") as fh:
                fh.write(bad)
            try:
                load(f)
                check("bad budget_file %r refused" % bad, False)
            except ConfigError as exc:
                check("bad budget_file %r refused" % bad, "budget_file[0]" in str(exc))
        with open(f, "w") as fh:
            fh.write('[[scopes]]\nname = "m1"\nroot = "%s/m1"\nmanifest = "notes/scope.md"\n'
                     '[[scopes]]\nname = "m2"\nroot = "%s/m2"\nmanifest = "~/abs/scope.md"\n'
                     '[[scopes]]\nname = "m3"\nroot = "%s/m3"\n' % (d, d, d))
        ms = [s["manifest"] for s in load(f)["scopes"]]
        check("scope stores without `store` are derived",
              all(s["store_derived"] for s in load(f)["scopes"]))
        with open(f, "w") as fh:
            fh.write('[paths]\nglobal_store = "/x/g"\n[[scopes]]\nname = "n"\nroot = "/x/n"\n'
                     'store = "/x/s"\n')
        named = load(f)
        check("a named store is not derived (claude mode)",
              not named["paths"]["global_store_derived"] and not named["scopes"][0]["store_derived"])
        cx = load(f, env={"SESSION_NOTES_AGENT": "codex"})
        check("every codex store is derived", cx["paths"]["global_store_derived"]
              and cx["scopes"][0]["store_derived"])
        with open(f, "w") as fh:
            fh.write('[[scopes]]\nname = "m1"\nroot = "%s/m1"\nmanifest = "notes/scope.md"\n'
                     '[[scopes]]\nname = "m2"\nroot = "%s/m2"\nmanifest = "~/abs/scope.md"\n'
                     '[[scopes]]\nname = "m3"\nroot = "%s/m3"\n' % (d, d, d))
        check("manifest relative to the scope root, ~ expanded, absent = empty",
              ms == [os.path.join(d, "m1", "notes", "scope.md"),
                     os.path.expanduser("~/abs/scope.md"), ""])
        with open(f, "w") as fh:
            fh.write('[[scopes]]\nname = "m1"\nroot = "/x"\nmanifest = 3\n')
        try:
            load(f)
            check("non-string manifest refused", False)
        except ConfigError as exc:
            check("non-string manifest refused", "manifest" in str(exc))
        dangling = os.path.join(d, "dangling.toml")
        os.symlink(os.path.join(d, "gone.toml"), dangling)
        try:
            load(dangling)
            check("dangling config symlink refused", False)
        except ConfigError as exc:
            check("dangling config symlink refused", "gone.toml" in str(exc))
        with open(f, "w") as fh:
            fh.write("[time]\nstale_after_minutes = -5\n")
        try:
            load(f)
            check("non-positive minutes refused", False)
        except ConfigError:
            check("non-positive minutes refused", True)
        check("missing file = defaults",
              load(os.path.join(d, "nope.toml"))["scopes"] == [])
        # pin_state_dir (Codex-only install): pins only with no Claude home and no explicit value.
        with tempfile.TemporaryDirectory() as hd:
            saved_env = {k: os.environ.get(k) for k in ("HOME", "XDG_STATE_HOME")}
            try:
                os.environ["HOME"] = hd
                os.environ.pop("XDG_STATE_HOME", None)
                cp = os.path.join(hd, "cfg", "config.toml")
                penv = {"SESSION_NOTES_CONFIG": cp}
                want = os.path.join(hd, ".local", "state", "session-notes", "state")
                check("unpinned default is the old one, with no existence check",
                      load(os.path.join(hd, "none.toml"), env={})["paths"]["state_dir"]
                      == os.path.join(hd, ".claude", "session-notes", "state"))
                os.makedirs(os.path.dirname(cp))
                open(cp, "w").close()
                check("pin: dry run says what it would write and writes nothing",
                      pin_state_dir(penv, dry_run=True).startswith("would pin") and open(cp).read() == "")
                check("pin: no Claude home pins ~/.local/state", pin_state_dir(penv).startswith("pinned")
                      and load(cp, env=penv)["paths"]["state_dir"] == want)
                check("pin: a second run changes nothing", pin_state_dir(penv).startswith("unchanged"))
                # a process started after something creates ~/.claude/session-notes/state still gets the pin
                os.makedirs(os.path.join(hd, ".claude", "session-notes", "state"))
                check("pin: the pinned dir holds even once ~/.claude/session-notes/state exists",
                      load(cp, env=penv)["paths"]["state_dir"] == want)
                with open(cp, "w") as fh:
                    fh.write("# mine\n[paths]\nclaude_home = \"%s/nochome\"\n[time]\nstale_after_minutes = 5\n" % hd)
                check("pin: an existing [paths] table gets the line, other keys kept",
                      pin_state_dir(penv).startswith("pinned")
                      and load(cp, env=penv)["paths"]["state_dir"] == want
                      and load(cp, env=penv)["time"]["stale_after_minutes"] == 5
                      and open(cp).read().startswith("# mine\n[paths]\nstate_dir ="))
                with open(cp, "w") as fh:
                    fh.write('[paths]\nclaude_home = "%s/nochome"\nstate_dir = "%s/mine"\n' % (hd, hd))
                check("pin: an explicit state_dir is never overwritten",
                      pin_state_dir(penv).startswith("unchanged")
                      and load(cp, env=penv)["paths"]["state_dir"] == os.path.join(hd, "mine"))
                with open(cp, "w") as fh:
                    fh.write("")
                check("pin: a machine with a Claude home pins nothing",
                      pin_state_dir(penv).startswith("unchanged") and open(cp).read() == "")
            finally:
                for k, v in saved_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        # Codex mode: every path under codex_home, Claude-side overrides ignored.
        with open(f, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\nglobal_store = "%s/claude/g"\n'
                     '[agent]\ncodex_home = "%s/codex"\n'
                     '[[scopes]]\nname = "app"\nroot = "%s/app"\nstore = "%s/claude/s"\n'
                     % ((d,) * 5))
        cc = load(f, env={})
        check("claude mode is the default", cc["agent"]["kind"] == "claude"
              and cc["paths"]["global_store"] == os.path.join(d, "claude/g"))
        cx = load(f, env={"SESSION_NOTES_AGENT": "codex"})
        check("tmux-wide state is the same dir for both agents (a Codex pane pulls the "
              "mailbox a Claude loop writes)",
              cx["paths"]["shared_state_dir"] == cc["paths"]["shared_state_dir"]
              == cc["paths"]["state_dir"])
        check("shared_state_path joins under the shared dir",
              shared_state_path("session-bus", cfg=cx)
              == os.path.join(cc["paths"]["state_dir"], "session-bus"))
        cxs = [cx["paths"]["state_dir"], cx["paths"]["global_store"], cx["scopes"][0]["store"]]
        check("codex mode: all stores under codex_home",
              all(x.startswith(os.path.join(d, "codex", "session-notes")) for x in cxs))
        check("codex mode: agent_home is codex_home",
              cx["paths"]["agent_home"] == os.path.join(d, "codex"))
        os.makedirs(os.path.join(d, "app", "sub"))
        st = stores_for(os.path.join(d, "app", "sub"), cx)
        check("stores_for: configured scope wins", st["scope"] == "app"
              and st["project"] == cx["scopes"][0]["store"])
        import subprocess
        repo = os.path.join(d, "repo")
        os.makedirs(os.path.join(repo, "deep"))
        subprocess.run(["git", "init", "-q", repo], check=True)
        st = stores_for(os.path.join(repo, "deep"), cx)
        check("stores_for: codex falls back to the git root",
              st["project"] == codex_project_store(os.path.realpath(repo), cx["agent"]["codex_home"]))
        check("stores_for: claude has no git fallback",
              stores_for(repo, cc)["project"] is None)
        try:
            load(f, env={"SESSION_NOTES_AGENT": "gpt"})
            check("bad agent refused", False)
        except ConfigError as exc:
            check("bad agent refused", "SESSION_NOTES_AGENT" in str(exc))
        with open(f, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/claude"\n[agent]\ncodex_home = "%s/claude/cx"\n'
                     % (d, d))
        try:
            load(f, env={"SESSION_NOTES_AGENT": "codex"})
            check("codex store under claude_home refused", False)
        except ConfigError as exc:
            check("codex store under claude_home refused", "under claude_home" in str(exc))
        import contextlib
        import io
        with open(f, "w") as fh:
            fh.write("")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            os.environ["SESSION_NOTES_AGENT"] = "codex"
            try:
                skipped = skip_in_codex("t", "why", load(f))
            except ConfigError:
                skipped = None
            finally:
                os.environ.pop("SESSION_NOTES_AGENT", None)
        check("skip_in_codex in codex mode", skipped is True and "skipped in codex mode" in buf.getvalue())
        check("skip_in_codex in claude mode", skip_in_codex("t", "why", load(f, env={})) is False)
    # [wt] defaults and the scope worktree_root key.
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "w.toml")
        with open(f, "w") as fh:
            fh.write('[wt]\nsize_cap_kb = 7\npublic_repos = ["~/pub", "rel"]\nroot = "wts"\n'
                     '[[scopes]]\nname = "a"\nroot = "%s/a"\nworktree_root = "wt"\n'
                     '[[scopes]]\nname = "b"\nroot = "%s/b"\nworktree_root = "/abs/wt"\n'
                     '[[scopes]]\nname = "c"\nroot = "%s/c"\n' % (d, d, d))
        c = load(f)
        check("wt keys resolve", c["wt"]["size_cap_kb"] == 7 and c["wt"]["root"] == os.path.join(d, "wts")
              and c["wt"]["public_repos"] == [os.path.expanduser("~/pub"), os.path.join(d, "rel")]
              and c["wt"]["token_command"] == "gh auth token")
        check("scope worktree_root: relative, absolute, unset",
              [s["worktree_root"] for s in c["scopes"]] == [os.path.join(d, "a", "wt"), "/abs/wt", ""])
        with open(f, "w") as fh:
            fh.write("[wt]\nsize_cap_kb = 0\n")
        try:
            load(f)
            check("wt.size_cap_kb 0 refused", False)
        except ConfigError:
            check("wt.size_cap_kb 0 refused", True)
    # [checkout_guard] defaults and validation.
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "g.toml")
        with open(f, "w") as fh:
            fh.write("")
        c = load(f)
        check("checkout_guard defaults empty", c["checkout_guard"] == {
            "exempt_repos": [], "exempt_paths": []})
        with open(f, "w") as fh:
            fh.write('[checkout_guard]\nexempt_repos = ["~/r", "rel"]\nexempt_paths = ["~/x/*.md"]\n')
        c = load(f)["checkout_guard"]
        check("checkout_guard repos resolved, paths ~ expanded",
              c["exempt_repos"] == [os.path.expanduser("~/r"), os.path.join(d, "rel")]
              and c["exempt_paths"] == [os.path.expanduser("~/x/*.md")])
        for key, bad in (("exempt_repos", '["a", ""]'), ("exempt_paths", '[1]'),
                         ("exempt_repos", '"a"')):
            with open(f, "w") as fh:
                fh.write("[checkout_guard]\n%s = %s\n" % (key, bad))
            try:
                load(f)
                check("bad checkout_guard.%s %s refused" % (key, bad), False)
            except ConfigError as exc:
                check("bad checkout_guard.%s %s refused" % (key, bad),
                      "checkout_guard." + key in str(exc))
    # [live] default path: same in both agent modes, outside claude_home.
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "l.toml")
        with open(f, "w") as fh:
            fh.write("")
        home = os.path.expanduser("~")
        cc = load(f, env={})
        cx = load(f, env={"SESSION_NOTES_AGENT": "codex"})
        want = os.path.join(home, ".local", "state", "session-notes", "live-context.md")
        check("live.path default, claude and codex mode identical",
              cc["live"]["path"] == cx["live"]["path"] == want)
        check("live.path default is not under claude_home",
              not _under(cc["live"]["path"], cc["paths"]["claude_home"]))
        with open(f, "w") as fh:
            fh.write('[live]\npath = "%s/mine.md"\n' % d)
        check("explicit live.path wins",
              load(f, env={})["live"]["path"] == os.path.join(d, "mine.md"))
        with open(f, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/ch"\n[live]\npath = "%s/ch/x.md"\n' % (d, d))
        try:
            load(f, env={"SESSION_NOTES_AGENT": "codex"})
            check("codex mode refuses a live.path under claude_home", False)
        except ConfigError as exc:
            check("codex mode refuses a live.path under claude_home", "claude_home" in str(exc))
    # [review].test_globs: default empty, a list is kept, a non-list names the key.
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "r.toml")
        check("review.test_globs defaults empty",
              load(os.path.join(d, "none.toml"))["review"]["test_globs"] == [])
        with open(f, "w") as fh:
            fh.write('[review]\ntest_globs = ["data/*.json"]\n')
        check("review.test_globs kept", load(f)["review"]["test_globs"] == ["data/*.json"])
        with open(f, "w") as fh:
            fh.write('[review]\ntest_globs = "data/*.json"\n')
        try:
            load(f)
            check("review.test_globs non-list refused", False)
        except ConfigError as exc:
            check("review.test_globs non-list refused", "review.test_globs" in str(exc))
    # shared_store: default off, one resolver for both agents, refused where it would leak.
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "s.toml")
        with open(f, "w") as fh:
            fh.write('[paths]\nclaude_home = "%s/c"\nshared_stores_dir = "%s/st"\n[agent]\ncodex_home = "%s/x"\n'
                     '[[scopes]]\nname = "p"\nroot = "/r/p"\nshared_store = true\n'
                     '[[scopes]]\nname = "q"\nroot = "/r/q"\n' % (d, d, d))
        envs = {a: {"SESSION_NOTES_AGENT": a} for a in AGENTS}
        got = {a: {s["name"]: s for s in load(f, env=envs[a])["scopes"]} for a in AGENTS}
        check("shared_store: both agents resolve the one shared path",
              got["claude"]["p"]["store"] == got["codex"]["p"]["store"] == os.path.join(d, "st", "-r-p"))
        check("shared_store: a scope that did not opt in keeps its per-agent store",
              got["claude"]["q"]["store"] == os.path.join(d, "c", "projects", "-r-q", "memory")
              and got["codex"]["q"]["store"] == os.path.join(d, "x", "session-notes", "projects", "-r-q")
              and not got["claude"]["q"]["shared_store"])
        check("shared_store: default shared_stores_dir is under ~/.local/share",
              load(os.path.join(d, "none.toml"))["paths"]["shared_stores_dir"]
              == os.path.expanduser("~/.local/share/session-notes/stores"))
    # shared_store refusals name their reason (real newlines in the fixtures), with loading controls
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "r.toml")
        head = '[paths]\nshared_stores_dir = "%s/st"\n' % d
        for tag, body, want in (
                ("live = none", 'shared_store = true\nlive = "none"\n', "isolated scope"),
                ("live = own", 'shared_store = true\nlive = "own"\nlive_file = "%s/l.md"\n' % d, "isolated scope"),
                ("store set", 'shared_store = true\nstore = "%s/s"\n' % d, "exclusive"),
                ("a string", 'shared_store = "yes"\n', "must be true or false")):
            with open(f, "w") as fh:
                fh.write(head + '[[scopes]]\nname = "x"\nroot = "/r/x"\n' + body)
            try:
                load(f)
                got = "loaded"
            except ConfigError as exc:
                got = str(exc)
            check("shared_store refused with %s" % tag, want in got and "shared_store" in got
                  and "cannot parse" not in got, got)
        for tag, body in (("false + live none", 'shared_store = false\nlive = "none"\n'),
                          ("false + store", 'shared_store = false\nstore = "%s/s"\n' % d)):
            with open(f, "w") as fh:
                fh.write(head + '[[scopes]]\nname = "x"\nroot = "/r/x"\n' + body)
            check("control loads: shared_store %s" % tag, bool(load(f)["scopes"]))
    check("encode", encode_project_dir("/a/b.c") == "-a-b-c")
    check("array parse", parse_toml('x = ["a", "b,c"]\n') == {"x": ["a", "b,c"]}
          if _tomllib is None else True)
    # Malformed TOML is a ConfigError on every interpreter (tomllib or the mini parser).
    for bad in ('this is not toml\n', 'x = "a"b"\n', 'x = ["a"b", "c"]\n'):
        try:
            parse_toml(bad)
            got = "parsed"
        except ConfigError:
            got = "ConfigError"
        except Exception as exc:
            got = type(exc).__name__
        check("bad TOML %r is a ConfigError" % bad, got == "ConfigError")
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        if "--stores" in sys.argv:
            # The stores this session reads and writes, for the cwd (or --cwd DIR).
            i = sys.argv.index("--cwd") + 1 if "--cwd" in sys.argv else 0
            print(json.dumps(stores_for(sys.argv[i] if i else os.getcwd()), indent=2))
            sys.exit(0)
        if "--live-path" in sys.argv:
            print(load()["live"]["path"])
            sys.exit(0)
        if "--pin-state-dir" in sys.argv:
            print(pin_state_dir(dry_run="--dry-run" in sys.argv))
            sys.exit(0)
        print(json.dumps(load(), indent=2))
    except ConfigError as exc:
        sys.exit("notes_config: %s" % exc)
