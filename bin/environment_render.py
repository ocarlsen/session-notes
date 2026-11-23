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
    environment_render.py apply [--target ...] [--yes] [--allow-worktree]
                                                              confirm, back up, atomic write
                                                              (codex: refuses from a linked git worktree)
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
FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")   # where codex may live off PATH


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
    # Drop a heading whose section is empty: the next entry is a heading of the same or a
    # shallower level, or there is none. Repeat, so a parent left holding only empty
    # subheadings goes too. (A final "# note" line with nothing after it is dropped as an
    # empty heading; that is the one ambiguity, and it is documented in the template.)
    def level(ln):
        m = re.match(r"(#{1,6})\s", ln)
        return len(m.group(1)) if m else 0
    changed = True
    while changed:
        changed = False
        for i, ln in enumerate(lines):
            lv = level(ln)
            if lv and (i + 1 == len(lines) or 0 < level(lines[i + 1]) <= lv):
                del lines[i]
                changed = True
                break
    return lines


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


def scan_toml(lines, where="config"):
    """(header line indexes, clean flags) for TOML lines. A line is `clean` when it starts
    outside any multi-line string or multi-line array/inline table; only a clean line whose
    first non-space character is `[` is a table header. Refuses (EnvError) when a string or
    array is still open at the end, since then nothing about placement can be trusted."""
    headers, clean = [], []
    in_str = None
    depth = 0
    for no, ln in enumerate(lines):
        is_clean = in_str is None and depth == 0
        clean.append(is_clean)
        if is_clean and ln.lstrip().startswith("["):
            headers.append(no)
            continue
        i, n = 0, len(ln)
        while i < n:
            if in_str:
                if in_str == '"""' and ln[i] == "\\":
                    i += 2
                elif ln.startswith(in_str, i):
                    in_str = None
                    i += 3
                else:
                    i += 1
                continue
            c = ln[i]
            if c == "#":
                break
            if ln.startswith('"""', i) or ln.startswith("'''", i):
                in_str = ln[i:i + 3]
                i += 3
            elif c in "\"'":
                j = i + 1
                while j < n and ln[j] != c:
                    j += 2 if (c == '"' and ln[j] == "\\") else 1
                i = j + 1
            else:
                if c in "[{":
                    depth += 1
                elif c in "]}":
                    depth = max(0, depth - 1)
                i += 1
    if in_str is not None or depth > 0:
        raise EnvError("%s: cannot tell where its tables start (an unterminated multi-line "
                       "string or array); fix the file by hand" % where)
    return headers, clean


def key_path(s):
    """The dotted key at the start of s (bare, "basic" or 'literal' segments), as a list,
    and the rest of s after it; ([], s) when s does not start with a key."""
    segs, i, n = [], 0, len(s)
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] in "\"'":
            q = s[i]
            j = i + 1
            while j < n and s[j] != q:
                j += 2 if (q == '"' and s[j] == "\\") else 1
            seg = s[i + 1:j]
            if q == '"':
                seg = seg.replace('\\"', '"').replace("\\\\", "\\")
            segs.append(seg)
            i = j + 1
        else:
            m = re.match(r"[A-Za-z0-9_-]+", s[i:])
            if not m:
                return [], s
            segs.append(m.group(0))
            i += m.end()
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] == ".":
            i += 1
            continue
        return segs, s[i:]


def _line_key(ln):
    """Key path of a `key = value` line, else []."""
    segs, rest = key_path(ln)
    return segs if segs and rest.startswith("=") else []


def _header_path(ln):
    t = ln.lstrip()
    t = t[2:] if t.startswith("[[") else t[1:]
    return key_path(t)[0]


def first_header(lines, where="config"):
    headers, _ = scan_toml(lines, where)
    return headers[0] if headers else len(lines)


def codex_block_lines(policy):
    out = [CODEX_BEGIN]
    out += ['%s = "%s"' % kv for kv in CODEX_SETTINGS]
    out += ("auto_review.policy = " + toml_ml_encode(policy)).split("\n")
    out.append(CODEX_END)
    return out


