"""Lenient `SKILL.md` frontmatter.

A skill file starts with a small YAML-like block between `---` lines. Public
skills are written for several tools, so this parser accepts what they use and
never turns a skill away for an extra key:

- `key: value` with plain, `'single'` or `"double"` quoted scalars
- folded and literal block scalars (`>`, `|`, with `-` or `+`)
- flow lists (`[a, b]`), block lists (`- a`) and, for `allowed-tools`, a
  space- or comma-separated string
- one level of nested mapping (`metadata:` with indented `key: value` lines),
  kept as a dict of strings
- full-line comments

Only `name`, `description` and `allowed-tools` mean something to Nexus. Any
other key is kept in `extra` and reported as a warning, not rejected. Hard
errors are limited to what makes a skill unusable: no frontmatter block, no
description, an invalid name, a file that is too large or not UTF-8.

Adapted from old Nexus `nexus/skills/frontmatter.py` (a strict six-key grammar
that turned away public skills; see PLAN.md ledger, F).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_FILE_BYTES = 256 * 1024
MAX_DESCRIPTION_CHARS = 1024
MAX_NAME_CHARS = 64
_NAME = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_NAME_CHARS - 1}}}\Z")
_KEY = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s+(.*)|\s*)\Z")
_KNOWN = ("name", "description", "allowed-tools")


class SkillParseError(ValueError):
    """The file cannot be used as a skill."""


@dataclass(frozen=True, slots=True)
class Parsed:
    name: str | None
    description: str
    allowed_tools: tuple[str, ...]
    extra: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    body: str = ""


def valid_name(name: object) -> bool:
    return isinstance(name, str) and bool(_NAME.fullmatch(name))


def sanitize(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """One line, no control characters, at most `limit` characters."""
    line = " ".join("".join(c if c.isprintable() or c.isspace() else " " for c in text).split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def split(text: str) -> tuple[list[str], str]:
    """The frontmatter lines and the body. Raises SkillParseError without a block."""
    text = text.removeprefix("\ufeff")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n").rstrip() != "---":
        raise SkillParseError("SKILL.md must start with a `---` frontmatter block")
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").rstrip() == "---":
            return [x.rstrip("\r\n") for x in lines[1:i]], "".join(lines[i + 1 :]).lstrip("\r\n")
    raise SkillParseError("the frontmatter block is not closed by a `---` line")


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), inner)
        return inner.replace("''", "'")
    # An unquoted ` #` starts a comment.
    return re.split(r"\s#", value, maxsplit=1)[0].strip()


def _flow_list(value: str) -> list[str]:
    inner = value.strip()[1:-1]
    return [_scalar(x) for x in inner.split(",") if x.strip()]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_scalar(style: str, lines: list[str]) -> str:
    """`>` folds lines into spaces and `|` keeps them; `-` strips and `+` keeps trailing newlines."""
    pad = min((_indent(x) for x in lines if x.strip()), default=0)
    rows = [x[pad:] if x.strip() else "" for x in lines]
    if style.startswith("|"):
        text = "\n".join(rows)
    else:
        paragraphs: list[str] = []
        current: list[str] = []
        for row in rows:
            if row:
                current.append(row)
            elif current:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))
        text = "\n".join(paragraphs)
    text = text.rstrip("\n")
    return text + ("\n" if style.endswith("+") else "")


def parse_block(lines: list[str]) -> dict[str, Any]:
    """Top-level keys to a string, a list of strings or a dict of strings."""
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line):
            continue  # stray continuation of something we do not read
        match = _KEY.match(line.strip())
        if match is None:
            raise SkillParseError(f"cannot read frontmatter line {line.strip()[:60]!r}")
        key, raw = match.group(1), (match.group(2) or "").strip()
        nested: list[str] = []
        while i < len(lines) and (not lines[i].strip() or _indent(lines[i])):
            nested.append(lines[i])
            i += 1
        while nested and not nested[-1].strip():
            nested.pop()
        if re.fullmatch(r"[>|][+-]?\d?", raw):
            result[key] = _block_scalar(raw.rstrip("0123456789"), nested)
        elif raw.startswith("["):
            result[key] = _flow_list(raw) if raw.endswith("]") else raw
        elif raw:
            # A plain scalar may continue on indented lines, as YAML allows.
            text = _scalar(raw)
            if nested and raw[0] not in "\"'":
                text = " ".join([text, *[x.strip() for x in nested if x.strip() and not x.lstrip().startswith("#")]])
            result[key] = text
        elif nested:
            body = [x for x in nested if x.strip() and not x.lstrip().startswith("#")]
            if body and all(x.lstrip().startswith("- ") or x.strip() == "-" for x in body):
                result[key] = [_scalar(x.lstrip()[1:]) for x in body]
            else:
                mapping: dict[str, str] = {}
                for x in body:
                    sub = _KEY.match(x.strip())
                    if sub:
                        mapping[sub.group(1)] = _scalar(sub.group(2) or "")
                result[key] = mapping
        else:
            result[key] = ""
    return result


def _tools(value: Any) -> tuple[str, ...]:
    items = value if isinstance(value, list) else re.split(r"[\s,]+", value) if isinstance(value, str) else []
    seen: list[str] = []
    for item in items:
        item = str(item).strip()
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def parse(data: bytes | str) -> Parsed:
    if isinstance(data, bytes):
        if len(data) > MAX_FILE_BYTES:
            raise SkillParseError(f"SKILL.md is {len(data)} bytes; the limit is {MAX_FILE_BYTES}")
        if b"\x00" in data:
            raise SkillParseError("SKILL.md contains a NUL byte")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillParseError("SKILL.md is not valid UTF-8") from exc
    else:
        text = data
    head, body = split(text)
    meta = parse_block(head)
    warnings: list[str] = []
    name = meta.get("name")
    if name is not None and (not isinstance(name, str) or not valid_name(name)):
        raise SkillParseError(
            f"name {name!r} is invalid: use letters, digits, '.', '_' and '-', up to {MAX_NAME_CHARS} characters")
    description = meta.get("description")
    if not isinstance(description, str) or not sanitize(description):
        raise SkillParseError("description is required (what the skill does and when to use it)")
    extra = {k: v for k, v in meta.items() if k not in _KNOWN}
    for key in extra:
        warnings.append(f"unknown key {key!r} is kept but Nexus does not use it")
    if len(sanitize(description, 10**6)) > MAX_DESCRIPTION_CHARS:
        warnings.append(f"description is longer than {MAX_DESCRIPTION_CHARS} characters; the index shows the start")
    return Parsed(name=name, description=sanitize(description), allowed_tools=_tools(meta.get("allowed-tools", "")),
                  extra=extra, warnings=tuple(warnings), body=body)
