#!/usr/bin/env python3
"""environment_render.py -- one private Markdown file describing your environment, rendered
into both agents' auto-approval reviewers.

The source (config key environment.source; default environment.md beside the config file;
start from templates/environment.md) is plain Markdown. After HTML comments are removed,
every non-blank line is ONE entry, verbatim and in order (headings included); lines that
still hold the template placeholder, and headings with nothing under them, are dropped.

    Claude Code: <claude_home>/settings.json  autoMode.environment  = the list of entries
    Codex:       <codex_home>/config.toml     a marked block: sandbox_mode,
                 approval_policy, approvals_reviewer and auto_review.policy, where the
                 policy is the pinned bundled Codex policy (templates/) with the entries
                 inserted at the end of its Environment Profile section; plus
                 <codex_home>/rules/default.rules: a marked block allowing `session_bus.py send`.

    environment_render.py render [--target claude|codex] [--diff]
    environment_render.py diff [--target claude|codex|all]    silent rc 0 when equal, rc 1 + diff
    environment_render.py apply [--target ...] [--yes]        confirm, back up, atomic write
    environment_render.py check                               drift check (nightly_check runs it)
    environment_render.py upstream [--codex-version X]        did Codex's bundled policy change?
    environment_render.py selftest

Exit codes: 0 ok, 1 differs / drift found, 2 error (missing source, malformed file, refusal).
`check` exits 3 on drift (nightly_check's finding code).
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import notes_config  # noqa: E402

REPO = os.path.dirname(BIN)
POLICY_PATH = os.path.join(REPO, "templates", "codex-auto-review-policy.md")
TEMPLATE_PATH = os.path.join(REPO, "templates", "environment.md")
# The vendored policy is codex-rs/prompts/templates/guardian/policy.md at this tag.
POLICY_TAG = "rust-v0.171.0"
POLICY_SHA256 = "e6b0cf0a2e1c4cabc0a37ac2a0bc424ddd7c89e85d049e32d281a8db6e8d3ce6"
UPSTREAM_URL = ("https://raw.githubusercontent.com/openai/codex/%s/"
                "codex-rs/prompts/templates/guardian/policy.md")
PLACEHOLDER = "<fill in>"
ENV_HEADING = "## Environment Profile"

# Codex settings the block writes (documented in docs/codex.md).
CODEX_SETTINGS = [("sandbox_mode", "workspace-write"),
                  ("approval_policy", "on-request"),
                  ("approvals_reviewer", "auto_review")]
CODEX_BEGIN = "# >>> session-notes environment (bin/environment_render.py; edits inside are overwritten) >>>"
CODEX_END = "# <<< session-notes environment <<<"
RULES_BEGIN = ("# >>> session-notes environment rules (bin/environment_render.py; "
               "edits inside are overwritten) >>>")
RULES_END = "# <<< session-notes environment rules <<<"
BAK = ".bak-environment-"

# Seams for the selftest: no network, no codex binary.
FETCH = None          # fn(url) -> str; None = urllib
VERSION_FN = None     # fn() -> "0.171.0"; None = run `codex --version`
INPUT = input


class EnvError(Exception):
    """A problem the user must fix; the message names it."""


def emit(s=""):
    sys.stdout.write(s + ("\n" if not s.endswith("\n") else ""))


# ------------------------------------------------------------------ context
class Ctx(object):
    def __init__(self, source, claude_home, codex_home, bin_dirs=None):
        self.source, self.claude_home, self.codex_home = source, claude_home, codex_home
        self.bin_dirs = bin_dirs
        self.policy_path = POLICY_PATH
        self.policy_sha = POLICY_SHA256

    @property
    def settings(self):
        return os.path.join(self.claude_home, "settings.json")

    @property
    def config_toml(self):
        return os.path.join(self.codex_home, "config.toml")

    @property
    def rules(self):
        return os.path.join(self.codex_home, "rules", "default.rules")

    def send_bin_dirs(self):
        """Directories holding the session_bus.py that bus-send rules must name: this
        repo's bin, plus <claude_home>/bin when it resolves to the same file."""
        if self.bin_dirs is not None:
            return list(self.bin_dirs)
        out = [BIN]
        mine = os.path.realpath(os.path.join(BIN, "session_bus.py"))
        other = os.path.join(self.claude_home, "bin")
        cand = os.path.join(other, "session_bus.py")
        if os.path.exists(cand) and os.path.realpath(cand) == mine and other not in out:
            out.append(other)
        return out


def context(env=None):
    try:
        cfg = notes_config.load(env=env)
    except notes_config.ConfigError as exc:
        raise EnvError("bad config: %s" % exc)
    return Ctx(cfg["environment"]["source"], cfg["paths"]["claude_home"],
               cfg["agent"]["codex_home"])


# ---------------------------------------------------------------- the source
def parse_entries(text):
    """The entry lines of an environment file. See the module docstring."""
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if "<!--" in stripped or "-->" in stripped:
        raise EnvError("the environment file has an unterminated or stray HTML comment marker")
    lines = [ln.rstrip() for ln in stripped.split("\n")]
    lines = [ln for ln in lines if ln.strip() and PLACEHOLDER not in ln]
    kept = []
    for i, ln in enumerate(lines):
        if re.match(r"#{1,6}\s", ln):
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            if nxt is None or re.match(r"#{1,6}\s", nxt):
                continue
        kept.append(ln)
    return kept