def stray_tail(inner, where):
    """Lines inside the block after the closing quotes of auto_review.policy that
    environment_render did not render: what an older `rules_block.py codex-defaults` put
    there (its `# session-notes defaults` comment and top-level `key = value` lines). Returns
    them with blank edges trimmed, to be kept after the END marker; [] when there are none.
    Refuses (EnvError) on anything but comments, blanks and one-line top-level keys, rather
    than delete it."""
    body = "\n".join(inner)
    k = body.find('auto_review.policy = """')
    if k < 0:
        return []
    try:
        _, end = toml_ml_decode(body, k + len("auto_review.policy = "))
    except ValueError:
        return []
    tail = body[end:].split("\n")[1:]          # the rest of the closing-quotes line is dropped
    tail = [ln for ln in tail]
    while tail and not tail[0].strip():
        tail.pop(0)
    while tail and not tail[-1].strip():
        tail.pop()
    if not tail:
        return []
    headers, clean = scan_toml(tail, where + " (inside the session-notes block)")
    bad = None
    for no, ln in enumerate(tail):
        t = ln.strip()
        if not t or t.startswith("#"):
            continue
        path = _line_key(ln)
        if no in headers or not clean[no] or not path or path[0] in (
                set(k2 for k2, _ in CODEX_SETTINGS) | {"auto_review"}):
            bad = ln
            break
    if bad is not None:
        raise EnvError("%s: the session-notes block holds %r after the policy, which is not "
                       "something this tool can move out of the block safely; move or remove "
                       "that line by hand and apply again" % (where, bad.strip()))
    return tail


