"""Subagent definitions: restricted ``*.md`` parsing and the immutable model.

Plan sections 5.6, 15.6-15.8. A subagent definition is a single ``.md`` file
whose head is a tiny frontmatter block::

    ---
    name: explorer
    description: Read-only search agent for broad fan-out searches.
    bundles: [fs]
    tools: ["-Write", "-Edit"]
    model: low
    max_iterations: 30
    context_tokens: 100000
    contexts: [root, subagent]
    ---

    System prompt for this agent.

This module deliberately does **not** use a YAML library (the runtime dependency
list is ``httpx``, ``msgspec``, and ``mcp``). It implements a *restricted*
grammar::

    frontmatter := "---" NEWLINE line* "---" NEWLINE
    line        := key ":" SP value | blank
    key         := [a-z][a-z0-9_-]*
    value       := scalar | flow-list
    flow-list   := "[" (item ("," item)*)? "]"

Everything outside that grammar is rejected: anchors, aliases, tags, block
scalars, flow mappings, comments, nested indentation, block sequences, typed
scalars, duplicate keys, unknown keys, and wrong value types. One bad line
rejects the whole declaration.

The supported keys are exactly ``name``, ``description``, ``bundles``,
``tools``, ``model``, ``max_iterations``, ``context_tokens``, and ``contexts``.
``name`` and ``description`` are required; ``bundles``, ``tools``, and
``contexts`` are the list-valued fields. Missing ``contexts`` preserves the
legacy subagent-only behavior. A ``tools`` item may carry a leading ``-`` to *exclude* a tool, which is
the only way a declaration can narrow a set -- declarations never grant.

``model`` is deliberately **opaque**: it is validated for shape only and is never
resolved, looked up, or sent to a network. A tier name (``low``/``medium``/
``high``), ``inherit``, a ``provider/model`` id, or a bare model id is accepted
verbatim; resolution belongs to the model registry, far above this layer.

An :class:`AgentDef` is frozen. It carries provenance, a whole-definition
fingerprint, and a bounded body snapshot taken from the *same* opened bytes as
the declaration, so a caller holding a pinned generation cannot observe a later
edit. The body is the subagent's system prompt and is disclosed only on demand.

Two roles -- ``explore`` and ``planner`` (plan section 15.7) -- have **no write
path at all**: they are structurally read-only. The model names the forbidden
tools and bundles here so the manager can diagnose a declaration that asks for
them and so any tool selection strips them regardless of what the file says.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..errors import NexusError

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DELIMITER",
    "FORBIDDEN_ROLE_BUNDLES",
    "FORBIDDEN_ROLE_TOOLS",
    "MAX_AGENT_FILE_BYTES",
    "MAX_BUNDLES",
    "MAX_CONTEXT_TOKENS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_FRONTMATTER_BYTES",
    "MAX_ITERATIONS",
    "MAX_LIST_ITEMS",
    "MAX_MODEL_CHARS",
    "MAX_NAME_CHARS",
    "MAX_TOOLS",
    "AGENT_CONTEXTS",
    "MODEL_INHERIT",
    "MODEL_TIERS",
    "MUTATING_FS_TOOLS",
    "READ_ONLY_ROLES",
    "READ_ONLY_TOOLS",
    "SHELL_TOOLS",
    "SOURCE_PRECEDENCE",
    "AgentDef",
    "AgentDiagnostic",
    "AgentDiagnosticCode",
    "AgentError",
    "AgentFile",
    "AgentIndex",
    "AgentIndexEntry",
    "AgentNotFoundError",
    "AgentOversizeError",
    "AgentParseError",
    "AgentProvenance",
    "AgentSeedError",
    "AgentSource",
    "AgentStaleError",
    "ParsedFrontmatter",
    "find_frontmatter_bounds",
    "is_model_tier",
    "parse_frontmatter",
    "read_agent_bytes",
    "read_agent_file",
    "read_declaration",
    "sanitize_description",
    "split_frontmatter",
    "validate_agent_name",
]


# ---------------------------------------------------------------------------
# Exception taxonomy (kept local so this packet owns exactly its own surface)
# ---------------------------------------------------------------------------


class AgentError(NexusError):
    """Base class for agent discovery, parsing, and seeding failures."""


class AgentParseError(AgentError, ValueError):
    """A definition's frontmatter is malformed or outside the restricted grammar."""