def load_entries(ctx):
    if not os.path.exists(ctx.source):
        raise EnvError("environment source %s does not exist; copy %s there and fill it in"
                       % (ctx.source, TEMPLATE_PATH))
    try:
        with open(ctx.source, encoding="utf-8") as fh:
            return parse_entries(fh.read())
    except (OSError, UnicodeDecodeError) as exc:
        raise EnvError("cannot read %s (%s)" % (ctx.source, exc))


# ------------------------------------------------------------- codex policy
def load_policy(path=POLICY_PATH, sha=POLICY_SHA256):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise EnvError("cannot read the vendored Codex policy %s (%s)" % (path, exc))
    got = hashlib.sha256(raw).hexdigest()
    if got != sha:
        raise EnvError("%s has sha256 %s but the pin (%s) is %s; the vendored policy must stay "
                       "byte-identical (see docs/codex.md, Updating the pin)"
                       % (path, got, POLICY_TAG, sha))
    return raw.decode("utf-8")


def insert_environment(policy, entries):
    """The policy with `entries` joined by newlines at the END of the Environment Profile
    section, one blank line on each side. Every other byte is unchanged."""
    if not entries:
        return ""
    m = re.search(r"^" + re.escape(ENV_HEADING) + r"[ \t]*$", policy, flags=re.M)
    if not m:
        raise EnvError("the vendored policy has no %r section" % ENV_HEADING)
    nxt = re.search(r"^## ", policy[m.end():], flags=re.M)
    end = m.end() + nxt.start() if nxt else len(policy)
    body = "\n".join(entries)
    head = policy[:end].rstrip("\n") + "\n\n" + body + "\n"
    return head + ("\n" + policy[end:] if nxt else "")


