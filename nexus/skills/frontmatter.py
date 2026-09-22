"""Restricted, dependency-free ``SKILL.md`` frontmatter parsing (plan 5.4).

A skill is a directory containing ``SKILL.md`` whose head is a tiny frontmatter
block::

    ---
    name: risk3-docker-testing
    description: Run DS-pack tests locally inside the Risk3 container. Use when...
    allowed-tools: [Bash, Read, Glob]
    bundles: [fs, shell]
    model: inherit
    version: 1
    ---

    # body: loaded only when invoked

This module deliberately does **not** use a YAML library (the runtime dependency
list is ``httpx`` and ``msgspec``, and this parser is pure stdlib). It implements
a *restricted* grammar instead::

    frontmatter := "---" NEWLINE line* "---" NEWLINE
    line        := key ":" SP value | blank
    key         := [a-z][a-z0-9_-]*
    value       := scalar | flow-list
    flow-list   := "[" (item ("," item)*)? "]"

Everything outside that grammar is rejected: anchors, aliases, tags, block
scalars, flow mappings, comments, nested indentation, block sequences, directives,
typed scalars, duplicate keys, unknown keys, and wrong value types. The parser is
all-or-nothing: one bad line rejects the whole declaration.

The six supported keys are exactly ``name``, ``description``, ``allowed-tools``,
``bundles``, ``model``, and ``version``; ``allowed-tools`` and ``bundles`` are
the only list-valued fields. ``name`` and ``description`` are required.

``model`` is deliberately **opaque**: it is validated for shape only and is never
resolved, looked up, or sent to a network. Any of a tier name (``low`` /
``medium`` / ``high``), ``inherit``, a ``provider/model`` id, or a bare model id
is accepted verbatim; resolution belongs to the model registry, far above this
layer.

The parser also exposes :func:`find_frontmatter_bounds`, which the manager uses
to read **only** the declaration, and :func:`read_skill_file`, which reads a
``SKILL.md`` exactly once so its declaration and body snapshot come from the same
opened bytes (no stat/open race).
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import SkillOversizeError, SkillParseError

__all__ = [
    "DELIMITER",
    "MAX_ALLOWED_TOOLS",
    "MAX_BUNDLES",
    "MAX_DESCRIPTION_CHARS",
    "MAX_FRONTMATTER_BYTES",
    "MAX_MODEL_CHARS",
    "MAX_NAME_CHARS",
    "MAX_SKILL_FILE_BYTES",
    "MAX_VERSION_CHARS",
    "MODEL_INHERIT",
    "MODEL_TIERS",
    "ParsedFrontmatter",
    "SkillFile",
    "find_frontmatter_bounds",
    "is_model_tier",
    "parse_frontmatter",
    "read_declaration",
    "read_skill_file",
    "sanitize_description",
    "split_frontmatter",
    "validate_skill_name",
]

#: The literal delimiter line. It must be exactly ``---`` (trailing CR allowed).
DELIMITER = "---"
_DELIMITER_BYTES = b"---"
_BOM_BYTES = b"\xef\xbb\xbf"

#: Whole ``SKILL.md`` cap. Matches the extension quarantine default (256 KiB).
MAX_SKILL_FILE_BYTES = 262_144
#: The declaration block is far smaller than the body and is separately capped.
MAX_FRONTMATTER_BYTES = 16_384
MAX_DESCRIPTION_CHARS = 2_048
MAX_NAME_CHARS = 64
MAX_MODEL_CHARS = 128
MAX_VERSION_CHARS = 64
MAX_LIST_ITEMS = 64
MAX_ALLOWED_TOOLS = MAX_LIST_ITEMS
MAX_BUNDLES = MAX_LIST_ITEMS

#: Reserved tier names a ``model:`` may use. They are matched literally and are
#: never resolved here: this layer only checks the token's shape.
MODEL_TIERS = ("low", "medium", "high")
#: The sentinel that means "use the session's model".
MODEL_INHERIT = "inherit"

_NAME_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_NAME_CHARS - 1}}}\Z")
_TOOL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_BUNDLE_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_INT_RE = re.compile(r"[0-9]+\Z")
_FLOAT_RE = re.compile(r"[0-9]+\.[0-9]+\Z")

#: Keys the grammar understands. Anything else is an error, never ignored.
_FIELDS = (
    "name",
    "description",
    "allowed-tools",
    "bundles",
    "model",
    "version",
)
_LIST_FIELDS = frozenset({"allowed-tools", "bundles"})
_REQUIRED_FIELDS = frozenset({"name", "description"})

#: A value may not *begin* with one of these: each is a YAML structural marker.
_STRUCTURAL_PREFIXES = ("[", "{", "]", "}", "&", "*", "!", "|", ">", "%", "@", "`", ",")
#: YAML's implicitly typed scalars. ``version`` alone rejects these explicitly.
_TYPED_SCALARS = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "~", ".nan", ".inf", "-.inf"}
)


@dataclass(frozen=True)
class ParsedFrontmatter:
    """The validated declaration, plus the exact bytes it was parsed from.

    ``raw`` is the frontmatter *inner* content (between the delimiter lines) and
    is retained so provenance can hash the declaration without ever reading the
    body.
    """

    name: str
    description: str
    allowed_tools: tuple[str, ...]
    bundles: tuple[str, ...]
    model: str | None
    version: str
    raw: bytes


@dataclass(frozen=True)
class SkillFile:
    """One ``SKILL.md`` read exactly once, split into declaration and body.

    ``body`` is the *bounded* snapshot taken during refresh. Because the whole
    file is read through a single open handle and every field is derived from
    those same bytes, a concurrent edit can never make the declared size, the
    declaration hash, and the body snapshot disagree (no TOCTOU split).
    """

    parsed: ParsedFrontmatter
    body: bytes
    file_size: int
    file_sha256: str
    declaration_sha256: str
    body_sha256: str

    @property
    def body_size(self) -> int:
        return len(self.body)


def is_model_tier(value: object) -> bool:
    """Whether ``value`` is one of the reserved tier names (never resolves it)."""
    return isinstance(value, str) and value in MODEL_TIERS


# ---------------------------------------------------------------------------
# Delimiter location (byte-oriented so the body is never decoded)
# ---------------------------------------------------------------------------


def _strip_cr(line: bytes) -> bytes:
    return line[:-1] if line.endswith(b"\r") else line


def find_frontmatter_bounds(data: bytes) -> tuple[int, int, int]:
    """Return ``(content_start, content_end, body_start)`` byte offsets.

    ``content_start``/``content_end`` bound the frontmatter inner bytes;
    ``body_start`` points just past the closing delimiter line (or ``len(data)``
    when the delimiter is the final line). Raises :class:`SkillParseError` when
    the file does not open with ``---`` or the closing delimiter is absent.
    """
    if not data:
        raise SkillParseError("SKILL.md is empty")
    pos = len(_BOM_BYTES) if data.startswith(_BOM_BYTES) else 0

    eol = data.find(b"\n", pos)
    first_end = len(data) if eol == -1 else eol
    first = _strip_cr(data[pos:first_end])
    if first != _DELIMITER_BYTES:
        raise SkillParseError(
            "SKILL.md must begin with a '---' frontmatter delimiter"
        )
    if eol == -1:
        raise SkillParseError("frontmatter is missing its closing '---' delimiter")
    content_start = eol + 1

    idx = content_start
    n = len(data)
    while idx <= n:
        line_end = data.find(b"\n", idx)
        has_newline = line_end != -1
        if not has_newline:
            line_end = n
        line = _strip_cr(data[idx:line_end])
        if line == _DELIMITER_BYTES:
            body_start = line_end + 1 if has_newline else n
            return content_start, idx, body_start
        if not has_newline:
            break
        idx = line_end + 1

    raise SkillParseError("frontmatter is missing its closing '---' delimiter")


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def validate_skill_name(name: object) -> str:
    """Return ``name`` when it matches the skill-name grammar, else raise."""
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise SkillParseError(
            "skill name must be 1-64 chars starting with a letter or digit and "
            "containing only letters, digits, '.', '_' or '-'"
        )
    return name


def _decode_frontmatter(raw: bytes) -> list[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillParseError("frontmatter is not valid UTF-8") from exc
    if "\x00" in text:
        raise SkillParseError("frontmatter contains a NUL byte")
    return [line.removesuffix("\r") for line in text.split("\n")]


def _reject_structural(value: str, field: str) -> None:
    if value and value[0] in _STRUCTURAL_PREFIXES:
        raise SkillParseError(
            f"{field}: value must not start with {value[0]!r}; only plain "
            "scalars and flow lists are supported"
        )
    if value.startswith(("- ", "? ", ": ")):
        raise SkillParseError(
            f"{field}: block sequences, complex keys, and nested mappings are "
            "not part of the restricted grammar"
        )


def _parse_scalar(value: str, field: str) -> str:
    if value == "":
        raise SkillParseError(f"{field}: value must not be empty")
    _reject_structural(value, field)
    return value


def _parse_list(value: str, field: str, token_re: re.Pattern[str], label: str) -> tuple[str, ...]:
    if not (value.startswith("[") and value.endswith("]")):
        raise SkillParseError(
            f"{field}: expected a flow list like [{label}, {label}]"
        )
    inner = value[1:-1].strip()
    if inner == "":
        return ()
    items = [item.strip() for item in inner.split(",")]
    if any(item == "" for item in items):
        raise SkillParseError(f"{field}: contains an empty list item")
    if len(items) > MAX_LIST_ITEMS:
        raise SkillParseError(f"{field}: at most {MAX_LIST_ITEMS} items are allowed")
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        _reject_structural(item, field)
        if token_re.fullmatch(item) is None:
            raise SkillParseError(f"{field}: {item!r} is not a valid {label}")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return tuple(ordered)


def _parse_version(value: str) -> str:
    if value == "":
        raise SkillParseError("version: value must not be empty")
    _reject_structural(value, "version")
    if value[0] == "-" or value.lower() in _TYPED_SCALARS:
        raise SkillParseError("version must be a number or a simple version token")
    if _FLOAT_RE.fullmatch(value):
        raise SkillParseError(
            "version must be an integer or a token like 'v2', not a float"
        )
    if _INT_RE.fullmatch(value):
        if len(str(int(value))) > MAX_VERSION_CHARS:
            raise SkillParseError("version is too long")
        return str(int(value))
    if _VERSION_RE.fullmatch(value) and len(value) <= MAX_VERSION_CHARS:
        return value
    raise SkillParseError("version must be a number or a simple version token")


def _parse_model(value: str) -> str:
    model = _parse_scalar(value, "model")
    if len(model) > MAX_MODEL_CHARS:
        raise SkillParseError(f"model must be at most {MAX_MODEL_CHARS} chars")
    if any(ch.isspace() for ch in model):
        raise SkillParseError("model must not contain whitespace")
    return model


def _parse_raw(raw: bytes) -> ParsedFrontmatter:
    fields: dict[str, str] = {}
    for line_no, line in enumerate(_decode_frontmatter(raw), start=1):
        if line.strip() == "":
            continue
        if line[0] in (" ", "\t"):
            raise SkillParseError(
                f"frontmatter line {line_no}: indentation/nesting is not allowed"
            )
        if line[0] == "#":
            raise SkillParseError(
                f"frontmatter line {line_no}: comments are not allowed"
            )
        if line[0] in ("-", "?"):
            raise SkillParseError(
                f"frontmatter line {line_no}: block sequences and complex keys "
                "are not allowed"
            )
        key, sep, value = line.partition(":")
        if not sep:
            raise SkillParseError(
                f"frontmatter line {line_no}: expected 'key: value'"
            )
        if not key or key != key.strip() or not re.fullmatch(r"[a-z][a-z0-9_-]*", key):
            raise SkillParseError(
                f"frontmatter line {line_no}: invalid field name {key!r}"
            )
        if key not in _FIELDS:
            raise SkillParseError(f"unknown frontmatter field {key!r}")
        if key in fields:
            raise SkillParseError(f"duplicate frontmatter field {key!r}")
        fields[key] = value.strip()

    missing = sorted(_REQUIRED_FIELDS - fields.keys())
    if missing:
        raise SkillParseError(
            "missing required frontmatter field(s): " + ", ".join(missing)
        )

    name = validate_skill_name(_parse_scalar(fields["name"], "name"))
    description = _parse_scalar(fields["description"], "description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillParseError(
            f"description must be at most {MAX_DESCRIPTION_CHARS} chars"
        )

    if "allowed-tools" in fields:
        allowed_tools = _parse_list(
            fields["allowed-tools"], "allowed-tools", _TOOL_RE, "tool name"
        )
    else:
        allowed_tools = ()
    if "bundles" in fields:
        bundles = _parse_list(fields["bundles"], "bundles", _BUNDLE_RE, "bundle name")
    else:
        bundles = ()
    model = _parse_model(fields["model"]) if "model" in fields else None
    version = _parse_version(fields["version"]) if "version" in fields else "1"

    return ParsedFrontmatter(
        name=name,
        description=description,
        allowed_tools=allowed_tools,
        bundles=bundles,
        model=model,
        version=version,
        raw=raw,
    )


def _as_bytes(source: str | bytes) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, str):
        return source.encode("utf-8")
    raise SkillParseError("frontmatter source must be str or bytes")


def parse_frontmatter(
    source: str | bytes,
    *,
    max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
) -> ParsedFrontmatter:
    """Parse a full ``SKILL.md`` (or a frontmatter-only document) strictly.

    Only the declaration is validated; a body after the closing delimiter is
    ignored and never decoded.
    """
    data = _as_bytes(source)
    start, end, _ = find_frontmatter_bounds(data)
    raw = data[start:end]
    if len(raw) > max_frontmatter_bytes:
        raise SkillOversizeError(
            f"frontmatter exceeds {max_frontmatter_bytes} bytes"
        )
    return _parse_raw(raw)


def split_frontmatter(
    source: str | bytes,
    *,
    max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
) -> tuple[ParsedFrontmatter, bytes]:
    """Parse the declaration and return it with the raw body bytes."""
    data = _as_bytes(source)
    start, end, body_start = find_frontmatter_bounds(data)
    raw = data[start:end]
    if len(raw) > max_frontmatter_bytes:
        raise SkillOversizeError(
            f"frontmatter exceeds {max_frontmatter_bytes} bytes"
        )
    return _parse_raw(raw), data[body_start:]


def read_declaration(
    path: str | Path,
    *,
    max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
    max_file_bytes: int = MAX_SKILL_FILE_BYTES,
) -> ParsedFrontmatter:
    """Read only the declaration from ``path`` without loading the body.

    The file is opened **once**; its size comes from ``fstat`` on that same
    handle and only the declaration head is read, so a stat/open race cannot make
    the size check disagree with the bytes that were parsed.
    """
    skill_md = Path(path)
    try:
        with skill_md.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > max_file_bytes:
                raise SkillOversizeError(
                    f"SKILL.md is {size} bytes (limit {max_file_bytes})"
                )
            chunk = handle.read(max_frontmatter_bytes)
    except OSError as exc:
        raise SkillParseError(f"cannot read {skill_md}: {exc}") from exc
    try:
        start, end, _ = find_frontmatter_bounds(chunk)
    except SkillParseError as exc:
        if size > max_frontmatter_bytes:
            raise SkillOversizeError(
                f"frontmatter exceeds {max_frontmatter_bytes} bytes"
            ) from exc
        raise
    return _parse_raw(chunk[start:end])


def read_skill_file(
    path: str | Path,
    *,
    max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
    max_file_bytes: int = MAX_SKILL_FILE_BYTES,
) -> SkillFile:
    """Read a whole ``SKILL.md`` once and return declaration plus body snapshot.

    Unlike :func:`read_declaration`, this reads the (bounded) body too, because
    the manager snapshots it during refresh for generation-stable invocation. The
    declaration, the body, and every hash are derived from the *same* opened
    bytes, which is what makes the snapshot TOCTOU-safe.
    """
    skill_md = Path(path)
    try:
        with skill_md.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > max_file_bytes:
                raise SkillOversizeError(
                    f"SKILL.md is {size} bytes (limit {max_file_bytes})"
                )
            data = handle.read(max_file_bytes + 1)
    except OSError as exc:
        raise SkillParseError(f"cannot read {skill_md}: {exc}") from exc
    if len(data) > max_file_bytes:
        raise SkillOversizeError(
            f"SKILL.md is {len(data)} bytes (limit {max_file_bytes})"
        )
    start, end, body_start = find_frontmatter_bounds(data)
    raw = data[start:end]
    if len(raw) > max_frontmatter_bytes:
        raise SkillOversizeError(
            f"frontmatter exceeds {max_frontmatter_bytes} bytes"
        )
    body = data[body_start:]
    return SkillFile(
        parsed=_parse_raw(raw),
        body=body,
        file_size=len(data),
        file_sha256=hashlib.sha256(data).hexdigest(),
        declaration_sha256=hashlib.sha256(raw).hexdigest(),
        body_sha256=hashlib.sha256(body).hexdigest(),
    )


def sanitize_description(text: object, *, max_chars: int = MAX_DESCRIPTION_CHARS) -> str:
    """Collapse a description to a single, safe, bounded index line.

    Control characters become spaces, whitespace runs collapse to one space, and
    an over-long description is truncated at the character budget with an ellipsis.
    """
    raw = str(text)
    cleaned = "".join(
        " " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in raw
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "\u2026"
    return cleaned