class AgentOversizeError(AgentParseError):
    """A definition, frontmatter block, or body exceeds its byte budget."""


class AgentStaleError(AgentError):
    """A snapshotted definition's on-disk file no longer matches its snapshot."""


class AgentSeedError(AgentError):
    """The built-in role definitions could not be seeded into a workspace."""


class AgentNotFoundError(AgentError):
    """No discovered subagent definition matches the requested name."""

    def __init__(self, name: object) -> None:
        self.name = name
        super().__init__(f"unknown agent {name!r}")


# ---------------------------------------------------------------------------
# Bounds and the restricted vocabulary
# ---------------------------------------------------------------------------

#: The literal delimiter line. It must be exactly ``---`` (trailing CR allowed).
DELIMITER = "---"
_DELIMITER_BYTES = b"---"
_BOM_BYTES = b"\xef\xbb\xbf"

#: Whole ``*.md`` cap. Matches the extension quarantine default (256 KiB).
MAX_AGENT_FILE_BYTES = 262_144
#: The declaration block is far smaller than the body and is separately capped.
MAX_FRONTMATTER_BYTES = 16_384
MAX_DESCRIPTION_CHARS = 2_048
MAX_NAME_CHARS = 64
MAX_MODEL_CHARS = 128
MAX_LIST_ITEMS = 64
MAX_TOOLS = MAX_LIST_ITEMS
MAX_BUNDLES = MAX_LIST_ITEMS
#: Integer clamps for the two numeric declarations.
MAX_ITERATIONS = 10_000
MAX_CONTEXT_TOKENS = 100_000_000

#: Bodies are read whole only on demand; the cap matches the whole-file cap.
DEFAULT_MAX_BODY_BYTES = MAX_AGENT_FILE_BYTES

#: Reserved tier names a ``model:`` may use. They are matched literally and are
#: never resolved here: this layer only checks the token's shape.
MODEL_TIERS = ("low", "medium", "high")
#: The sentinel that means "use the session's model".
MODEL_INHERIT = "inherit"

#: Tools that write to the filesystem; a read-only role may never hold one.
MUTATING_FS_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
#: The shell bundle tools; a read-only role may never hold one.
SHELL_TOOLS = frozenset({"Bash", "BashOutput", "KillShell"})
#: Everything the read-only roles are structurally denied.
FORBIDDEN_ROLE_TOOLS = SHELL_TOOLS | MUTATING_FS_TOOLS | frozenset({"Task"})
#: Bundles the read-only roles are structurally denied.
FORBIDDEN_ROLE_BUNDLES = frozenset({"shell"})
#: Roles with no write path at all, regardless of what their file declares.
READ_ONLY_ROLES = frozenset({"explore", "plan", "planner"})
#: Tools permitted to structurally read-only roles.
READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS"})
AGENT_CONTEXTS = frozenset({"root", "subagent"})

_NAME_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_NAME_CHARS - 1}}}\Z")
_BUNDLE_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
#: A tool item: a tool name with an optional leading ``-`` (an exclusion).
_TOOL_RE = re.compile(r"-?[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_INT_RE = re.compile(r"[0-9]+\Z")
_FLOAT_RE = re.compile(r"[0-9]+\.[0-9]+\Z")

#: Keys the grammar understands. Anything else is an error, never ignored.
_FIELDS = (
    "name",
    "description",
    "bundles",
    "tools",
    "model",
    "max_iterations",
    "context_tokens",
    "contexts",
    "profile",
)
_LIST_FIELDS = frozenset({"bundles", "tools", "contexts"})
_REQUIRED_FIELDS = frozenset({"name", "description"})

#: A value may not *begin* with one of these: each is a YAML structural marker.
_STRUCTURAL_PREFIXES = (
    "[",
    "{",
    "]",
    "}",
    "&",
    "*",
    "!",
    "|",
    ">",
    "%",
    "@",
    "`",
    ",",
)
#: YAML's implicitly typed scalars, rejected where an integer is required.
_TYPED_SCALARS = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "~", ".nan", ".inf", "-.inf"}
)


