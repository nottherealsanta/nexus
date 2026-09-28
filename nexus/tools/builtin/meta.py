"""The Phase 4 meta builtins: reload, inspect, and author extensions.

Bundle ``meta`` (plan sections 5.3, 6.3-6.5):

* :data:`RELOAD_EXTENSIONS_SPEC` / :func:`reload_extensions` -- the explicit
  reload trigger. It is a mutating, exclusive tool that calls the serialized
  :class:`~nexus.tools.spec.ExtensionServiceView` (a real ``ExtensionManager``)
  with ``trigger="tool"`` and returns the concise diff, or a sanitized,
  actionable error. It never touches the manifest itself.
* :data:`LIST_EXTENSIONS_SPEC` / :func:`list_extensions` -- a read-only,
  side-effect-free view of the live world: active tools/modules, discovered
  skills with their scope, and quarantined/shadowed diagnostics. It exposes only
  names, provenance, hashes, and sanitized messages -- never a source body,
  credential, or raw path beyond the bounded provenance string.
* :data:`WRITE_TOOL_SPEC` / :func:`write_tool` -- author one extension file at
  ``.nexus/tools/<name>.py``. It validates a *simple* filename (no traversal,
  separators, symlink target, reserved name, or leading underscore), bounds the
  UTF-8 payload, and writes atomically (temp + fsync + replace) under the
  configured filesystem roots. It deliberately does **not** auto-reload: the
  model must call ``ReloadExtensions`` so the diff is explicit and reviewable.

Every tool reaches the managers only through the narrow
:class:`~nexus.tools.spec.ExtensionServiceView`/``SkillServiceView`` seams on
:class:`~nexus.tools.spec.ToolContext`; nothing here imports ``nexus.runtime``.
"""
from __future__ import annotations

import contextlib
import inspect
import re
from collections.abc import Mapping
from typing import Any

from ...errors import ToolError
from ...ext.quarantine import sanitize_text
from ..bundles import BUNDLES
from ..permissions import PathSecurityError
from ..spec import (
    ExtensionServiceView,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)
from .read import (
    _check_cancel,
    _error,
    _guard,
    _opt_bool,
    _require_text,
)
from .write import atomic_write_bytes

__all__ = [
    "LIST_EXTENSIONS_SPEC",
    "MAX_TOOL_FILENAME_BYTES",
    "RELOAD_EXTENSIONS_SPEC",
    "RESERVED_TOOL_STEMS",
    "TOOLS_SUBDIR",
    "WRITE_TOOL_SPEC",
    "list_extensions",
    "reload_extensions",
    "run_list_extensions",
    "run_reload_extensions",
    "run_write_tool",
    "validate_tool_filename",
    "write_tool",
]

#: The workspace-relative directory extension tools live in.
TOOLS_SUBDIR = ".nexus/tools"
#: A filename (not a path) is bounded so it cannot be used to smuggle a path.
MAX_TOOL_FILENAME_BYTES = 255
#: A simple Python module filename: one leading letter, then word characters.
_TOOL_FILENAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,126}\.py\Z")
#: Windows device names that are unsafe as a filename stem on any platform.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
#: Builtin tool names are reserved: a workspace extension must not claim one.
RESERVED_TOOL_STEMS = frozenset(
    tool.casefold()
    for bundle in BUNDLES.values()
    for tool in bundle.tools
)


class _MetaToolError(ToolError):
    """A model-visible failure from a meta tool."""


# ---------------------------------------------------------------------------
# Service resolution
# ---------------------------------------------------------------------------


def _extension_service(ctx: ToolContext) -> ExtensionServiceView | None:
    for name in ("extensions", "extension_service"):
        service = getattr(ctx, name, None)
        if service is not None:
            return service
    return None


def _missing_service(tool: str) -> ToolExecutionResult:
    return _error(
        f"{tool}: no extension service is available for this call; the harness "
        "was not given an extension manager (this is a configuration error, not "
        "a missing extension)"
    )


# ---------------------------------------------------------------------------
# ReloadExtensions
# ---------------------------------------------------------------------------

_RELOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

RELOAD_EXTENSIONS_SPEC = ToolSpec(
    name="ReloadExtensions",
    group="extensions",
    description=(
        "Rebuild the extension manifest from disk and atomically swap it in. "
        "Call this after writing or editing a tool in .nexus/tools/ so the new "
        "tool becomes callable on the next iteration. Returns the diff."
    ),
    input_schema=_RELOAD_SCHEMA,
    bundle="meta",
    mutates=True,
    concurrency="exclusive",
    max_result_tokens=25_000,
)


def _forwarding_sink(ctx: ToolContext):
    """A reload sink that re-emits manager events through the context seam."""
    emit = getattr(ctx, "emit", None)
    if emit is None:
        return None

    async def _sink(event: object) -> None:
        event_type = getattr(event, "type", None)
        if not isinstance(event_type, str):
            return
        data = getattr(event, "data", None)
        payload = dict(data) if isinstance(data, Mapping) else {}
        outcome = emit(event_type, payload)
        if inspect.isawaitable(outcome):
            await outcome

    return _sink


async def _call_reload(service: ExtensionServiceView, ctx: ToolContext):
    sink = _forwarding_sink(ctx)
    if sink is None:
        return await service.reload(trigger="tool")
    try:
        return await service.reload(trigger="tool", sink=sink)
    except TypeError:
        # A structural test double may not accept the ``sink`` keyword; retry
        # without it rather than failing a legitimate reload.
        return await service.reload(trigger="tool")


