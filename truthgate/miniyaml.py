"""A tiny YAML subset parser, stdlib only.

truthgate ships a hard "no third-party packages" promise -- that is what makes
a verification gate auditable, because there is no transitive dependency tree
to trust.  PyYAML would break it, so this parses the subset a check file
actually needs:

    checks:
      - name: unit tests pass
        type: command
        run: python3 -m pytest -q
        expect_exit: 0
      - name: README exists
        type: file_exists
        path: README.md

Supported: nested mappings by indentation, block sequences, scalars
(str/int/float/bool/null), quoted strings, inline flow lists, and ``#``
comments.  Anything outside that raises :class:`MiniYamlError` rather than
being silently misparsed -- a check file that cannot be understood must not
quietly become an empty check set, because "0 checks passed" looks exactly
like "everything is fine".
"""

from __future__ import annotations

from typing import Any


class MiniYamlError(ValueError):
    """The document is outside the supported subset or is malformed."""


_BOOL_TRUE = {"true", "yes", "on"}
_BOOL_FALSE = {"false", "no", "off"}
_NULL = {"", "~", "null"}

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "/": "/",
}


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment that is not inside quotes."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (not out or out[-1] in " \t"):
            break
        else:
            out.append(ch)
        i += 1
    if quote:
        raise MiniYamlError(f"unterminated quote in: {line!r}")
    return "".join(out).rstrip()


def _unescape(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "u" and i + 5 < len(raw) + 1:
                try:
                    out.append(chr(int(raw[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except ValueError as exc:
                    raise MiniYamlError(f"bad \\u escape: {raw!r}") from exc
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _scalar(token: str) -> Any:
    """Convert a bare YAML scalar token into a Python value."""
    text = token.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        body = text[1:-1]
        return _unescape(body) if text[0] == '"' else body.replace("''", "'")
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_flow(inner)]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        result: dict[str, Any] = {}
        if not inner:
            return result
        for part in _split_flow(inner):
            if ":" not in part:
                raise MiniYamlError(f"bad inline mapping entry: {part!r}")
            k, v = part.split(":", 1)
            result[str(_scalar(k))] = _scalar(v)
        return result
    low = text.lower()
    if low in _NULL:
        return None
    if low in _BOOL_TRUE:
        return True
    if low in _BOOL_FALSE:
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _split_flow(inner: str) -> list[str]:
    """Split a flow collection body on top-level commas."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if quote:
        raise MiniYamlError(f"unterminated quote in flow: {inner!r}")
    tail = "".join(buf)
    if tail.strip():
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines: list[tuple[int, str]], start: int, indent: int) -> tuple[Any, int]:
    """Parse one block at ``indent``; return (value, next_index)."""
    if start >= len(lines):
        return None, start
    if lines[start][1].startswith("- "):
        return _parse_sequence(lines, start, indent)
    if lines[start][1] == "-":
        return _parse_sequence(lines, start, indent)
    return _parse_mapping(lines, start, indent)


def _parse_sequence(lines: list[tuple[int, str]], start: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    i = start
    while i < len(lines):
        ind, text = lines[i]
        if ind < indent or not (text == "-" or text.startswith("- ")):
            break
        rest = text[1:].strip()
        child_indent = ind + 2
        if not rest:
            i += 1
            if i < len(lines) and lines[i][0] > ind:
                value, i = _parse_block(lines, i, lines[i][0])
                items.append(value)
            else:
                items.append(None)
            continue
        if ":" in rest and not rest.startswith(("[", "{", '"', "'")):
            inline = [(child_indent, rest)]
            j = i + 1
            while j < len(lines) and lines[j][0] >= child_indent and not lines[j][1].startswith("- "):
                inline.append((lines[j][0], lines[j][1]))
                j += 1
            value, _ = _parse_mapping(inline, 0, child_indent)
            items.append(value)
            i = j
            continue
        items.append(_scalar(rest))
        i += 1
    return items, i


def _parse_mapping(lines: list[tuple[int, str]], start: int, indent: int) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    i = start
    while i < len(lines):
        ind, text = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise MiniYamlError(f"unexpected indent at line {i}: {text!r}")
        if text.startswith("- "):
            break
        if ":" not in text:
            raise MiniYamlError(f"expected 'key: value' but found: {text!r}")
        key, _, rest = text.partition(":")
        key = key.strip()
        if not key:
            raise MiniYamlError(f"empty key at line {i}: {text!r}")
        rest = rest.strip()
        if rest:
            mapping[key] = _scalar(rest)
            i += 1
            continue
        i += 1
        if i < len(lines) and lines[i][0] > ind:
            value, i = _parse_block(lines, i, lines[i][0])
            mapping[key] = value
        elif i < len(lines) and lines[i][0] == ind and lines[i][1].startswith("- "):
            value, i = _parse_sequence(lines, i, ind)
            mapping[key] = value
        else:
            mapping[key] = None
    return mapping, i


def _common_indent(lines: list[str]) -> int:
    """Smallest leading-space count among non-blank lines."""
    widths = [len(ln) - len(ln.lstrip(" ")) for ln in lines if ln.strip()]
    return min(widths) if widths else 0


def loads(text: str) -> Any:
    """Parse a YAML-subset document into Python data.

    A uniformly indented document (e.g. embedded in a Python string) is
    dedented to column 0 first, so a check file does not have to be flush
    left when it is authored inside indented context.
    """
    raw_lines = text.splitlines()
    shift = _common_indent(raw_lines)
    lines: list[tuple[int, str]] = []
    for original in raw_lines:
        raw = original[shift:] if shift and original[:shift].strip() == "" else original
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise MiniYamlError("tabs are not valid YAML indentation; use spaces")
        cleaned = _strip_comment(raw)
        if not cleaned.strip():
            continue
        if cleaned.strip() in ("---", "..."):
            continue
        lines.append((_indent_of(cleaned), cleaned.strip()))
    if not lines:
        return {}
    base = lines[0][0]
    if base != 0:
        raise MiniYamlError("document must start at column 0")
    value, consumed = _parse_block(lines, 0, 0)
    if consumed != len(lines):
        raise MiniYamlError(f"unparsed trailing content at line {consumed}")
    return value


def dump(data: Any, indent: int = 0) -> str:
    """Serialise Python data back to the same subset (used in receipts)."""
    pad = " " * indent
    if isinstance(data, dict):
        if not data:
            return pad + "{}\n"
        out = []
        for key, val in data.items():
            if isinstance(val, (dict, list)) and val:
                out.append(f"{pad}{key}:\n{dump(val, indent + 2)}")
            else:
                out.append(f"{pad}{key}: {_dump_scalar(val)}\n")
        return "".join(out)
    if isinstance(data, list):
        if not data:
            return pad + "[]\n"
        return "".join(f"{pad}- {dump(item, indent + 2)}" for item in data)
    return pad + _dump_scalar(data) + "\n"


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(c in text for c in ":#[]{}\"'\n") or text != text.strip():
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return text