class AgentSource(StrEnum):
    """Where a definition was discovered, lowest to highest precedence."""

    BUILTIN = "builtin"
    USER = "user"
    WORKSPACE = "workspace"

    @property
    def precedence(self) -> int:
        return SOURCE_PRECEDENCE[self]


#: Higher precedence wins a same-name collision: workspace > user > builtin.
SOURCE_PRECEDENCE: Mapping[AgentSource, int] = {
    AgentSource.BUILTIN: 0,
    AgentSource.USER: 1,
    AgentSource.WORKSPACE: 2,
}


class AgentDiagnosticCode(StrEnum):
    """Machine-readable reasons a definition was skipped, shadowed, or flagged."""

    PARSE_ERROR = "parse_error"
    READ_ERROR = "read_error"
    OVERSIZE = "oversize"
    SHADOWED = "shadowed"
    CASE_COLLISION = "case_collision"
    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_BUNDLE = "unknown_bundle"
    FORBIDDEN_TOOL = "forbidden_tool"
    FORBIDDEN_BUNDLE = "forbidden_bundle"
    SEED_ERROR = "seed_error"
    DEPRECATED_PLANNER = "deprecated_planner"


@dataclass(frozen=True)
class AgentProvenance:
    """Where a definition came from and a hash of its declaration.

    ``declaration_sha256`` covers only the frontmatter bytes, so it is stable
    across body edits and can be computed without reading the body.
    """

    tier: AgentSource
    root: Path
    path: Path
    relpath: str
    declaration_sha256: str
    file_size: int


@dataclass(frozen=True)
class AgentDiagnostic:
    """A retained discovery diagnostic. Never silently dropped."""

    code: AgentDiagnosticCode
    message: str
    tier: AgentSource | None = None
    path: str = ""
    name: str | None = None
    shadowed_by: str | None = None


@dataclass(frozen=True)
class ParsedFrontmatter:
    """The validated declaration, plus the exact bytes it was parsed from.

    ``raw`` is the frontmatter *inner* content (between the delimiter lines) and
    is retained so provenance can hash the declaration without ever reading the
    body. ``excluded_tools`` holds the ``-name`` entries separately; if a name
    appears both positively and negatively, the exclusion wins.
    """

    name: str
    description: str
    bundles: tuple[str, ...]
    tools: tuple[str, ...]
    excluded_tools: tuple[str, ...]
    model: str | None
    max_iterations: int | None
    context_tokens: int | None
    contexts: tuple[str, ...]
    profile: str | None
    raw: bytes


@dataclass(frozen=True)
class AgentFile:
    """One ``*.md`` read exactly once, split into declaration and body.

    ``body`` is the *bounded* system-prompt snapshot taken during refresh.
    Declaration, body, and every hash derive from the same opened bytes, so a
    concurrent edit can never make them disagree (no TOCTOU split).
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
    when the delimiter is the final line). Raises :class:`AgentParseError` when
    the file does not open with ``---`` or the closing delimiter is absent.
    """
    if not data:
        raise AgentParseError("agent definition is empty")
    pos = len(_BOM_BYTES) if data.startswith(_BOM_BYTES) else 0

    eol = data.find(b"\n", pos)
    first_end = len(data) if eol == -1 else eol
    first = _strip_cr(data[pos:first_end])
    if first != _DELIMITER_BYTES:
        raise AgentParseError(
            "agent definition must begin with a '---' frontmatter delimiter"
        )
    if eol == -1:
        raise AgentParseError("frontmatter is missing its closing '---' delimiter")
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

    raise AgentParseError("frontmatter is missing its closing '---' delimiter")


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def validate_agent_name(name: object) -> str:
    """Return ``name`` when it matches the agent-name grammar, else raise."""
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise AgentParseError(
            "agent name must be 1-64 chars starting with a letter or digit and "
            "containing only letters, digits, '.', '_' or '-'"
        )
    return name


