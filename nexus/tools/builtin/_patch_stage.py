"""Pure snapshot-based planning for parsed Nexus patches."""
from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from nexus.tools.builtin._patch_parse import PatchOperation

__all__ = [
    "MAX_DIFF_CHARS",
    "PatchStageError",
    "StagedChange",
    "StagedChanges",
    "stage_patch",
]

MAX_DIFF_CHARS = 1_000_000

_ERRORS = {
    "missing_snapshot": "patch path is missing from the supplied snapshots",
    "source_missing": "patch source does not exist in the supplied snapshots",
    "destination_exists": "patch destination already exists in the supplied snapshots",
    "invalid_snapshot": "snapshot must be bytes or None",
    "invalid_operation": "patch operation is invalid",
    "invalid_content": "patch content contains an invalid Unicode surrogate",
    "invalid_ranges": "hunk ranges do not match exact source and destination positions",
    "context_mismatch": "hunk context does not match the source snapshot",
    "binary_context": "update context requires UTF-8 LF text",
    "diff_too_large": "combined patch diff exceeds character limit",
}


class PatchStageError(ValueError):
    """A bounded, content-free error raised while planning staged changes."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERRORS else "invalid_operation"
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True, slots=True)
class StagedChange:
    """One immutable path change; move operations produce source and destination entries."""

    path: str
    old_bytes: bytes | None
    new_bytes: bytes | None
    operation_status: Literal["add", "update", "delete", "move"]


@dataclass(frozen=True, slots=True)
class StagedChanges:
    """The complete immutable plan and its bounded, combined unified diff."""

    changes: tuple[StagedChange, ...]
    diff: str


def _fail(code: str) -> None:
    raise PatchStageError(code)


def _encode_text(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        _fail("invalid_content")


def _snapshot(snapshots: Mapping[str, bytes | None], path: str) -> bytes | None:
    if path not in snapshots:
        _fail("missing_snapshot")
    value = snapshots[path]
    if value is not None and not isinstance(value, bytes):
        _fail("invalid_snapshot")
    return value


def _text_lines(data: bytes) -> tuple[list[str], bool]:
    """Decode a byte snapshot as strict UTF-8 LF text, retaining final-LF state."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("binary_context")
    if "\r" in text or "\x00" in text:
        _fail("binary_context")
    terminated = text.endswith("\n")
    if not text:
        return [], False
    return (text[:-1].split("\n") if terminated else text.split("\n")), terminated


def _updated_bytes(original: bytes, operation: PatchOperation) -> bytes:
    old_lines, terminated = _text_lines(original)
    output: list[str] = []
    old_cursor = 0
    new_cursor = 0

    for hunk in operation.hunks:
        old_offset = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        new_offset = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        if old_offset < old_cursor or old_offset > len(old_lines):
            _fail("invalid_ranges")
        unchanged = old_offset - old_cursor
        if new_offset != new_cursor + unchanged:
            _fail("invalid_ranges")
        output.extend(old_lines[old_cursor:old_offset])
        old_cursor = old_offset
        new_cursor += unchanged

        consumed_old = 0
        produced_new = 0
        for line in hunk.lines:
            if line.kind in ("context", "remove"):
                source_index = old_cursor + consumed_old
                if source_index >= len(old_lines) or old_lines[source_index] != line.text:
                    _fail("context_mismatch")
                consumed_old += 1
            if line.kind in ("context", "add"):
                output.append(line.text)
                produced_new += 1
        if consumed_old != hunk.old_count or produced_new != hunk.new_count:
            _fail("invalid_ranges")
        old_cursor += consumed_old
        new_cursor += produced_new

    output.extend(old_lines[old_cursor:])
    text = "\n".join(output)
    if terminated and output:
        text += "\n"
    return _encode_text(text)


def _diff_for(change: StagedChange) -> str:
    old = change.old_bytes
    new = change.new_bytes
    if old is not None and new is not None:
        old_path, new_path = change.path, change.path
    elif old is not None:
        old_path, new_path = change.path, "/dev/null"
    else:
        old_path, new_path = "/dev/null", change.path

    old_text: str | None = None
    new_text: str | None = None
    try:
        if old is not None:
            old_text = old.decode("utf-8")
        if new is not None:
            new_text = new.decode("utf-8")
    except UnicodeDecodeError:
        return f"Binary files {old_path} and {new_path} differ\n"
    if any(text is not None and ("\r" in text or "\x00" in text) for text in (old_text, new_text)):
        return f"Binary files {old_path} and {new_path} differ\n"

    old_lines = old_text.splitlines(keepends=True) if old_text is not None else []
    new_lines = new_text.splitlines(keepends=True) if new_text is not None else []
    # difflib omits the distinction between an empty file and one with a
    # missing final LF unless keepends=True is used above; patch-plan diff is
    # display-only and remains bounded by MAX_DIFF_CHARS.
    records = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=old_path,
        tofile=new_path,
        lineterm="\n",
    )
    rendered: list[str] = []
    for record in records:
        rendered.append(record)
        if not record.endswith("\n"):
            rendered.extend(("\n", "\\ No newline at end of file\n"))
    return "".join(rendered)


def stage_patch(
    operations: Sequence[PatchOperation],
    snapshots: Mapping[str, bytes | None],
) -> StagedChanges:
    """Validate parsed operations against approved byte snapshots without I/O.

    ``None`` denotes an absent path. The caller remains responsible for path
    authorization and for obtaining the snapshots that are passed here.
    """

    staged: list[StagedChange] = []
    for operation in operations:
        kind = operation.kind
        if kind == "add":
            destination = operation.source
            if _snapshot(snapshots, destination) is not None:
                _fail("destination_exists")
            lines = operation.hunks[0].lines
            new_bytes = _encode_text(
                "\n".join(line.text for line in lines) + ("\n" if lines else "")
            )
            staged.append(StagedChange(destination, None, new_bytes, "add"))
        elif kind == "update":
            old_bytes = _snapshot(snapshots, operation.source)
            if old_bytes is None:
                _fail("source_missing")
            staged.append(
                StagedChange(
                    operation.source,
                    old_bytes,
                    _updated_bytes(old_bytes, operation),
                    "update",
                )
            )
        elif kind == "delete":
            old_bytes = _snapshot(snapshots, operation.source)
            if old_bytes is None:
                _fail("source_missing")
            staged.append(StagedChange(operation.source, old_bytes, None, "delete"))
        elif kind == "move" and operation.destination is not None:
            old_bytes = _snapshot(snapshots, operation.source)
            if old_bytes is None:
                _fail("source_missing")
            if _snapshot(snapshots, operation.destination) is not None:
                _fail("destination_exists")
            staged.extend(
                (
                    StagedChange(operation.source, old_bytes, None, "move"),
                    StagedChange(operation.destination, None, old_bytes, "move"),
                )
            )
        else:
            _fail("invalid_operation")

    diff_parts: list[str] = []
    diff_size = 0
    for change in staged:
        part = _diff_for(change)
        diff_size += len(part)
        if diff_size > MAX_DIFF_CHARS:
            _fail("diff_too_large")
        diff_parts.append(part)
    return StagedChanges(tuple(staged), "".join(diff_parts))
