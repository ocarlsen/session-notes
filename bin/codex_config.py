"""Reads the two Codex config.toml keys that size AGENTS.md files: project_doc_max_bytes and
project_doc_fallback_filenames. One reader for every caller (the memory canary and rules_block),
so the TOML handling, the scanner fallback and the validity rules live in one place.

read_settings(text) -> ({key: value}, [problem]); read_key(text, key, default) -> (value, problem).
Neither raises on a malformed config: a problem comes back as text naming the key, the value and
the reason, and the caller decides whether that is a finding (the canary) or a refusal (rules_block).
tomllib/tomli is used when importable, else a small top-level scanner.
"""
import re

try:  # Python 3.11+
    import tomllib as _tomllib
except ImportError:
    try:
        import tomli as _tomllib
    except ImportError:
        _tomllib = None


_CODEX_KEYS = ("project_doc_max_bytes", "project_doc_fallback_filenames")


def _skip_string(text, i):
    """text[i] opens a TOML string; the index just past it. ValueError if unterminated."""
    n = len(text)
    for q in ('"""', "'''"):
        if text.startswith(q, i):
            j = i + 3
            while True:
                j = text.find(q, j)
                if j < 0:
                    raise ValueError("unterminated multi-line string")
                if q == "'''" or (len(text[:j]) - len(text[:j].rstrip("\\"))) % 2 == 0:
                    j += 3
                    while j < n and text[j] == q[0]:  # up to two extra quotes belong to the string
                        j += 1
                    return j
                j += 1
    q = text[i]
    j = i + 1
    while j < n and text[j] != "\n":
        if q == '"' and text[j] == "\\":
            j += 2
            continue
        if text[j] == q:
            return j + 1
        j += 1
    raise ValueError("unterminated string")


def _scan_top_level(text):
    """[(key, value_text, line)] of the top-level key/value pairs, up to the first table header
    outside any string or array. Tracks multi-line strings and bracket depth, so a key-looking
    line inside a string or array is never mistaken for a key. Comments are dropped from the
    value text. ValueError on text it cannot follow."""
    out, i, n = [], 0, len(text)
    while True:
        while i < n and (text[i].isspace()):
            i += 1
        while i < n and text[i] == "#":
            while i < n and text[i] != "\n":
                i += 1
            while i < n and text[i].isspace():
                i += 1
        if i >= n or text[i] == "[":
            return out
        line = text.count("\n", 0, i) + 1
        parts = []
        while True:
            while i < n and text[i] in " \t":
                i += 1
            if i < n and text[i] in "\"'":
                j = _skip_string(text, i)
                parts.append(_toml_string(text[i:j]))
                i = j
            else:
                m = re.compile(r"[A-Za-z0-9_-]+").match(text, i)
                if not m:
                    raise ValueError("cannot parse a key at line %d" % line)
                parts.append(m.group(0))
                i = m.end()
            while i < n and text[i] in " \t":
                i += 1
            if i < n and text[i] == ".":
                i += 1
                continue
            break
        if i >= n or text[i] != "=":
            raise ValueError("expected '=' at line %d" % line)
        i += 1
        buf, depth = [], 0
        while i < n:
            c = text[i]
            if c in "\"'":
                j = _skip_string(text, i)
                buf.append(text[i:j])
                i = j
                continue
            if c == "#":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            elif c == "\n" and depth <= 0:
                break
            buf.append(c)
            i += 1
        out.append((".".join(parts), "".join(buf).strip(), line))


def _toml_string(tok):
    if tok.startswith('"""') or tok.startswith("'''"):
        body = tok[3:-3]
        return body[1:] if body.startswith("\n") else body
    if tok[0] == "'":
        return tok[1:-1]
    import json
    try:
        return json.loads(tok)
    except ValueError:
        raise ValueError("bad string %s" % tok) from None


def _toml_value(raw):
    """int, str or list of those from the value text; ValueError for anything else."""
    raw = raw.strip()
    if re.fullmatch(r"[+-]?\d+(?:_\d+)*", raw):
        return int(raw.replace("_", ""))
    if raw in ("true", "false"):
        return raw == "true"
    if not raw:
        raise ValueError("empty value")
    if raw.startswith(('"', "'")):
        if _skip_string(raw, 0) != len(raw):
            raise ValueError("trailing text")
        return _toml_string(raw)
    if raw.startswith("[") and raw.endswith("]"):
        items, i, body = [], 0, raw[1:-1]
        while True:
            while i < len(body) and body[i].isspace():
                i += 1
            if i >= len(body):
                return items
            if body[i] not in "\"'":
                raise ValueError("array item is not a string")
            j = _skip_string(body, i)
            items.append(_toml_string(body[i:j]))
            i = j
            while i < len(body) and body[i].isspace():
                i += 1
            if i < len(body):
                if body[i] != ",":
                    raise ValueError("expected ',' in array")
                i += 1
    raise ValueError("unsupported value")


def config_values(text, tomllib=_tomllib):
    """{key: python value or _Bad(reason)} for the two keys, from the Codex config text. Uses
    tomllib/tomli when importable, else the scanner above. A reason string means unreadable."""
    found = {}
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            return {"": _Bad("invalid TOML: %s" % exc)}
        return {k: data[k] for k in _CODEX_KEYS if k in data}
    try:
        pairs = _scan_top_level(text)
    except ValueError as exc:
        return {"": _Bad("cannot read the config: %s" % exc)}
    for key, raw, line in pairs:
        if key in _CODEX_KEYS:
            try:
                found[key] = _toml_value(raw)
            except ValueError as exc:
                found[key] = _Bad("%s (value %s)" % (exc, raw) if raw.strip() else str(exc))
    return found


class _Bad(object):
    def __init__(self, reason):
        self.reason = reason


def _valid(key, v):
    """None when v is a usable value for key, else the "must be ..." reason."""
    if key == "project_doc_max_bytes":
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return "must be a non-negative integer"
    elif key == "project_doc_fallback_filenames":
        if not isinstance(v, list) or not all(isinstance(x, str) and x and "/" not in x for x in v):
            return "must be a list of file names"
    return None


def read_settings(text, tomllib=_tomllib):
    """({key: valid value}, [problem]) from Codex config text. Keys that are absent are absent
    from the dict. A problem reads "KEY: REASON" or "KEY = VALUE: must be ..."; a config that
    cannot be parsed at all is one problem keyed "config". `tomllib` lets a selftest force the
    scanner (None)."""
    values, problems = {}, []
    for key, v in config_values(text, tomllib).items():
        name = key or "config"
        if isinstance(v, _Bad):
            problems.append("%s: %s" % (name, v.reason))
            continue
        why = _valid(key, v)
        if why:
            problems.append("%s = %r: %s" % (name, v, why))
        else:
            values[key] = v
    return values, problems


def read_key(text, key, default, tomllib=_tomllib):
    """(value, problem) for one key: (default, None) when absent, (value, None) when valid,
    (default, problem) when the key or the whole config is unreadable or invalid."""
    values, problems = read_settings(text, tomllib)
    mine = [p for p in problems if p.startswith(key) or p.startswith("config")]
    if mine:
        return default, "; ".join(mine)
    return values.get(key, default), None