def _decode_frontmatter(raw: bytes) -> list[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentParseError("frontmatter is not valid UTF-8") from exc
    if "\x00" in text:
        raise AgentParseError("frontmatter contains a NUL byte")
    return [line.removesuffix("\r") for line in text.split("\n")]


def _reject_structural(value: str, field: str) -> None:
    if value and value[0] in _STRUCTURAL_PREFIXES:
        raise AgentParseError(
            f"{field}: value must not start with {value[0]!r}; only plain "
            "scalars and flow lists are supported"
        )
    if value.startswith(("- ", "? ", ": ")):
        raise AgentParseError(
            f"{field}: block sequences, complex keys, and nested mappings are "
            "not part of the restricted grammar"
        )


def _parse_scalar(value: str, field: str) -> str:
    if value == "":
        raise AgentParseError(f"{field}: value must not be empty")
    _reject_structural(value, field)
    return value


def _parse_list(
    value: str, field: str, token_re: re.Pattern[str], label: str
) -> tuple[str, ...]:
    if not (value.startswith("[") and value.endswith("]")):
        raise AgentParseError(
            f"{field}: expected a flow list like [{label}, {label}]"
        )
    inner = value[1:-1].strip()
    if inner == "":
        return ()
    items = [item.strip() for item in inner.split(",")]
    if any(item == "" for item in items):
        raise AgentParseError(f"{field}: contains an empty list item")
    if len(items) > MAX_LIST_ITEMS:
        raise AgentParseError(f"{field}: at most {MAX_LIST_ITEMS} items are allowed")
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        _reject_structural(item, field)
        if token_re.fullmatch(item) is None:
            raise AgentParseError(f"{field}: {item!r} is not a valid {label}")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
        elif field == "contexts":
            raise AgentParseError(f"{field}: duplicate list item {item!r}")
    return tuple(ordered)


def _parse_tools(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a ``tools`` flow list into positive tools and exclusions."""
    items = _parse_list(value, "tools", _TOOL_RE, "tool name")
    tools: list[str] = []
    excluded: list[str] = []
    for item in items:
        if item.startswith("-"):
            excluded.append(item[1:])
        else:
            tools.append(item)
    excluded_set = set(excluded)
    # An exclusion wins over an inclusion of the same name.
    tools = [name for name in tools if name not in excluded_set]
    return tuple(tools), tuple(excluded)


def _parse_model(value: str) -> str:
    model = _parse_scalar(value, "model")
    if len(model) > MAX_MODEL_CHARS:
        raise AgentParseError(f"model must be at most {MAX_MODEL_CHARS} chars")
    if any(ch.isspace() for ch in model):
        raise AgentParseError("model must not contain whitespace")
    return model


def _parse_int(value: str, field: str, max_value: int) -> int:
    text = _parse_scalar(value, field)
    if text.lower() in _TYPED_SCALARS or text[0] == "-":
        raise AgentParseError(f"{field}: expected a positive integer")
    if _FLOAT_RE.fullmatch(text):
        raise AgentParseError(f"{field}: must be an integer, not a float")
    if _INT_RE.fullmatch(text) is None:
        raise AgentParseError(f"{field}: expected a positive integer")
    number = int(text)
    if number < 1:
        raise AgentParseError(f"{field}: must be >= 1")
    if number > max_value:
        raise AgentParseError(f"{field}: must be <= {max_value}")
    return number


def _parse_raw(raw: bytes) -> ParsedFrontmatter:
    fields: dict[str, str] = {}
    for line_no, line in enumerate(_decode_frontmatter(raw), start=1):
        if line.strip() == "":
            continue
        if line[0] in (" ", "\t"):
            raise AgentParseError(
                f"frontmatter line {line_no}: indentation/nesting is not allowed"
            )
        if line[0] == "#":
            raise AgentParseError(
                f"frontmatter line {line_no}: comments are not allowed"
            )
        if line[0] in ("-", "?"):
            raise AgentParseError(
                f"frontmatter line {line_no}: block sequences and complex keys "
                "are not allowed"
            )
        key, sep, value = line.partition(":")
        if not sep:
            raise AgentParseError(
                f"frontmatter line {line_no}: expected 'key: value'"
            )
        if not key or key != key.strip() or not re.fullmatch(r"[a-z][a-z0-9_-]*", key):
            raise AgentParseError(
                f"frontmatter line {line_no}: invalid field name {key!r}"
            )
        if key not in _FIELDS:
            raise AgentParseError(f"unknown frontmatter field {key!r}")
        if key in fields:
            raise AgentParseError(f"duplicate frontmatter field {key!r}")
        fields[key] = value.strip()

    missing = sorted(_REQUIRED_FIELDS - fields.keys())
    if missing:
        raise AgentParseError(
            "missing required frontmatter field(s): " + ", ".join(missing)
        )

    name = validate_agent_name(_parse_scalar(fields["name"], "name"))
    description = _parse_scalar(fields["description"], "description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise AgentParseError(
            f"description must be at most {MAX_DESCRIPTION_CHARS} chars"
        )

    bundles = (
        _parse_list(fields["bundles"], "bundles", _BUNDLE_RE, "bundle name")
        if "bundles" in fields
        else ()
    )
    if "tools" in fields:
        tools, excluded_tools = _parse_tools(fields["tools"])
    else:
        tools, excluded_tools = (), ()
    model = _parse_model(fields["model"]) if "model" in fields else None
    max_iterations = (
        _parse_int(fields["max_iterations"], "max_iterations", MAX_ITERATIONS)
        if "max_iterations" in fields
        else None
    )
    context_tokens = (
        _parse_int(fields["context_tokens"], "context_tokens", MAX_CONTEXT_TOKENS)
        if "context_tokens" in fields
        else None
    )
    contexts = (
        _parse_list(fields["contexts"], "contexts", re.compile(r"[a-z]+\Z"), "context")
        if "contexts" in fields
        else ("subagent",)
    )
    if not contexts or len(set(contexts)) != len(contexts) or set(contexts) - AGENT_CONTEXTS:
        invalid = sorted(set(contexts) - AGENT_CONTEXTS)
        raise AgentParseError(
            "contexts must contain one or more of: root, subagent"
            + (f" (unknown: {', '.join(invalid)})" if invalid else "")
        )
    profile = (
        validate_agent_name(_parse_scalar(fields["profile"], "profile"))
        if "profile" in fields
        else None
    )

    return ParsedFrontmatter(
        name=name,
        description=description,
        bundles=bundles,
        tools=tools,
        excluded_tools=excluded_tools,
        model=model,
        max_iterations=max_iterations,
        context_tokens=context_tokens,
        contexts=contexts,
        profile=profile,
        raw=raw,
    )


def _as_bytes(source: str | bytes) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, str):
        return source.encode("utf-8")
    raise AgentParseError("frontmatter source must be str or bytes")


def parse_frontmatter(
    source: str | bytes,
    *,
    max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
) -> ParsedFrontmatter:
    """Parse a full definition (or a frontmatter-only document) strictly.

    Only the declaration is validated; a body after the closing delimiter is
    ignored and never decoded.
    """
    data = _as_bytes(source)
    start, end, _ = find_frontmatter_bounds(data)
    raw = data[start:end]
    if len(raw) > max_frontmatter_bytes:
        raise AgentOversizeError(
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
        raise AgentOversizeError(
            f"frontmatter exceeds {max_frontmatter_bytes} bytes"
        )
    return _parse_raw(raw), data[body_start:]


# ---------------------------------------------------------------------------
# Bounded file reads
# ---------------------------------------------------------------------------


def read_agent_bytes(
    path: str | Path,
    *,
    max_file_bytes: int = MAX_AGENT_FILE_BYTES,
) -> bytes:
    """Read a definition file once, bounded, so an oversize file fails closed."""
    agent_md = Path(path)
    try:
        with agent_md.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > max_file_bytes:
                raise AgentOversizeError(
                    f"agent definition is {size} bytes (limit {max_file_bytes})"
                )
            data = handle.read(max_file_bytes + 1)
    except OSError as exc:
        raise AgentParseError(f"cannot read {agent_md}: {exc}") from exc
    if len(data) > max_file_bytes:
        raise AgentOversizeError(
            f"agent definition is {len(data)} bytes (limit {max_file_bytes})"
        )
    return data


def read_declaration(
    path: str | Path,
    *,
    max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
    max_file_bytes: int = MAX_AGENT_FILE_BYTES,
) -> ParsedFrontmatter:
    """Read only the declaration, from the same bounded bytes used everywhere."""
    data = read_agent_bytes(path, max_file_bytes=max_file_bytes)
    start, end, _ = find_frontmatter_bounds(data)
    raw = data[start:end]
    if len(raw) > max_frontmatter_bytes:
        raise AgentOversizeError(
            f"frontmatter exceeds {max_frontmatter_bytes} bytes"
        )
    return _parse_raw(raw)


def read_agent_file(
    path: str | Path,
    *,
    max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
    max_file_bytes: int = MAX_AGENT_FILE_BYTES,
) -> AgentFile:
    """Read a whole definition once and return declaration plus body snapshot."""
    data = read_agent_bytes(path, max_file_bytes=max_file_bytes)
    start, end, body_start = find_frontmatter_bounds(data)
    raw = data[start:end]
    if len(raw) > max_frontmatter_bytes:
        raise AgentOversizeError(
            f"frontmatter exceeds {max_frontmatter_bytes} bytes"
        )
    body = data[body_start:]
    return AgentFile(
        parsed=_parse_raw(raw),
        body=body,
        file_size=len(data),
        file_sha256=hashlib.sha256(data).hexdigest(),
        declaration_sha256=hashlib.sha256(raw).hexdigest(),
        body_sha256=hashlib.sha256(body).hexdigest(),
    )


def sanitize_description(
    text: object, *, max_chars: int = MAX_DESCRIPTION_CHARS
) -> str:
    """Collapse a description to a single, safe, bounded index line."""
    raw = str(text)
    cleaned = "".join(
        " " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in raw
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "\u2026"
    return cleaned


# ---------------------------------------------------------------------------
# The immutable definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentDef:
    """A discovered, immutable subagent definition plus a bounded body snapshot.

    The manager snapshots the system-prompt body during refresh, so a
    generation-pinned definition keeps serving the bytes it was discovered with
    even after the file on disk changes. When ``snapshotted`` is false (a
    hand-built :class:`AgentDef`, e.g. in a unit test) the body is read from disk
    on demand instead.
    """

    name: str
    description: str
    provenance: AgentProvenance
    bundles: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    excluded_tools: tuple[str, ...] = ()
    model: str | None = None
    max_iterations: int | None = None
    context_tokens: int | None = None
    contexts: tuple[str, ...] = ("subagent",)
    profile: str | None = None
    # -- refresh-time snapshot (progressive disclosure keeps it out of the index)
    body: bytes | None = None
    body_sha256: str = ""
    body_size: int = 0
    file_sha256: str = ""
    snapshotted: bool = False

    # -- provenance conveniences ------------------------------------------

    @property
    def source(self) -> AgentSource:
        return self.provenance.tier

    @property
    def path(self) -> Path:
        return self.provenance.path

    @property
    def directory(self) -> Path:
        return self.provenance.path.parent

    @property
    def declaration_sha256(self) -> str:
        return self.provenance.declaration_sha256

    @property
    def has_body_snapshot(self) -> bool:
        return self.body is not None

    @property
    def model_is_tier(self) -> bool:
        """Whether ``model`` is a reserved tier name (never resolved here)."""
        return self.model in MODEL_TIERS

    @property
    def model_is_inherit(self) -> bool:
        return self.model == MODEL_INHERIT

    @property
    def read_only(self) -> bool:
        """Whether this definition is structurally denied a write path.

        The three seeded roles include ``explore`` and ``planner``; membership is
        by *declared name*, not by tier, so a workspace copy cannot escape the
        restriction by shadowing the built-in file.
        """
        return self.name.casefold() in READ_ONLY_ROLES

    def eligible_in(self, context: str) -> bool:
        """Whether the declaration can be selected in a root/subagent context."""
        return context in self.contexts

    def forbidden_role_declarations(self) -> tuple[str, ...]:
        """Declared positive tools/bundles a read-only role may never hold."""
        if not self.read_only:
            return ()
        found: list[str] = [
            name for name in self.tools if name in FORBIDDEN_ROLE_TOOLS
        ]
        found.extend(
            bundle for bundle in self.bundles if bundle in FORBIDDEN_ROLE_BUNDLES
        )
        return tuple(dict.fromkeys(found))

    # -- stable fingerprint ------------------------------------------------

    def fingerprint(self) -> str:
        """A whole-definition content digest: declaration, body, role flags.

        Stable across a rebuild that reproduced equal content, so an unchanged
        definition reuses its object and never churns a manifest diff. The digest
        deliberately ignores filesystem paths, timestamps, and the discovery
        generation; it changes exactly when the definition's *meaning* changes.
        """
        payload = {
            "name": self.name,
            "description": self.description,
            "bundles": list(self.bundles),
            "tools": list(self.tools),
            "excluded_tools": list(self.excluded_tools),
            "model": self.model,
            "max_iterations": self.max_iterations,
            "context_tokens": self.context_tokens,
            "contexts": list(self.contexts),
            "profile": self.profile,
            "read_only": self.read_only,
            "declaration": self.provenance.declaration_sha256,
            "body": self.body_sha256,
            "file": self.file_sha256,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    # -- progressive disclosure -------------------------------------------

    def load_body(self, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> str:
        """Return the system-prompt body. Call this only when spawning.

        For a snapshotted definition the bytes captured at refresh are returned,
        so a later edit is invisible under the pinned generation. A hand-built
        definition falls back to reading from disk.
        """
        if self.body is not None:
            data = self.body
        else:
            data = read_agent_bytes(self.provenance.path, max_file_bytes=max_bytes)
            _, _, body_start = find_frontmatter_bounds(data)
            data = data[body_start:]
        if len(data) > max_bytes:
            raise AgentOversizeError(
                f"agent {self.name!r} body exceeds {max_bytes} bytes"
            )
        if b"\x00" in data:
            raise AgentParseError(f"agent {self.name!r} body contains a NUL byte")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentParseError(
                f"agent {self.name!r} body is not valid UTF-8"
            ) from exc

    def verify_snapshot(self) -> None:
        """Refuse when the on-disk file no longer matches the snapshot.

        This is the explicit staleness check: a caller that wants to detect a
        file edited *after* discovery can call it and receive
        :class:`AgentStaleError` instead of silently reading newer bytes.
        """
        if not self.snapshotted or not self.file_sha256:
            return
        path = self.provenance.path
        try:
            with path.open("rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise AgentStaleError(
                f"agent {self.name!r} snapshot cannot be verified: {exc}"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != self.file_sha256:
            raise AgentStaleError(
                f"agent {self.name!r} changed on disk after discovery"
            )

    def sanitized_description(
        self, *, max_chars: int = MAX_DESCRIPTION_CHARS
    ) -> str:
        """The single-line, safe description used by the index."""
        return sanitize_description(self.description, max_chars=max_chars)

    def index_line(self, *, max_chars: int = MAX_DESCRIPTION_CHARS) -> str:
        """The single sanitized ``name: description`` line."""
        return f"{self.name}: {self.sanitized_description(max_chars=max_chars)}"


@dataclass(frozen=True)
class AgentIndexEntry:
    """One sanitized index row. Carries no body, path, or tool information."""

    name: str
    description: str
    source: AgentSource
    model: str | None = None
    read_only: bool = False

    def line(self) -> str:
        return f"{self.name}: {self.description}"


@dataclass(frozen=True)
class AgentIndex:
    """An immutable snapshot of discovery: entries plus retained diagnostics."""

    entries: tuple[AgentIndexEntry, ...]
    diagnostics: tuple[AgentDiagnostic, ...] = ()
    generation: int = 0

    def render(self, *, max_chars: int | None = None) -> str:
        """Render whole ``name: description`` lines, optionally under a budget."""
        lines = [entry.line() for entry in self.entries]
        if max_chars is None:
            return "\n".join(lines)
        out: list[str] = []
        total = 0
        for line in lines:
            extra = len(line) + (1 if out else 0)
            if total + extra > max_chars:
                break
            out.append(line)
            total += extra
        return "\n".join(out)