# ------------------------------------------------------------- TOML strings
def toml_ml_encode(s):
    """A TOML multi-line basic string for s: backslashes and control characters escaped,
    every quote in a run of three or more (and at the very end) escaped."""
    out = []
    esc = {"\\": "\\\\", "\b": "\\b", "\f": "\\f", "\r": "\\r"}
    for ch in s:
        if ch in esc:
            out.append(esc[ch])
        elif ch in "\n\t":
            out.append(ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    t = "".join(out)
    t = re.sub(r'"{3,}|"+\Z', lambda m: '\\"' * len(m.group(0)), t)
    return '"""\n' + t + '"""'


def toml_ml_decode(text, start):
    """Decode the multi-line basic string whose opening triple quote is at text[start:].
    Returns (value, index after the closing quotes)."""
    if not text.startswith('"""', start):
        raise ValueError("no opening triple quote")
    i = start + 3
    if text.startswith("\r\n", i):
        i += 2
    elif text.startswith("\n", i):
        i += 1
    simple = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
    out = []
    while i < len(text):
        if text.startswith('"""', i):
            return "".join(out), i + 3
        c = text[i]
        if c == "\\":
            n = text[i + 1:i + 2]
            if n in simple:
                out.append(simple[n])
                i += 2
            elif n in ("u", "U"):
                k = 4 if n == "u" else 8
                hx = text[i + 2:i + 2 + k]
                if len(hx) != k or not re.match(r"^[0-9a-fA-F]+$", hx):
                    raise ValueError("bad unicode escape")
                out.append(chr(int(hx, 16)))
                i += 2 + k
            else:
                raise ValueError("unsupported escape \\%s" % n)
        else:
            out.append(c)
            i += 1
    raise ValueError("unterminated string")


# ----------------------------------------------------------- marked blocks
def split_block(lines, begin, end, where):
    """(before, block_inner or None, after) around the one marked block in lines."""
    bs = [i for i, ln in enumerate(lines) if ln.rstrip() == begin]
    es = [i for i, ln in enumerate(lines) if ln.rstrip() == end]
    if not bs and not es:
        return lines, None, []
    if len(bs) != 1 or len(es) != 1 or es[0] < bs[0]:
        raise EnvError("%s has a broken session-notes environment block (begin markers %d, end "
                       "markers %d); fix or remove it by hand" % (where, len(bs), len(es)))
    return lines[:bs[0]], lines[bs[0] + 1:es[0]], lines[es[0] + 1:]


_HEADER_RE = re.compile(r"^\s*\[\[?\s*[A-Za-z0-9_.\"' /:@-]+\s*\]\]?\s*(#.*)?$")
_KEY_RE = re.compile(r"^\s*(sandbox_mode|approval_policy|approvals_reviewer)\s*=")
_AR_KEY_RE = re.compile(r"^\s*auto_review\s*[.=]")
_AR_TABLE_RE = re.compile(r"^\s*\[\[?\s*auto_review\s*[\].]")


def first_header(lines):
    for i, ln in enumerate(lines):
        if _HEADER_RE.match(ln):
            return i
    return len(lines)


def codex_block_lines(policy):
    out = [CODEX_BEGIN]
    out += ['%s = "%s"' % kv for kv in CODEX_SETTINGS]
    out += ("auto_review.policy = " + toml_ml_encode(policy)).split("\n")
    out.append(CODEX_END)
    return out


def place_codex_block(text, policy, where):
    """config.toml text with the block set to `policy`. Refuses on a conflicting setting."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    before, inner, after = split_block(lines, CODEX_BEGIN, CODEX_END, where)
    block_len = 0 if inner is None else len(inner) + 2
    outside = before + after
    h = first_header(outside)

    def orig_no(k):
        return k + 1 + (block_len if inner is not None and k >= len(before) else 0)
    for k, ln in enumerate(outside):
        if k < h and (_KEY_RE.match(ln) or _AR_KEY_RE.match(ln)):
            raise EnvError("%s line %d sets %r outside the session-notes block; resolve it by "
                           "hand (remove it, or move it out of the way) and apply again"
                           % (where, orig_no(k), ln.strip()))
        if _AR_TABLE_RE.match(ln):
            raise EnvError("%s line %d has an [auto_review] table outside the session-notes "
                           "block; resolve it by hand and apply again" % (where, orig_no(k)))
    new = codex_block_lines(policy)
    if inner is not None and h >= len(before):
        res = before + new + after                      # replace in place
    else:
        rest = before + after
        p = h
        if p >= len(rest):                              # no table header: append
            sep = [""] if rest and rest[-1].strip() else []
            res = rest + sep + new
        else:
            while p > 0 and rest[p - 1].lstrip().startswith("#"):
                p -= 1
            pre = rest[:p]
            sep = [""] if pre and pre[-1].strip() else []
            res = pre + sep + new + [""] + rest[p:]
    return "\n".join(res) + "\n"


def read_codex_block(text, where):
    """(settings dict, policy or None, raw inner lines) from the block, or None."""
    lines = text.split("\n")
    _, inner, _ = split_block(lines, CODEX_BEGIN, CODEX_END, where)
    if inner is None:
        return None
    settings = {}
    for ln in inner:
        m = re.match(r'^(sandbox_mode|approval_policy|approvals_reviewer) = "(.*)"$', ln)
        if m:
            settings[m.group(1)] = m.group(2)
    body = "\n".join(inner)
    k = body.find('auto_review.policy = """')
    policy = None
    if k >= 0:
        try:
            policy, _ = toml_ml_decode(body, k + len("auto_review.policy = "))
        except ValueError:
            policy = None
    return settings, policy, inner


def rules_inner(ctx):
    return ["prefix_rule(pattern=[\"python3\", %s, \"send\"], decision=\"allow\")"
            % json.dumps(os.path.join(d, "session_bus.py")) for d in ctx.send_bin_dirs()]


def place_rules_block(text, inner, where):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    before, old, after = split_block(lines, RULES_BEGIN, RULES_END, where)
    new = [RULES_BEGIN] + inner + [RULES_END]
    if old is not None:
        res = before + new + after
    else:
        sep = [""] if lines and lines[-1].strip() else []
        res = lines + sep + new
    return "\n".join(res) + "\n"


def read_rules_block(text, where):
    _, inner, _ = split_block(text.split("\n"), RULES_BEGIN, RULES_END, where)
    return inner


# ------------------------------------------------------------- file helpers
def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise EnvError("cannot read %s (%s)" % (path, exc))


def atomic_write(path, data, backup=True):
    """Write data to path (through a symlink), after a timestamped backup of what is there.
    Returns the backup path or None."""
    real = os.path.realpath(path)
    bak = None
    mode = 0o600
    if os.path.exists(real):
        mode = os.stat(real).st_mode & 0o7777
        if backup:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bak = path + BAK + stamp
            n = 0
            while os.path.exists(bak):
                n += 1
                bak = "%s%s%s-%d" % (path, BAK, stamp, n)
            shutil.copy2(real, bak)
    d = os.path.dirname(real) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".environment-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return bak


def udiff(a, b, fa, fb):
    return "".join(l if l.endswith("\n") else l + "\n"
                   for l in difflib.unified_diff(a, b, fa, fb, lineterm=""))


# ----------------------------------------------------------------- targets
TARGETS = ("claude", "codex")


def claude_installed(ctx):
    """The installed autoMode.environment list, or None when there is none."""
    text = read_text(ctx.settings)
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise EnvError("%s is not valid JSON (%s); fix it by hand" % (ctx.settings, exc))
    if not isinstance(doc, dict):
        raise EnvError("%s is not a JSON object" % ctx.settings)
    am = doc.get("autoMode")
    if am is None:
        return None
    if not isinstance(am, dict):
        raise EnvError("%s: autoMode is not an object" % ctx.settings)
    env = am.get("environment")
    if env is None:
        return None
    if not (isinstance(env, list) and all(isinstance(x, str) for x in env)):
        raise EnvError("%s: autoMode.environment is not a list of strings" % ctx.settings)
    return env


def codex_doc(settings, policy):
    lines = ["# settings"]
    lines += ['%s = "%s"' % (k, settings[k]) for k, _ in CODEX_SETTINGS if k in settings]
    lines.append("# auto_review.policy")
    if policy is None:
        lines.append("<missing or unreadable>")
    else:
        lines += policy.split("\n")
    return lines


def codex_diff(ctx, entries):
    """(differs, text) for config.toml + rules."""
    policy = insert_environment(load_policy(ctx.policy_path, ctx.policy_sha), entries)
    want_cfg = codex_doc(dict(CODEX_SETTINGS), policy)
    cfg_text = read_text(ctx.config_toml)
    blk = read_codex_block(cfg_text, ctx.config_toml) if cfg_text is not None else None
    have_cfg = codex_doc(blk[0], blk[1]) if blk else []
    want_rules = rules_inner(ctx)
    rt = read_text(ctx.rules)
    have_rules = (read_rules_block(rt, ctx.rules) if rt is not None else None) or []
    out = []
    if blk is None:
        out.append("codex: no session-notes block in %s" % ctx.config_toml)
    if have_cfg != want_cfg:
        out.append(udiff(have_cfg, want_cfg, "installed " + ctx.config_toml,
                         "rendered " + ctx.config_toml))
    if have_rules != want_rules:
        out.append(udiff(have_rules, want_rules, "installed " + ctx.rules,
                         "rendered " + ctx.rules))
    return bool(out), "\n".join(o.rstrip("\n") for o in out) + ("\n" if out else "")


def claude_diff(ctx, entries):
    have = claude_installed(ctx)
    if have == entries:
        return False, ""
    head = "" if have is not None else (
        "claude: no autoMode.environment in %s\n" % ctx.settings)
    return True, head + udiff(have or [], entries, "installed %s autoMode.environment"
                              % ctx.settings, "rendered autoMode.environment")


def target_list(ctx, target):
    return list(TARGETS) if target == "all" else [target]


def cmd_diff(ctx, target):
    entries = load_entries(ctx)
    if not entries:
        raise EnvError("%s renders to nothing (no entries); nothing to compare" % ctx.source)
    rc = 0
    for t in target_list(ctx, target):
        home = ctx.claude_home if t == "claude" else ctx.codex_home
        if target == "all" and not os.path.isdir(home):
            sys.stderr.write("%s: skipped (no %s)\n" % (t, home))
            continue
        differs, text = (claude_diff if t == "claude" else codex_diff)(ctx, entries)
        if differs:
            rc = 1
            emit(text)
    return rc


def cmd_render(ctx, target):
    entries = load_entries(ctx)
    if target == "claude":
        emit(json.dumps(entries, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(insert_environment(load_policy(ctx.policy_path, ctx.policy_sha), entries))
    return 0


def confirm(question):
    if not sys.stdin.isatty() and INPUT is input:
        raise EnvError("not a terminal: pass --yes to apply without a prompt")
    try:
        return INPUT(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def apply_claude(ctx, entries, yes):
    differs, text = claude_diff(ctx, entries)
    if not differs:
        emit("claude: %s autoMode.environment already matches" % ctx.settings)
        return
    raw = read_text(ctx.settings)
    doc = json.loads(raw) if raw is not None else {}
    if raw is not None and not isinstance(doc, dict):
        raise EnvError("%s is not a JSON object" % ctx.settings)
    emit(text)
    if not yes and not confirm("Write autoMode.environment to %s?" % ctx.settings):
        emit("claude: not written")
        return
    doc.setdefault("autoMode", {})["environment"] = entries
    bak = atomic_write(ctx.settings, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    emit("claude: wrote %s%s" % (ctx.settings, " (backup %s)" % bak if bak else ""))


def apply_codex(ctx, entries, yes):
    policy = insert_environment(load_policy(ctx.policy_path, ctx.policy_sha), entries)
    cfg_old = read_text(ctx.config_toml)
    rules_old = read_text(ctx.rules)
    cfg_new = place_codex_block(cfg_old or "", policy, ctx.config_toml)
    rules_new = place_rules_block(rules_old or "", rules_inner(ctx), ctx.rules)
    if cfg_new == (cfg_old or "") and rules_new == (rules_old or "") and cfg_old is not None \
            and rules_old is not None:
        emit("codex: %s and %s already match" % (ctx.config_toml, ctx.rules))
        return
    _, text = codex_diff(ctx, entries)
    emit(text)
    if not yes and not confirm("Write %s and %s?" % (ctx.config_toml, ctx.rules)):
        emit("codex: not written")
        return
    for path, old, new in ((ctx.config_toml, cfg_old, cfg_new), (ctx.rules, rules_old, rules_new)):
        if old is not None and old == new:
            continue
        bak = atomic_write(path, new)
        emit("codex: wrote %s%s" % (path, " (backup %s)" % bak if bak else ""))


def cmd_apply(ctx, target, yes):
    entries = load_entries(ctx)
    if not entries:
        raise EnvError("%s renders to nothing (an untouched template, or all comments); "
                       "refusing to apply an empty environment" % ctx.source)
    for t in target_list(ctx, target):
        home = ctx.claude_home if t == "claude" else ctx.codex_home
        if target == "all" and not os.path.isdir(home):
            emit("%s: skipped (no %s)" % (t, home))
            continue
        (apply_claude if t == "claude" else apply_codex)(ctx, entries, yes)
    return 0


# --------------------------------------------------------------- upstream
def codex_version():
    if VERSION_FN:
        return VERSION_FN()
    out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=30)
    m = re.search(r"(\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.-]+)?)", out.stdout + out.stderr)
    if not m:
        raise ValueError("no version in %r" % out.stdout.strip())
    return m.group(1)


def fetch_url(url):
    if FETCH:
        return FETCH(url)
    import urllib.request
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read().decode("utf-8")


def upstream_check(version=None, policy_path=POLICY_PATH):
    """(rc, text): rc 1 + a short diff stat when Codex's bundled policy at the installed
    version differs from the vendored copy; a could-not-check note is rc 0."""
    try:
        version = version or codex_version()
    except Exception as exc:
        return 0, "could not check the upstream Codex policy (no codex version: %s)\n" % exc
    tag = "rust-v" + version
    try:
        up = fetch_url(UPSTREAM_URL % tag)
    except Exception as exc:
        return 0, "could not check the upstream Codex policy at %s (%s: %s)\n" % (
            tag, type(exc).__name__, exc)
    mine = read_text(policy_path)
    if mine is None:
        raise EnvError("cannot read the vendored policy %s" % policy_path)
    if up == mine:
        return 0, ""
    a, b = mine.split("\n"), up.split("\n")
    add = sum(1 for l in difflib.ndiff(a, b) if l.startswith("+ "))
    rem = sum(1 for l in difflib.ndiff(a, b) if l.startswith("- "))
    return 1, ("upstream Codex policy at %s differs from the vendored copy (pinned %s): "
               "+%d -%d lines; see docs/codex.md, Updating the pin\n" % (tag, POLICY_TAG, add, rem))


# ------------------------------------------------------------------ check
def check(env=None):
    """(ran, rc, text): the drift check. ran is False when the feature is not set up (no
    source file, or one that renders to nothing); that is silent. rc 0 clean, 3 drift."""
    ctx = context(env)
    if not os.path.exists(ctx.source):
        return False, 0, ""
    entries = load_entries(ctx)
    if not entries:
        return False, 0, ""
    out = []
    codex_on = False
    if claude_installed(ctx) is not None:
        differs, text = claude_diff(ctx, entries)
        if differs:
            out.append("claude: autoMode.environment in %s differs from %s; run "
                       "`environment_render.py diff` then `apply --target claude`\n%s"
                       % (ctx.settings, ctx.source, text))
    cfg_text = read_text(ctx.config_toml)
    if cfg_text is not None and read_codex_block(cfg_text, ctx.config_toml) is not None:
        codex_on = True
        differs, text = codex_diff(ctx, entries)
        if differs:
            out.append("codex: the session-notes block in %s (or %s) differs from %s; run "
                       "`environment_render.py diff` then `apply --target codex`\n%s"
                       % (ctx.config_toml, ctx.rules, ctx.source, text))
    if codex_on:
        rc, text = upstream_check()
        if rc:
            out.append(text)
    return True, (3 if out else 0), "".join(out)


# -------------------------------------------------------------------- CLI
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--selftest"]:
        argv = ["selftest"]
    ap = argparse.ArgumentParser(prog="environment_render.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--target", choices=TARGETS)
    r.add_argument("--diff", action="store_true", help="alias for the diff command")
    d = sub.add_parser("diff")
    d.add_argument("--target", choices=TARGETS + ("all",), default="all")
    a = sub.add_parser("apply")
    a.add_argument("--target", choices=TARGETS + ("all",), default="all")
    a.add_argument("--yes", "-y", action="store_true")
    sub.add_parser("check")
    u = sub.add_parser("upstream")
    u.add_argument("--codex-version")
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    try:
        if args.cmd == "upstream":
            rc, text = upstream_check(args.codex_version)
            sys.stdout.write(text)
            return rc
        ctx = context()
        if args.cmd == "render":
            if args.diff:
                return cmd_diff(ctx, args.target or "all")
            return cmd_render(ctx, args.target or "claude")
        if args.cmd == "diff":
            return cmd_diff(ctx, args.target)
        if args.cmd == "apply":
            return cmd_apply(ctx, args.target, args.yes)
        if args.cmd == "check":
            _, rc, text = check()
            sys.stdout.write(text)
            return rc
    except EnvError as exc:
        sys.stderr.write("environment_render: %s\n" % exc)
        return 2
    return 2


# --------------------------------------------------------------- selftest
FIXTURE = """<!-- header
spans lines -->
### Org-wide
- Organization: Example Co
- Cloud provider(s): <fill in>
  indented  line   <!-- inline -->
### Empty section
### User-specific
- Primary use: testing   \n\n- Trusted repo: example/repo
"""
FIXTURE_WANT = ["### Org-wide", "- Organization: Example Co", "  indented  line",
                "### User-specific", "- Primary use: testing", "- Trusted repo: example/repo"]


def selftest():
    import contextlib
    import io
    global FETCH, VERSION_FN, INPUT
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    def raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
        except EnvError as exc:
            return str(exc)
        return None

    def run(fn, *a, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*a, **kw)
        return rc, buf.getvalue()

    policy = load_policy()
    check("vendored policy matches the pin", hashlib.sha256(policy.encode()).hexdigest() == POLICY_SHA256)
    check("em dash free", "\u2014" not in open(__file__, encoding="utf-8").read())

    # ---- source parsing
    check("round trip: comments stripped, headings kept, placeholders and empty headings dropped",
          parse_entries(FIXTURE) == FIXTURE_WANT)
    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        tpl = fh.read()
    check("the untouched template renders empty", parse_entries(tpl) == [])
    check("an unterminated comment is an error", raises(parse_entries, "a <!-- b") is not None)

    # ---- codex insertion
    ins = insert_environment(policy, FIXTURE_WANT)
    a = policy.index(ENV_HEADING)
    b = policy.index("\n## Risk Taxonomy")
    body = "\n".join(FIXTURE_WANT)
    k = ins.index(body)
    check("insertion lands inside Environment Profile", a < k < ins.index("## Risk Taxonomy"))
    check("only the inserted text differs", ins == policy[:b].rstrip("\n") + "\n\n" + body
          + "\n\n" + policy[b + 1:] or ins.replace("\n\n" + body + "\n", "", 1) == policy)
    check("bundled bullets precede the entries and the rest is byte-identical",
          ins.startswith(policy[:policy.index("\n\n## Risk Taxonomy")] + "\n\n" + body + "\n\n## Risk Taxonomy")
          and ins.endswith(policy[policy.index("## Risk Taxonomy"):]))
    check("empty entries render empty", insert_environment(policy, []) == "")
    check("no Environment Profile section fails loudly",
          "Environment Profile" in (raises(insert_environment, "## Other\nx\n", ["a"]) or ""))

    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "policy.md")
        with open(bad, "w") as fh:
            fh.write(policy + "x")
        check("pin hash mismatch fails loudly", "sha256" in (raises(load_policy, bad) or ""))

    # ---- TOML strings
    tricky = ['plain', 'back\\slash \\n literal', 'q " and "" and """ and """" end"',
              'ends with quote"', '"""', '\ttab\r\nctl\x01\x7f', 'uni é 中', '\nlead newline', '']
    ok = True
    for t in tricky:
        enc = toml_ml_encode(t)
        try:
            dec, endi = toml_ml_decode("x = " + enc + "\nrest", 4)
            ok = ok and dec == t and ("x = " + enc + "\nrest")[endi:] == "\nrest"
        except ValueError:
            ok = False
        try:
            import tomllib
            ok = ok and tomllib.loads("x = " + enc)["x"] == t
        except ImportError:
            pass
    check("TOML encode/decode round trip on tricky strings", ok)

    # ---- config.toml block placement
    blk = place_codex_block("", "POLICY\n", "c.toml")
    check("new file: block only, readable back",
          blk.startswith(CODEX_BEGIN) and read_codex_block(blk, "c")[1] == "POLICY\n"
          and read_codex_block(blk, "c")[0] == dict(CODEX_SETTINGS))
    src = 'model = "x"\n\n# about tables\n[shell_environment_policy]\nset = { A = "1" }\n'
    out = place_codex_block(src, "P1\n", "c.toml")
    check("block goes before the first table header, after top-level keys",
          out.index('model = "x"') < out.index(CODEX_BEGIN) < out.index("# about tables")
          < out.index("[shell_environment_policy]"))
    out2 = place_codex_block(out, "P2\n", "c.toml")
    check("existing block replaced in place",
          out2.count(CODEX_BEGIN) == 1 and read_codex_block(out2, "c")[1] == "P2\n"
          and out2.replace("P2", "P1") == out)
    check("applying the same policy is idempotent", place_codex_block(out2, "P2\n", "c") == out2)
    out3 = place_codex_block('[a]\nb = 1\n', "P\n", "c")
    check("header-only file: block first, table kept",
          out3.index(CODEX_BEGIN) < out3.index("[a]") and out3.endswith("b = 1\n"))
    out4 = place_codex_block('model = "x"', "P\n", "c")
    check("no table headers: appended", out4.startswith('model = "x"\n\n' + CODEX_BEGIN))
    for bad_src in ('sandbox_mode = "read-only"\n', 'approval_policy="never"\n',
                    'approvals_reviewer = "user"\n', '[auto_review]\npolicy = "x"\n',
                    'auto_review.policy = "x"\n', '[a]\nk=1\n[auto_review.x]\n'):
        msg = raises(place_codex_block, bad_src, "P\n", "c.toml")
        check("refuses a conflict: %r" % bad_src.split("\n")[0], bool(msg) and "line" in msg)
    check("a setting inside another table is not a conflict",
          raises(place_codex_block, '[p]\nsandbox_mode = "x"\n', "P\n", "c") is None)
    moved = place_codex_block('[a]\nk = 1\n\n' + "\n".join(codex_block_lines("OLD\n")) + "\n", "NEW\n", "c")
    check("a block stranded after a table header is moved ahead of it",
          moved.index(CODEX_BEGIN) < moved.index("[a]") and moved.count(CODEX_BEGIN) == 1)
    check("a broken block is refused", raises(place_codex_block, CODEX_BEGIN + "\nx\n", "P", "c") is not None)

    # ---- rules block
    r1 = place_rules_block("# mine\nprefix_rule(pattern=[\"x\"], decision=\"allow\")\n", ["R1"], "r")
    r2 = place_rules_block(r1, ["R1", "R2"], "r")
    check("rules block appended after foreign lines",
          r1.startswith("# mine\nprefix_rule") and read_rules_block(r1, "r") == ["R1"])
    check("rules block replaced in place, foreign lines kept",
          read_rules_block(r2, "r") == ["R1", "R2"] and r2.startswith("# mine\n")
          and place_rules_block(r2, ["R1", "R2"], "r") == r2)
    check("rules block created in an empty file", place_rules_block("", ["R"], "r").startswith(RULES_BEGIN))

    # ---- full flows in a temp dir
    with tempfile.TemporaryDirectory() as d:
        ch, xh = os.path.join(d, "claude"), os.path.join(d, "codex")
        os.makedirs(ch)
        os.makedirs(xh)
        srcf = os.path.join(d, "environment.md")
        fake_bin = os.path.join(d, "repo", "bin")
        ctx = Ctx(srcf, ch, xh, bin_dirs=[fake_bin, os.path.join(ch, "bin")])
        check("check: silent and not run when the source is absent",
              check_env(ctx) == (False, 0, ""))
        with open(srcf, "w") as fh:
            fh.write(tpl)
        check("apply refuses an untouched template",
              "nothing" in (raises(cmd_apply, ctx, "all", True) or "") and not os.path.exists(ctx.settings))
        check("check: silent when the source renders empty", check_env(ctx) == (False, 0, ""))
        with open(srcf, "w") as fh:
            fh.write(FIXTURE)

        # claude
        with open(ctx.settings, "w") as fh:
            json.dump({"model": "m", "autoMode": {"allow": ["keep"], "environment": ["old"]}, "z": 1}, fh)
        rc, out = run(cmd_diff, ctx, "claude")
        check("diff rc 1 with a diff when different", rc == 1 and "-old" in out and "+- Organization: Example Co" in out)
        rc, out = run(cmd_apply, ctx, "claude", True)
        doc = json.load(open(ctx.settings))
        baks = [f for f in os.listdir(ch) if ".bak-environment-" in f]
        check("apply writes the list, a backup, and keeps other keys",
              doc["autoMode"]["environment"] == FIXTURE_WANT and doc["autoMode"]["allow"] == ["keep"]
              and doc["model"] == "m" and doc["z"] == 1 and len(baks) == 1
              and json.load(open(os.path.join(ch, baks[0])))["autoMode"]["environment"] == ["old"])
        check("settings.json ends with a newline", open(ctx.settings).read().endswith("}\n"))
        check("diff silent rc 0 when equal", run(cmd_diff, ctx, "claude") == (0, ""))
        check("apply is a no-op when equal", "already matches" in run(cmd_apply, ctx, "claude", True)[1]
              and len([f for f in os.listdir(ch) if ".bak-environment-" in f]) == 1)
        rc, out = run(cmd_render, ctx, "claude")
        check("render claude is the JSON list", json.loads(out) == FIXTURE_WANT)
        # symlink
        real = os.path.join(d, "real-settings.json")
        os.replace(ctx.settings, real)
        os.symlink(real, ctx.settings)
        with open(srcf, "a") as fh:
            fh.write("- Extra: y\n")
        run(cmd_apply, ctx, "claude", True)
        check("a symlinked settings.json is written through",
              os.path.islink(ctx.settings) and json.load(open(real))["autoMode"]["environment"][-1] == "- Extra: y")
        # malformed + prompt
        with open(real, "w") as fh:
            fh.write("{nope")
        check("malformed settings.json is refused",
              "not valid JSON" in (raises(cmd_apply, ctx, "claude", True) or "") and open(real).read() == "{nope")
        os.unlink(ctx.settings)
        os.unlink(real)
        INPUT = lambda q: "n"
        try:
            run(cmd_apply, ctx, "claude", False)
            check("declined confirm writes nothing", not os.path.exists(ctx.settings))
            INPUT = lambda q: "y"
            run(cmd_apply, ctx, "claude", False)
            check("a yes writes (creating the file)", os.path.exists(ctx.settings))
        finally:
            INPUT = input

        # codex
        with open(ctx.config_toml, "w") as fh:
            fh.write('model = "m"\n[sandbox_workspace_write]\nwritable_roots = ["/x"]\n')
        os.makedirs(os.path.dirname(ctx.rules), exist_ok=True)
        with open(ctx.rules, "w") as fh:
            fh.write('prefix_rule(pattern=["foreign"], decision="allow")\n')
        rc, out = run(cmd_diff, ctx, "codex")
        check("codex diff shows the policy and rules", rc == 1 and "+- Organization: Example Co" in out
              and "session_bus.py" in out)
        run(cmd_apply, ctx, "codex", True)
        cfg = open(ctx.config_toml).read()
        rls = open(ctx.rules).read()
        got = read_codex_block(cfg, "c")
        want_policy = insert_environment(policy, parse_entries(open(srcf).read()))
        check("codex apply writes the block (settings + policy) before the first table",
              got[1] == want_policy and got[0] == dict(CODEX_SETTINGS)
              and cfg.index(CODEX_BEGIN) < cfg.index("[sandbox_workspace_write]")
              and cfg.startswith('model = "m"\n'))
        check("rules: foreign line kept, both bus-send rules present",
              rls.startswith('prefix_rule(pattern=["foreign"]')
              and rls.count('"send"], decision="allow")') == 2 and fake_bin in rls)
        check("codex backups made",
              any(".bak-environment-" in f for f in os.listdir(xh))
              and any(".bak-environment-" in f for f in os.listdir(os.path.dirname(ctx.rules))))
        check("codex diff silent when equal", run(cmd_diff, ctx, "codex") == (0, ""))
        check("codex re-apply is a no-op", "already match" in run(cmd_apply, ctx, "codex", True)[1])
        with open(ctx.config_toml, "a") as fh:
            fh.write('[auto_review]\nx = 1\n')
        check("codex apply refuses a stray [auto_review] table and writes nothing",
              "auto_review" in (raises(cmd_apply, ctx, "codex", True) or ""))
        with open(ctx.config_toml, "w") as fh:
            fh.write(cfg)

        # nightly check + upstream, injected
        with open(srcf, "w") as fh:
            fh.write(FIXTURE)
        run(cmd_apply, ctx, "all", True)
        VERSION_FN = lambda: "0.171.0"
        FETCH = lambda url: policy
        try:
            check("check: silent when everything is equal", check_env(ctx) == (True, 0, ""))
            FETCH = lambda url: policy + "new line\n"
            ran, rc, text = check_env(ctx)
            check("check: upstream policy change is a finding", rc == 3 and "differs from the vendored" in text)
            FETCH = lambda url: policy
            with open(srcf, "a") as fh:
                fh.write("- Changed: yes\n")
            ran, rc, text = check_env(ctx)
            check("check: rendered != installed is a finding naming both targets and the fix",
                  rc == 3 and "claude:" in text and "codex:" in text and "environment_render.py diff" in text
                  and "apply --target codex" in text)
            check("upstream: equal is silent rc 0", upstream_check("0.171.0") == (0, ""))
            FETCH = lambda url: policy.replace("Organization", "Org")
            rc, text = upstream_check("0.171.0")
            check("upstream: differs is rc 1 with a diff stat", rc == 1 and "+1 -1" in text and POLICY_TAG in text)

            def boom(url):
                raise OSError("offline")
            FETCH = boom
            rc, text = upstream_check("0.171.0")
            check("upstream: fetch failure is a note, rc 0", rc == 0 and text.startswith("could not check"))

            def nover():
                raise OSError("no codex")
            VERSION_FN = nover
            rc, text = upstream_check()
            check("upstream: missing codex is a note, rc 0", rc == 0 and text.startswith("could not check"))
            VERSION_FN = lambda: "0.171.0"
            FETCH = lambda url: policy

            # through nightly_check, with a real config in a temp HOME
            import nightly_check
            conf = os.path.join(d, "config.toml")
            with open(conf, "w") as fh:
                fh.write('[paths]\nclaude_home = "%s"\nstate_dir = "%s"\n[agent]\ncodex_home = "%s"\n'
                         '[environment]\nsource = "%s"\n' % (ch, os.path.join(d, "state"), xh, srcf))
            env = {"SESSION_NOTES_CONFIG": conf, "HOME": d}

            class NoRun(object):
                def __call__(self, argv, **kw):
                    class R(object):
                        returncode, stdout, stderr = 0, "", ""
                    return R()
            buf = io.StringIO()
            rc = nightly_check.run_all(env, NoRun(), buf)
            check("nightly_check reports the environment drift (rc 3)",
                  rc == 3 and "environment_render" in buf.getvalue())
            run(cmd_apply, context(env), "all", True)
            buf = io.StringIO()
            rc = nightly_check.run_all(env, NoRun(), buf)
            check("nightly_check silent when the environment is in sync", rc == 0 and buf.getvalue() == "")
            os.unlink(srcf)
            buf = io.StringIO()
            rc = nightly_check.run_all(env, NoRun(), buf)
            doc = json.load(open(os.path.join(d, "state", "last-check.json")))
            check("nightly_check silent and unrecorded when the source is absent",
                  rc == 0 and buf.getvalue() == "" and all(c["check"] != "environment_render" for c in doc["checks"]))
        finally:
            FETCH = VERSION_FN = None

    check("every doc and template is em dash free", all(
        "\u2014" not in open(p, encoding="utf-8").read() for p in (TEMPLATE_PATH,)))
    print("%d failed" % len(fails) if fails else "all passed")
    return 1 if fails else 0


def check_env(ctx):
    """check() against a Ctx instead of the config (selftest seam)."""
    global context
    saved = context
    context = lambda env=None: ctx
    try:
        return check()
    finally:
        context = saved


if __name__ == "__main__":
    sys.exit(main())