def _failure_line(failure: object) -> str:
    to_dict = getattr(failure, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:  # noqa: BLE001 - a bad to_dict must not break output
            data = {}
    elif isinstance(failure, Mapping):
        data = failure
    else:
        data = {}
    name = sanitize_text(data.get("name") or getattr(failure, "name", "?"), limit=120)
    kind = sanitize_text(
        data.get("kind") or getattr(failure, "kind", "ext"), limit=40
    )
    error_type = sanitize_text(
        data.get("error_type") or getattr(failure, "error_type", ""), limit=60
    )
    error = sanitize_text(
        data.get("error") or getattr(failure, "error", ""), limit=300
    )
    label = f"{name} [{kind}]" if kind else name
    if error_type:
        label += f" {error_type}"
    return f"! {label}: {error}" if error else f"! {label}"


def _report_metrics(report: object) -> dict[str, Any]:
    to_dict = getattr(report, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, Mapping):
            return dict(data)
    return {
        "generation": getattr(report, "generation", None),
        "changed": getattr(report, "changed", None),
        "ok": getattr(report, "ok", None),
        "summary": getattr(report, "summary", None),
    }


async def reload_extensions(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    service = _extension_service(ctx)
    if service is None:
        return _missing_service("ReloadExtensions")
    try:
        _check_cancel(ctx)
        report = await _call_reload(service, ctx)
    except Exception as exc:  # noqa: BLE001 - a failed reload is model-visible
        message = sanitize_text(f"{type(exc).__name__}: {exc}")
        return _error(
            f"ReloadExtensions: reload failed: {message}. The previous manifest "
            "is still in effect; fix the extension and try again."
        )

    summary = getattr(report, "summary", None)
    if not isinstance(summary, str) or not summary:
        summary = "reload complete"
    lines = [f"ReloadExtensions: {sanitize_text(summary, limit=400)}"]
    failed = getattr(report, "failed", ()) or ()
    for failure in failed:
        lines.append(_failure_line(failure))
    metrics = _report_metrics(report)
    # A failed reload keeps the previous manifest but must be a model-visible
    # error so the agent fixes the extension and retries.
    return ToolExecutionResult.text(
        "\n".join(lines),
        is_error=bool(failed),
        display=f"ReloadExtensions: {sanitize_text(summary, limit=200)}",
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# ListExtensions
# ---------------------------------------------------------------------------

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

LIST_EXTENSIONS_SPEC = ToolSpec(
    name="ListExtensions",
    group="extensions",
    description=(
        "List the live tools, hot-loaded extension modules, discovered skills, "
        "and quarantined or shadowed extensions. Read-only; never returns "
        "source bodies or secrets."
    ),
    input_schema=_LIST_SCHEMA,
    bundle="meta",
    mutates=False,
    concurrency="parallel",
    max_result_tokens=25_000,
)


def _row_mapping(value: object) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:  # noqa: BLE001
            return {}
        return dict(data) if isinstance(data, Mapping) else {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _tool_line(name: str, tool: object) -> str:
    spec = getattr(tool, "spec", None)
    bundle = getattr(spec, "bundle", None) or getattr(tool, "bundle", "?")
    origin = getattr(tool, "origin", "builtin")
    mutates = getattr(spec, "mutates", getattr(tool, "mutates", False))
    generation = getattr(tool, "generation", 0)
    flags = "mutates" if mutates else "read"
    return (
        f"  {sanitize_text(name, limit=120)} "
        f"[{sanitize_text(bundle, limit=40)}] {sanitize_text(origin, limit=40)} "
        f"{flags} gen={generation}"
    )


def _skill_line(name: str, skill: object) -> str:
    source = getattr(getattr(skill, "source", None), "value", None)
    if source is None:
        provenance = getattr(skill, "provenance", None)
        source = getattr(getattr(provenance, "tier", None), "value", None)
    source = source or "unknown"
    description = ""
    sanitizer = getattr(skill, "sanitized_description", None)
    if callable(sanitizer):
        with contextlib.suppress(Exception):
            description = sanitizer()
    if not description:
        description = sanitize_text(getattr(skill, "description", ""), limit=160)
    return (
        f"  {sanitize_text(name, limit=120)} "
        f"[{sanitize_text(source, limit=40)}] {description}"
    )


def _module_line(row: Mapping[str, Any]) -> str:
    name = sanitize_text(row.get("name", "?"), limit=120)
    path = sanitize_text(row.get("path") or "", limit=200)
    sha = sanitize_text(row.get("sha256") or "", limit=64)
    generation = row.get("generation", 0)
    origin = sanitize_text(row.get("origin") or "ext", limit=40)
    return f"  {name} [{origin}] gen={generation} sha256={sha} path={path}"


def _diagnostic_line(row: Mapping[str, Any]) -> str:
    kind = sanitize_text(row.get("kind") or row.get("code") or "diag", limit=40)
    name = sanitize_text(row.get("name") or "?", limit=120)
    code = sanitize_text(row.get("code") or "", limit=60)
    message = sanitize_text(row.get("message") or row.get("error") or "", limit=300)
    label = f"{name} [{kind}]" if kind else name
    if code:
        label += f" {code}"
    return f"  {label}: {message}" if message else f"  {label}"


def _classify_diagnostics(
    rows: list[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    shadow_codes = {"shadowed", "case_collision"}
    shadowed: list[Mapping[str, Any]] = []
    quarantined: list[Mapping[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "")
        kind = str(row.get("kind") or "")
        if code in shadow_codes or kind in shadow_codes:
            shadowed.append(row)
        else:
            quarantined.append(row)
    return shadowed, quarantined


async def list_extensions(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    service = _extension_service(ctx)
    if service is None:
        return _missing_service("ListExtensions")

    manifest = getattr(service, "manifest", None)
    tools = getattr(manifest, "tools", None)
    skills = getattr(manifest, "skills", None)
    modules = getattr(manifest, "modules", None)

    try:
        extension_rows = list(service.list_extensions())
    except Exception:  # noqa: BLE001 - a broken listing degrades, never fails
        extension_rows = []
    try:
        diagnostics = [_row_mapping(row) for row in service.diagnostics()]
    except Exception:  # noqa: BLE001
        diagnostics = []
    shadowed, quarantined = _classify_diagnostics(diagnostics)

    lines: list[str] = []
    if isinstance(tools, Mapping):
        lines.append(f"Tools ({len(tools)}):")
        for name in sorted(tools):
            lines.append(_tool_line(str(name), tools[name]))
    else:
        lines.append("Tools (0):")

    if isinstance(skills, Mapping):
        lines.append(f"Skills ({len(skills)}):")
        for name in sorted(skills):
            lines.append(_skill_line(str(name), skills[name]))
    else:
        lines.append("Skills (0):")

    if isinstance(modules, Mapping):
        lines.append(f"Modules ({len(modules)}):")
        for name in sorted(modules):
            lines.append(_module_line(_row_mapping(modules[name]) | {"name": name}))
    else:
        lines.append(f"Modules ({len(extension_rows)}):")
        for row in extension_rows:
            lines.append(_module_line(_row_mapping(row)))

    lines.append(f"Quarantined ({len(quarantined)}):")
    for row in quarantined:
        lines.append(_diagnostic_line(row))
    lines.append(f"Shadowed ({len(shadowed)}):")
    for row in shadowed:
        lines.append(_diagnostic_line(row))

    text = "\n".join(lines)
    metrics = {
        "tools": len(tools) if isinstance(tools, Mapping) else 0,
        "skills": len(skills) if isinstance(skills, Mapping) else 0,
        "modules": len(modules) if isinstance(modules, Mapping) else len(extension_rows),
        "quarantined": len(quarantined),
        "shadowed": len(shadowed),
    }
    return ToolExecutionResult.text(
        text,
        display=(
            f"ListExtensions: {metrics['tools']} tools, {metrics['skills']} skills, "
            f"{metrics['quarantined']} quarantined, {metrics['shadowed']} shadowed"
        ),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# WriteTool
# ---------------------------------------------------------------------------

_WRITE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filename": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Simple .py filename to create under .nexus/tools (for example "
                "'metrics_query.py'). No directories or path separators."
            ),
        },
        "content": {
            "type": "string",
            "description": "Full UTF-8 Python source for the extension.",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Replace an existing file (default false).",
        },
    },
    "required": ["filename", "content"],
    "additionalProperties": False,
}


def _write_tool_key(data: dict[str, Any]) -> str:
    filename = data.get("filename")
    if not isinstance(filename, str) or not filename:
        return ""
    return f"{TOOLS_SUBDIR}/{filename}"


WRITE_TOOL_SPEC = ToolSpec(
    name="WriteTool",
    group="extensions",
    description=(
        "Write one Python extension tool to .nexus/tools/<filename>. Validates "
        "the filename, bounds the payload, and writes atomically. Does not "
        "reload: call ReloadExtensions afterwards to activate it."
    ),
    input_schema=_WRITE_TOOL_SCHEMA,
    bundle="meta",
    mutates=True,
    concurrency="exclusive",
    permission_key=_write_tool_key,
    # The declared key is workspace-relative (``.nexus/tools/<name>``); the
    # manager canonicalizes it to an absolute path and applies the fs write-root
    # / read-deny boundary, so WriteTool is gated exactly like ``Write``.
    path_mode=True,
    max_result_tokens=25_000,
)

_DEFAULT_MAX_TOOL_BYTES = 262_144
_HARD_MAX_TOOL_BYTES = 1_048_576


def _max_tool_bytes(config: object) -> int:
    v2 = getattr(config, "v2", None)
    ext = getattr(v2, "ext", None)
    value = getattr(ext, "max_file_bytes", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        value = _DEFAULT_MAX_TOOL_BYTES
    return min(value, _HARD_MAX_TOOL_BYTES)


def validate_tool_filename(filename: object) -> str:
    """Return a validated simple ``*.py`` filename, or raise.

    Refuses a non-string/empty/oversize value, NUL bytes, path separators,
    ``.``/``..``/``~`` prefixes, a leading underscore, any name that is not a
    simple ``<letter><word>*.py``, and reserved stems (builtin tool names,
    ``__init__``/``_template``, and Windows device names).
    """
    if not isinstance(filename, str) or not filename:
        raise _MetaToolError("filename must be a non-empty string")
    if "\x00" in filename:
        raise _MetaToolError("filename must not contain a NUL byte")
    if len(filename.encode("utf-8")) > MAX_TOOL_FILENAME_BYTES:
        raise _MetaToolError(
            f"filename is longer than {MAX_TOOL_FILENAME_BYTES} bytes"
        )
    if filename.startswith((".", "~")):
        raise _MetaToolError(
            "filename must be a simple name, not a hidden/relative path"
        )
    if "/" in filename or "\\" in filename:
        raise _MetaToolError(
            "filename must not contain a path separator; it is written directly "
            "under .nexus/tools"
        )
    if filename.startswith("_"):
        raise _MetaToolError(
            "filename must not start with an underscore; underscore-prefixed "
            "files are support modules and are not loaded as tools"
        )
    if _TOOL_FILENAME_RE.fullmatch(filename) is None:
        raise _MetaToolError(
            "filename must be a simple Python filename such as 'my_tool.py' "
            "(a letter, then letters/digits/underscores, ending in .py)"
        )
    stem = filename[:-3]
    folded = stem.casefold()
    if folded in RESERVED_TOOL_STEMS:
        raise _MetaToolError(
            f"filename {filename!r} is reserved: it collides with a builtin "
            "tool name"
        )
    if folded in _WINDOWS_RESERVED:
        raise _MetaToolError(
            f"filename {stem!r} is a reserved device name on some platforms"
        )
    return filename


def _tools_dir(ctx: ToolContext):
    return ctx.workspace / ".nexus" / "tools"


async def write_tool(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return _error("WriteTool: arguments must be an object")
    try:
        filename = validate_tool_filename(args.get("filename"))
        content = _require_text(args, "content")
        overwrite = _opt_bool(args, "overwrite", False)
    except ToolError as exc:
        return _error(f"WriteTool: {exc}")

    data = content.encode("utf-8")
    limit = _max_tool_bytes(ctx.config)
    if len(data) > limit:
        return _error(
            f"WriteTool: content is {len(data)} bytes, exceeding the {limit}-byte "
            "extension cap"
        )
    if b"\x00" in data:
        return _error("WriteTool: content must not contain a NUL byte")

    tools_dir = _tools_dir(ctx)
    if tools_dir.is_symlink():
        return _error(
            "WriteTool: .nexus/tools is a symlink; refusing to write through it"
        )
    target = tools_dir / filename
    if target.is_symlink():
        return _error(
            f"WriteTool: {filename!r} is a symlink; refusing to overwrite it"
        )
    existed = target.exists()
    if existed and not overwrite:
        return _error(
            f"WriteTool: {TOOLS_SUBDIR}/{filename} already exists; pass "
            "overwrite=true to replace it"
        )

    guard = _guard(ctx)
    relative = f"{TOOLS_SUBDIR}/{filename}"
    try:
        first = guard.resolve(relative, for_write=True)
        if not first.absolute.is_relative_to(tools_dir.resolve()):
            return _error(
                f"WriteTool: {relative} resolves outside .nexus/tools; refusing"
            )
        resolved = guard.recheck(relative, for_write=True)  # TOCTOU re-check
        _check_cancel(ctx)
        await atomic_write_bytes(
            ctx, resolved.absolute, data, create_parents=True
        )
    except PathSecurityError as exc:
        return _error(f"WriteTool: {exc}")
    except ToolError as exc:
        return _error(f"WriteTool: {exc}")
    except OSError as exc:
        return _error(f"WriteTool: write failed for {relative}: {exc}")

    body = (
        f"Wrote {len(data)} bytes to {TOOLS_SUBDIR}/{filename}. "
        "It is not active yet; call ReloadExtensions to load it."
    )
    return ToolExecutionResult.text(
        body,
        display=f"WriteTool {TOOLS_SUBDIR}/{filename}: {len(data)} bytes",
        context_note=(
            f"[WriteTool {TOOLS_SUBDIR}/{filename}: written but not loaded; "
            "call ReloadExtensions to activate]"
        ),
        metrics={
            "bytes": len(data),
            "created": not existed,
            "path": resolved.key,
            "reload_required": True,
        },
    )


#: Public runner aliases used by the builtin catalog.
run_reload_extensions = reload_extensions
run_list_extensions = list_extensions
run_write_tool = write_tool
