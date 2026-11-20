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
codex mode; scope ROOTS are shared, since they name folders, not memory.

Python 3.11+ ships tomllib; on older interpreters (macOS still ships 3.9) a
small parser below handles the subset the example config uses: tables,
arrays of tables, strings, integers, booleans and flat arrays. A config that
uses anything fancier fails loudly instead of being half-read.

    python3 notes_config.py            # print the resolved config as JSON
    python3 notes_config.py --stores [--cwd DIR]   # this folder's stores, as JSON
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
    # Each scope is a project folder with its own memory store. `store` may be
    # empty, meaning ~/.claude/projects/<encoded root>/memory.
    "scopes": [],
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
    if kind == "codex":
        notes = os.path.join(a["codex_home"], "session-notes")
        p["state_dir"] = os.path.join(notes, "state")
        p["global_store"] = os.path.join(notes, "global")
    elif not p["global_store"]:
        p["global_store"] = os.path.join(
            p["claude_home"], "projects", encode_project_dir("~"), "memory")
    p["global_store"] = _abs(p["global_store"], base)
    # The home of the running agent: skills/ and skill-feedback/ live here.
    p["agent_home"] = a["codex_home"] if kind == "codex" else p["claude_home"]
    scopes = []
    for i, s in enumerate(cfg["scopes"]):
        if not isinstance(s, dict) or not s.get("name") or not s.get("root"):
            raise ConfigError("scopes[%d] needs a name and a root" % i)
        unknown = set(s) - {"name", "root", "store"}
        if unknown:
            raise ConfigError("scopes[%d]: unknown key(s) %s" % (i, sorted(unknown)))
        root = _abs(s["root"], base)
        if kind == "codex":
            store = codex_project_store(root, a["codex_home"])
        else:
            store = s.get("store") or os.path.join(
                p["claude_home"], "projects", encode_project_dir(root), "memory")
        scopes.append({"name": s["name"], "root": root,
                       "store": _abs(store, base)})
    cfg["scopes"] = scopes
    if kind == "codex":
        # The no-piggyback rule as a mechanism: a codex_home inside claude_home
        # (or a symlink into it) would make "separate memories" false.
        for where in [p["state_dir"], p["global_store"]] + [s["store"] for s in scopes]:
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


def state_path(*parts, cfg=None):
    """A path under the state dir (created on demand by callers)."""
    cfg = cfg or load()
    return os.path.join(cfg["paths"]["state_dir"], *parts)


def selftest():
    import tempfile
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
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
            fh.write("[time]\nstale_after_minutes = -5\n")
        try:
            load(f)
            check("non-positive minutes refused", False)
        except ConfigError:
            check("non-positive minutes refused", True)
        check("missing file = defaults",
              load(os.path.join(d, "nope.toml"))["scopes"] == [])
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
        print(json.dumps(load(), indent=2))
    except ConfigError as exc:
        sys.exit("notes_config: %s" % exc)
