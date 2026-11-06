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

Python 3.11+ ships tomllib; on older interpreters (macOS still ships 3.9) a
small parser below handles the subset the example config uses: tables,
arrays of tables, strings, integers, booleans and flat arrays. A config that
uses anything fancier fails loudly instead of being half-read.

    python3 notes_config.py            # print the resolved config as JSON
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


DEFAULTS = {
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
        return json.loads(text)
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
        return _tomllib.loads(text)
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


def _resolve(cfg, base=None):
    p = cfg["paths"]
    for k in ("claude_home", "state_dir"):
        p[k] = _abs(p[k], base)
    if not p["global_store"]:
        p["global_store"] = os.path.join(
            p["claude_home"], "projects", encode_project_dir("~"), "memory")
    p["global_store"] = _abs(p["global_store"], base)
    scopes = []
    for i, s in enumerate(cfg["scopes"]):
        if not isinstance(s, dict) or not s.get("name") or not s.get("root"):
            raise ConfigError("scopes[%d] needs a name and a root" % i)
        unknown = set(s) - {"name", "root", "store"}
        if unknown:
            raise ConfigError("scopes[%d]: unknown key(s) %s" % (i, sorted(unknown)))
        root = _abs(s["root"], base)
        store = s.get("store") or os.path.join(
            p["claude_home"], "projects", encode_project_dir(root), "memory")
        scopes.append({"name": s["name"], "root": root,
                       "store": _abs(store, base)})
    cfg["scopes"] = scopes
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
        with open(path) as fh:
            try:
                data = parse_toml(fh.read())
            except ConfigError as exc:
                raise ConfigError("%s: %s" % (path, exc))
        _merge(cfg, data)
    return _resolve(cfg, base)


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
    check("encode", encode_project_dir("/a/b.c") == "-a-b-c")
    check("array parse", parse_toml('x = ["a", "b,c"]\n') == {"x": ["a", "b,c"]}
          if _tomllib is None else True)
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        print(json.dumps(load(), indent=2))
    except ConfigError as exc:
        sys.exit("notes_config: %s" % exc)