def place_codex_block(text, policy, where):
    """config.toml text with the block set to `policy`. Refuses on a conflicting setting."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    before, inner, after = split_block(lines, CODEX_BEGIN, CODEX_END, where)
    block_len = 0 if inner is None else len(inner) + 2
    outside = before + after
    headers, clean = scan_toml(outside, where)
    h = headers[0] if headers else len(outside)

    def orig_no(k):
        return k + 1 + (block_len if inner is not None and k >= len(before) else 0)
    guarded = set(k for k, _ in CODEX_SETTINGS) | {"auto_review"}
    for k, ln in enumerate(outside):
        if k in headers:
            if _header_path(ln)[:1] == ["auto_review"]:
                raise EnvError("%s line %d has an [auto_review] table outside the session-notes "
                               "block; resolve it by hand and apply again" % (where, orig_no(k)))
        elif k < h and clean[k]:
            path = _line_key(ln)
            if path and path[0] in guarded:
                raise EnvError("%s line %d sets %r outside the session-notes block; resolve it "
                               "by hand (remove it, or move it out of the way) and apply again"
                               % (where, orig_no(k), ln.strip()))
    new = codex_block_lines(policy)
    moved = stray_tail(inner, where) if inner is not None else []
    if moved:
        new = new + [""] + moved       # kept, top-level, right after the END marker
    if inner is not None and h >= len(before):
        if moved and after and after[0].strip():
            after = [""] + after
        res = before + new + after                      # replace in place
    else:
        rest = before + after
        p = h
        if p >= len(rest):                              # no table header: append
            sep = [""] if rest and rest[-1].strip() else []
            res = rest + sep + new
        else:
            # Step over a comment block above the first table (it belongs to that table), but
            # only lines outside any multi-line string or array: a string that closes on a
            # `#` line must not have the block inserted into it.
            while p > 0 and clean[p - 1] and rest[p - 1].lstrip().startswith("#"):
                p -= 1
            pre = rest[:p]
            sep = [""] if pre and pre[-1].strip() else []
            res = pre + sep + new + [""] + rest[p:]
    return "\n".join(res) + "\n"


def read_codex_block(text, where):
    """(settings dict, policy or None, raw inner lines, the table header the block sits
    under or None) from the block, or None."""
    lines = text.split("\n")
    _, inner, _ = split_block(lines, CODEX_BEGIN, CODEX_END, where)
    if inner is None:
        return None
    before = split_block(lines, CODEX_BEGIN, CODEX_END, where)[0]
    hs, _ = scan_toml(before, where)
    inside = before[hs[-1]].strip() if hs else None   # the nearest table header above
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
    return settings, policy, inner, inside


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
            # On a same-second clash: the number after every existing one (never a pruned
            # lower name), so ranking backups by name stays correct.
            d0, name0 = os.path.split(bak)
            top = -1
            for other in os.listdir(d0 or "."):
                if other == name0:
                    top = max(top, 0)
                elif other.startswith(name0 + "-") and other[len(name0) + 1:].isdigit():
                    top = max(top, int(other[len(name0) + 1:]))
            if top >= 0:
                bak = "%s-%d" % (bak, top + 1)
            shutil.copyfile(real, bak)     # not copy2: prune orders by when the copy was made
            shutil.copymode(real, bak)
    if bak:
        import rules_block      # lazy: rules_block imports this module for CODEX_END
        rules_block.prune_backups(path, bak, BAK.rstrip("-"))
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


def codex_diff(ctx, entries):
    """(differs, text) for config.toml + rules. The whole marked block is compared, so a
    foreign line inside it shows as a removal."""
    policy = insert_environment(load_policy(ctx.policy_path, ctx.policy_sha), entries)
    want_cfg = codex_block_lines(policy)
    cfg_text = read_text(ctx.config_toml)
    blk = read_codex_block(cfg_text, ctx.config_toml) if cfg_text is not None else None
    have_cfg = [CODEX_BEGIN] + blk[2] + [CODEX_END] if blk else []
    want_rules = rules_inner(ctx)
    rt = read_text(ctx.rules)
    have_rules = (read_rules_block(rt, ctx.rules) if rt is not None else None) or []
    out = []
    if blk is None:
        out.append("codex: no session-notes block in %s" % ctx.config_toml)
    elif blk[3]:
        out.append("codex: the session-notes block in %s is inside table %s; Codex does not "
                   "read it as top-level (run apply to move it)" % (ctx.config_toml, blk[3]))
    moved = stray_tail(blk[2], ctx.config_toml) if blk else []
    if moved:
        out.append("codex: %d line(s) inside the block are not ours (an older codex-defaults put "
                   "them there); apply moves them, unchanged, to just after the END marker:\n%s"
                   % (len(moved), "\n".join("  " + ln for ln in moved)))
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
    if "autoMode" in doc and not isinstance(doc["autoMode"], dict):
        raise EnvError("%s: autoMode is not an object; fix or remove that key by hand"
                       % ctx.settings)
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


def linked_worktree(path):
    """True when path sits in a linked git worktree (its .git is a file, or its git dir is not
    the common dir): a checkout that is deleted when its branch merges."""
    if not os.path.isdir(path):
        return False
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--git-dir", "--git-common-dir"],
                           capture_output=True, text=True, timeout=20,
                           env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    except (OSError, subprocess.SubprocessError):
        r = None
    if r is not None and r.returncode == 0 and len(r.stdout.split("\n")) >= 2:
        gd, cd = r.stdout.split("\n")[:2]
        gd, cd = (os.path.realpath(os.path.join(path, x)) for x in (gd, cd))
        return gd != cd
    cur = os.path.realpath(path)
    while True:                       # no usable git: look for a .git FILE up the tree
        if os.path.isfile(os.path.join(cur, ".git")):
            return True
        if os.path.exists(os.path.join(cur, ".git")) or os.path.dirname(cur) == cur:
            return False
        cur = os.path.dirname(cur)


def cmd_apply(ctx, target, yes, allow_worktree=False):
    entries = load_entries(ctx)
    if not entries:
        raise EnvError("%s renders to nothing (an untouched template, or all comments); "
                       "refusing to apply an empty environment" % ctx.source)
    codex_applies = "codex" in target_list(ctx, target) and not (
        target == "all" and not os.path.isdir(ctx.codex_home))
    if codex_applies and not allow_worktree:      # before ANY write, Claude's included
        for d in ctx.send_bin_dirs():
            if linked_worktree(d):
                raise EnvError("%s is in a linked git worktree, and the Codex allow rules "
                               "would name it (deleted when the branch merges); apply from "
                               "the main checkout, or pass --allow-worktree to test a "
                               "branch" % d)
    for t in target_list(ctx, target):
        home = ctx.claude_home if t == "claude" else ctx.codex_home
        if target == "all" and not os.path.isdir(home):
            emit("%s: skipped (no %s)" % (t, home))
            continue
        (apply_claude if t == "claude" else apply_codex)(ctx, entries, yes)
    return 0


# --------------------------------------------------------------- upstream
def find_codex():
    """codex on PATH, else the usual install dirs (a LaunchAgent's PATH is minimal)."""
    found = shutil.which("codex")
    if found:
        return found
    home = os.environ.get("HOME") or os.path.expanduser("~")
    for d in (os.path.join(home, ".local", "bin"),) + FALLBACK_DIRS:
        cand = os.path.join(d, "codex")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise FileNotFoundError("codex not found on PATH or in ~/.local/bin, /opt/homebrew/bin, "
                            "/usr/local/bin")


def codex_version():
    if VERSION_FN:
        return VERSION_FN()
    exe = find_codex()
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
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
    note = ""
    if codex_on:
        rc, text = upstream_check()
        if rc:
            out.append(text)
        else:
            note = text      # a could-not-check note: shown, never a finding
    return True, (3 if out else 0), "".join(out) + note


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
    a.add_argument("--allow-worktree", action="store_true",
                   help="codex target: allow rules naming a linked git worktree's bin")
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
            return cmd_apply(ctx, args.target, args.yes, args.allow_worktree)
        if args.cmd == "check":
            _, rc, text = check()
            sys.stdout.write(text)
            return rc
    except EnvError as exc:
        sys.stderr.write("environment_render: %s\n" % exc)
        return 2
    return 2


# --------------------------------------------------------------- selftest
try:
    import tomllib
    HAVE_TOMLLIB = True
except ImportError:     # Python < 3.11: the selftest's TOML validity asserts are skipped, loudly
    tomllib = None
    HAVE_TOMLLIB = False

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
    ri = policy.index("## Risk Taxonomy")
    check("only the inserted text differs (exact)", ins == policy[:ri] + body + "\n\n" + policy[ri:])
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

    # ---- nested headings (B3)
    nested = "## Org\n### Cloud\n- Provider: x\n### Other\n## Next\n- a\n"
    check("nested headings are kept; empty ones drop", parse_entries(nested) ==
          ["## Org", "### Cloud", "- Provider: x", "## Next", "- a"])
    check("a parent holding only empty subheadings drops with them",
          parse_entries("## Org\n### A\n### B\n") == [] and parse_entries("## A\n### B\n#### C\n") == [])
    check("a final heading with nothing after it is dropped", parse_entries("- a\n# note\n") == ["- a"])

    # ---- table headers and strings (B1, N1, N2)
    def tomlcheck(text):
        if HAVE_TOMLLIB:
            tomllib.loads(text)
        return True
    for name, src in (
            ("odd header chars", '[projects."/srv/data/Sync Folder/cloud~store/proj"]\ntrust = "x"\n'),
            ("parens and plus", '[a."x(y)+z,w"]\nk = 1\n'),
            ("non-ascii header", '[projects."/h/caf\u00e9"]\nk = 1\n'),
            ("array of arrays", 'x = [\n  ["a", "b"],\n  ["c"]\n]\n[a]\nk = 1\n'),
            ("multi-line string with [note]", 'instructions = """\nsee\n[note]\nbye\n"""\n[a]\nk = 1\n'),
            ("literal ml string", "t = \'\'\'\n[inner]\n\'\'\'\n[a]\nk = 1\n")):
        o = place_codex_block(src, "P\n", "c.toml")
        tail = o[o.index(CODEX_END):]
        check("block lands before the first real table: " + name,
              o.index(CODEX_BEGIN) < o.index("[a" if "[a" in src else "[projects") and src.split("\n")[0] in o
              and read_codex_block(o, "c")[3] is None and read_codex_block(o, "c")[1] == "P\n" and tomlcheck(o))
    check("unterminated multi-line string refuses",
          "cannot tell" in (raises(place_codex_block, 'x = """\nnever closed\n', "P\n", "c") or ""))
    check("unterminated array refuses",
          "cannot tell" in (raises(place_codex_block, 'x = [\n 1,\n', "P\n", "c") or ""))
    for bad_src in ('"sandbox_mode" = "read-only"\n', "'approval_policy' = 'never'\n",
                    '"approvals_reviewer"="user"\n', '["auto_review"]\nx=1\n', '"auto_review".policy = "x"\n',
                    "[ 'auto_review' . x ]\n", '[[auto_review]]\n', '[a]\nk=1\n["auto_review"]\n'):
        check("refuses a quoted-key conflict: %r" % bad_src.split("\n")[0],
              raises(place_codex_block, bad_src, "P\n", "c.toml") is not None)
    check("a key mentioned inside a multi-line string is not a conflict",
          raises(place_codex_block, 'x = """\nsandbox_mode = 1\n"""\n', "P\n", "c") is None)

    # ---- block inside a table is drift (B2)
    stray = '[a]\nk = 1\n' + "\n".join(codex_block_lines("P\n")) + "\n"
    blk = read_codex_block(stray, "c")
    check("read reports the table a block sits under", blk[3] == "[a]")

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

        # block inside a table, foreign line inside the block
        good = open(ctx.config_toml).read()
        with open(ctx.config_toml, "w") as fh:
            fh.write("[t]\nk = 1\n" + good[good.index(CODEX_BEGIN):good.index(CODEX_END) + len(CODEX_END)] + "\n")
        rc, out = run(cmd_diff, ctx, "codex")
        check("diff: a block under a table header is drift with a clear message",
              rc == 1 and "inside table [t]" in out and "not read it as top-level" in out)
        with open(ctx.config_toml, "w") as fh:
            fh.write(good)
        with open(srcf, "w") as fh:
            fh.write(FIXTURE)
        run(cmd_apply, ctx, "codex", True)
        good = open(ctx.config_toml).read()
        with_extra = good.replace(CODEX_END, "model = 'foreign'\n" + CODEX_END)
        with open(ctx.config_toml, "w") as fh:
            fh.write(with_extra)
        rc, out = run(cmd_diff, ctx, "codex")
        check("diff: a foreign line inside the block shows as a removal",
              rc == 1 and "-model = 'foreign'" in out)
        rc, out = run(cmd_apply, ctx, "codex", True)
        moved_cfg = open(ctx.config_toml).read()
        check("apply shows the move and keeps the foreign key, now after the END marker",
              "-model = 'foreign'" in out and "moves them" in out
              and moved_cfg.count("model = 'foreign'") == 1
              and moved_cfg.index("model = 'foreign'") > moved_cfg.index(CODEX_END)
              and moved_cfg.index("model = 'foreign'") < moved_cfg.index("[sandbox_workspace_write]")
              and run(cmd_diff, ctx, "codex") == (0, ""))

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
            fakehome = os.path.join(d, "fh")
            os.makedirs(os.path.join(fakehome, ".local", "bin"))
            exe = os.path.join(fakehome, ".local", "bin", "codex")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\necho codex-cli 9.8.7\n")
            os.chmod(exe, 0o755)
            oldenv = dict(os.environ)
            os.environ["PATH"] = "/nonexistent"
            os.environ["HOME"] = fakehome
            try:
                VERSION_FN = None
                check("codex is found under ~/.local/bin when PATH lacks it",
                      find_codex() == exe and codex_version() == "9.8.7")
                os.environ["HOME"] = os.path.join(d, "nohome")
                global FALLBACK_DIRS
                saved_dirs, FALLBACK_DIRS = FALLBACK_DIRS, ()
                try:
                    check("codex missing everywhere raises FileNotFoundError", _missing(find_codex))
                finally:
                    FALLBACK_DIRS = saved_dirs
            finally:
                os.environ.clear()
                os.environ.update(oldenv)
            VERSION_FN = nover
            run(cmd_apply, ctx, "all", True)
            ran, rc, text = check_env(ctx)
            check("check prints the could-not-check note but it is not a finding",
                  rc == 0 and text.startswith("could not check") and ran)
            VERSION_FN = lambda: "0.171.0"
            FETCH = lambda url: policy

            # through nightly_check, with a real config in a temp HOME. nightly_check does its
            # own `import environment_render`; make that resolve to THIS module so the FETCH /
            # VERSION_FN stubs reach it (never the real codex or network).
            saved_mod = sys.modules.get("environment_render")
            sys.modules["environment_render"] = sys.modules[__name__]
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
            run(cmd_apply, context(env), "all", True, allow_worktree=linked_worktree(BIN))   # a worktree needs the flag
            buf = io.StringIO()
            rc = nightly_check.run_all(env, NoRun(), buf)
            check("nightly_check silent when the environment is in sync", rc == 0 and buf.getvalue() == "")
            def failing_fetch(url):
                raise OSError("offline")
            FETCH = failing_fetch
            buf = io.StringIO()
            rc = nightly_check.run_all(env, NoRun(), buf)
            doc = json.load(open(os.path.join(d, "state", "last-check.json")))
            envc = [c for c in doc["checks"] if c["check"] == "environment_render"]
            check("nightly_check: a failed upstream fetch puts the note in the report, rc 0, 0 findings",
                  rc == 0 and "could not check" in buf.getvalue()
                  and envc and envc[0]["findings"] == 0 and envc[0]["status"] == "clean")
            FETCH = lambda url: policy
            os.unlink(srcf)
            buf = io.StringIO()
            rc = nightly_check.run_all(env, NoRun(), buf)
            doc = json.load(open(os.path.join(d, "state", "last-check.json")))
            check("nightly_check silent and unrecorded when the source is absent",
                  rc == 0 and buf.getvalue() == "" and all(c["check"] != "environment_render" for c in doc["checks"]))
        finally:
            FETCH = VERSION_FN = None
            if saved_mod is None:
                sys.modules.pop("environment_render", None)
            else:
                sys.modules["environment_render"] = saved_mod

    # ---- review follow-ups (PR #49)
    # a multi-line string closing on a `#` line right before the first table
    src = 'instructions = """\ndo\n# notes"""\n[tbl]\nk=1\n'
    o = place_codex_block(src, "P\n", "c.toml")
    check("a string closing on a # line: block lands top-level, before the table, not in the string",
          o.index(CODEX_BEGIN) > o.index('# notes"""') and o.index(CODEX_END) < o.index("[tbl]")
          and read_codex_block(o, "c")[3] is None and place_codex_block(o, "P\n", "c.toml") == o)
    if HAVE_TOMLLIB:
        data = tomllib.loads(o)
        check("... and the result parses with the string intact and the settings top-level",
              data["instructions"] == "do\n# notes" and data["sandbox_mode"] == "workspace-write"
              and data["tbl"] == {"k": 1})
    # a plain comment above the first table still stays with that table
    o = place_codex_block("# about tbl\n[tbl]\nk=1\n", "P\n", "c.toml")
    check("a comment block above the first table stays with it",
          o.index(CODEX_END) < o.index("# about tbl") < o.index("[tbl]"))
    # the "inside table" message names the nearest header above the block
    two = "[a]\nk = 1\n[b]\nj = 1\n" + "\n".join(codex_block_lines("P\n")) + "\n"
    check("a block under two tables names the nearest one", read_codex_block(two, "c")[3] == "[b]")

    # N-e: autoMode that is not an object is a loud refusal naming the key
    with tempfile.TemporaryDirectory() as d:
        ch2 = os.path.join(d, "claude")
        os.makedirs(ch2)
        srcf2 = os.path.join(d, "environment.md")
        with open(srcf2, "w") as fh:
            fh.write(FIXTURE)
        ctx2 = Ctx(srcf2, ch2, os.path.join(d, "codex"), bin_dirs=[os.path.join(d, "bin")])
        for bad in (None, "x", [1]):
            with open(ctx2.settings, "w") as fh:
                json.dump({"autoMode": bad, "z": 1}, fh)
            msg = None
            try:
                run(cmd_apply, ctx2, "claude", True)
            except EnvError as exc:
                msg = str(exc)
            check("apply refuses autoMode=%r naming the key, settings untouched" % (bad,),
                  msg is not None and "autoMode" in msg
                  and json.load(open(ctx2.settings)) == {"autoMode": bad, "z": 1})

        # N-g: only the newest 3 .bak-environment-* copies of each file are kept
        with open(ctx2.settings, "w") as fh:
            json.dump({"autoMode": {"environment": ["old"]}}, fh)
        for i in range(6):
            run(cmd_apply, ctx2, "claude", True)
            with open(ctx2.settings, "w") as fh:
                json.dump({"autoMode": {"environment": ["old%d" % i]}}, fh)
            time.sleep(0.02)
        baks2 = sorted(f for f in os.listdir(ch2) if ".bak-environment-" in f)
        lookalike = os.path.join(ch2, "settings.json.bak-environment-notes")
        open(lookalike, "w").write("keep\n")
        run(cmd_apply, ctx2, "claude", True)
        left = [f for f in os.listdir(ch2) if f.startswith("settings.json.bak-environment-2")]
        survivors = sorted(open(os.path.join(ch2, f)).read() for f in left)
        check("backups pruned to the newest 3; a lookalike name is never touched",
              len(baks2) <= 3 and len(left) == 3 and open(lookalike).read() == "keep\n")
        check("the newest three backups are the ones kept",
              all(json.loads(t)["autoMode"]["environment"][0] in ("old3", "old4", "old5") for t in survivors)
              and len(set(survivors)) == 3)

    # F2: codex apply from a linked git worktree refuses unless --allow-worktree
    if shutil.which("git"):
        with tempfile.TemporaryDirectory() as d:
            main_co, wt = os.path.join(d, "main"), os.path.join(d, "wt")
            clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            g = lambda *a, cwd=main_co: subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                 "-c", "commit.gpgsign=false", "-C", cwd] + list(a),
                capture_output=True, text=True, check=True, env=clean_env)
            os.makedirs(os.path.join(main_co, "bin"))
            open(os.path.join(main_co, "bin", "x"), "w").write("x\n")
            g("init", "-q")
            g("add", ".")
            g("commit", "-q", "-m", "init")
            g("worktree", "add", "-q", "-b", "b", wt)
            check("linked_worktree: true in a worktree, false in the main checkout",
                  linked_worktree(os.path.join(wt, "bin")) and not linked_worktree(os.path.join(main_co, "bin"))
                  and not linked_worktree(os.path.join(d, "nowhere")))
            other = os.path.join(d, "other")
            os.makedirs(other)
            g("init", "-q", cwd=other)
            saved_git = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("GIT_")}
            os.environ["GIT_DIR"] = os.path.join(other, ".git")
            try:
                check("linked_worktree ignores an inherited GIT_DIR pointing at another repo",
                      linked_worktree(os.path.join(wt, "bin")) and not linked_worktree(os.path.join(main_co, "bin")))
            finally:
                os.environ.pop("GIT_DIR", None)
                os.environ.update(saved_git)
            srcf3, xh3 = os.path.join(d, "environment.md"), os.path.join(d, "codex")
            os.makedirs(xh3)
            with open(srcf3, "w") as fh:
                fh.write(FIXTURE)
            ctx3 = Ctx(srcf3, os.path.join(d, "claude"), xh3, bin_dirs=[os.path.join(wt, "bin")])
            msg = raises(cmd_apply, ctx3, "codex", True) or ""
            check("apply --target codex from a worktree refuses, naming the fix, writes nothing",
                  "linked git worktree" in msg and "main checkout" in msg and "--allow-worktree" in msg
                  and not os.path.exists(ctx3.config_toml) and not os.path.exists(ctx3.rules))
            os.makedirs(ctx3.claude_home)
            with open(ctx3.settings, "w") as fh:
                json.dump({"z": 1}, fh)
            buf3 = io.StringIO()
            with contextlib.redirect_stdout(buf3):
                msg = raises(cmd_apply, ctx3, "all", True) or ""
            check("apply --target all refuses before writing anything, Claude's settings included",
                  "linked git worktree" in msg and json.load(open(ctx3.settings)) == {"z": 1}
                  and not [f for f in os.listdir(ctx3.claude_home) if ".bak" in f])
            rc, out = run(cmd_diff, ctx3, "codex")
            check("diff from a worktree still runs", rc == 1 and "no session-notes block" in out)
            run(cmd_apply, ctx3, "codex", True, allow_worktree=True)
            check("--allow-worktree applies", os.path.exists(ctx3.config_toml)
                  and os.path.join(wt, "bin") in open(ctx3.rules).read())
            ctx4 = Ctx(srcf3, os.path.join(d, "claude"), os.path.join(d, "codex4"),
                       bin_dirs=[os.path.join(main_co, "bin")])
            os.makedirs(ctx4.codex_home)
            run(cmd_apply, ctx4, "codex", True)
            check("the main checkout applies without the flag", os.path.exists(ctx4.config_toml))
    else:
        print("git unavailable: worktree guard asserts skipped")

    if not HAVE_TOMLLIB:
        print("tomllib unavailable: TOML validity asserts skipped (run under python3.11+)")

    dash = chr(0x2014)
    import glob
    covered = [os.path.join(REPO, f) for f in ("README.md", "NOTICE", "install.sh", "config.example.toml",
                                                "docs/codex.md", "docs/tools.md", "bin/environment_render.py")]
    covered += [f for f in glob.glob(os.path.join(REPO, "templates", "*.md")) if f != POLICY_PATH]
    check("every file this feature touches is em dash free (%d files)" % len(covered),
          len(covered) >= 9 and all(dash not in open(f, encoding="utf-8").read() for f in covered))
    print("%d failed" % len(fails) if fails else "all passed")
    return 1 if fails else 0


def _missing(fn):
    try:
        fn()
    except FileNotFoundError:
        return True
    return False


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
