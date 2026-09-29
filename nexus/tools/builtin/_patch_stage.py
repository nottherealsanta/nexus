"""Pure snapshot-based planning for parsed Nexus patches."""
from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from nexus.tools.builtin._patch_parse import PatchHunk, PatchOperation

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

    def __init__(self, code: str, path: str | None = None) -> None:
        self.code = code if code in _ERRORS else "invalid_operation"
        #: The workspace-relative patch path, set once the failing operation is known.
        self.path = path
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


def _seek(
    lines: Sequence[str], pattern: Sequence[str], start: int, end_of_file: bool
) -> int | None:
    """Find ``pattern`` in ``lines`` at or after ``start``.

    Matching follows the Codex ``apply_patch`` ladder: exact, then ignoring
    trailing whitespace, then ignoring surrounding whitespace. The first
    match wins; an ``end_of_file`` hunk tries the final position first.
    """

    if not pattern:
        return len(lines) if end_of_file or start >= len(lines) else None
    last = len(lines) - len(pattern)
    if last < start:
        return None
    for normalize in (lambda text: text, str.rstrip, str.strip):
        wanted = [normalize(text) for text in pattern]
        if end_of_file and [normalize(text) for text in lines[last:]] == wanted:
            return last
        for index in range(start, last + 1):
            if all(normalize(lines[index + k]) == wanted[k] for k in range(len(pattern))):
                return index
    return None


def _locate(lines: Sequence[str], hunk: PatchHunk, cursor: int) -> int:
    """Return the source offset of a hunk that is located by content."""

    start = cursor
    if hunk.anchor is not None:
        found = _seek(lines, (hunk.anchor,), start, False)
        if found is None:
            _fail("context_mismatch")
        start = found + 1
    old = [line.text for line in hunk.lines if line.kind != "add"]
    if not old:
        # A pure insertion goes right after its anchor, else at end of file.
        return start if hunk.anchor is not None else len(lines)
    found = _seek(lines, old, start, hunk.end_of_file)
    if found is None:
        _fail("context_mismatch")
    return found


def _matches_at(lines: Sequence[str], hunk: PatchHunk, offset: int) -> bool:
    old = [line.text for line in hunk.lines if line.kind != "add"]
    return lines[offset : offset + len(old)] == old


def _updated_bytes(original: bytes, operation: PatchOperation) -> bytes:
    old_lines, terminated = _text_lines(original)
    output: list[str] = []
    old_cursor = 0
    new_cursor = 0
    # Once one hunk is located by content, later destination positions no
    # longer line up with their headers, so only source context is enforced.
    relaxed = False

    for hunk in operation.hunks:
        if hunk.numbered:
            old_offset = hunk.old_start - 1 if hunk.old_count else hunk.old_start
            new_offset = hunk.new_start - 1 if hunk.new_count else hunk.new_start
            if old_offset < old_cursor or old_offset > len(old_lines):
                if not relaxed:
                    _fail("invalid_ranges")
                old_offset = _locate(old_lines, hunk, old_cursor)
            unchanged = old_offset - old_cursor
            if not relaxed and new_offset != new_cursor + unchanged:
                _fail("invalid_ranges")
            if not _matches_at(old_lines, hunk, old_offset):
                # Models miscount line numbers; trust the context instead.
                old_offset = _locate(old_lines, hunk, old_cursor)
                relaxed = True
        else:
            old_offset = _locate(old_lines, hunk, old_cursor)
            relaxed = True
        output.extend(old_lines[old_cursor:old_offset])
        new_cursor += old_offset - old_cursor
        old_cursor = old_offset

        consumed_old = 0
        produced_new = 0
        for line in hunk.lines:
            if line.kind in ("context", "remove"):
                source_index = old_cursor + consumed_old
                if source_index >= len(old_lines):
                    _fail("context_mismatch")
                if line.kind == "context":
                    # Keep the file's own text when matched loosely.
                    output.append(old_lines[source_index])
                    produced_new += 1
                consumed_old += 1
            else:
                output.append(line.text)
                produced_new += 1
        if hunk.numbered and (consumed_old != hunk.old_count or produced_new != hunk.new_count):
            _fail("invalid_ranges")
        old_cursor += consumed_old
        new_cursor += produced_new

    output.extend(old_lines[old_cursor:])
    text = "\n".join(output)
    if (terminated or not old_lines) and output:
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
        try:
            staged.extend(_stage_operation(operation, snapshots))
        except PatchStageError as exc:
            exc.path = exc.path or operation.source
            raise
    return _with_diff(staged)


def _stage_operation(
    operation: PatchOperation, snapshots: Mapping[str, bytes | None]
) -> tuple[StagedChange, ...]:
    kind = operation.kind
    if kind == "add":
        destination = operation.source
        if _snapshot(snapshots, destination) is not None:
            _fail("destination_exists")
        lines = operation.hunks[0].lines
        new_bytes = _encode_text(
            "\n".join(line.text for line in lines) + ("\n" if lines else "")
        )
        return (StagedChange(destination, None, new_bytes, "add"),)
    old_bytes = _snapshot(snapshots, operation.source)
    if old_bytes is None:
        _fail("source_missing")
    if kind == "update":
        return (
            StagedChange(
                operation.source, old_bytes, _updated_bytes(old_bytes, operation), "update"
            ),
        )
    if kind == "delete":
        return (StagedChange(operation.source, old_bytes, None, "delete"),)
    if kind == "move" and operation.destination is not None:
        if _snapshot(snapshots, operation.destination) is not None:
            _fail("destination_exists")
        moved = _updated_bytes(old_bytes, operation) if operation.hunks else old_bytes
        return (
            StagedChange(operation.source, old_bytes, None, "move"),
            StagedChange(operation.destination, None, moved, "move"),
        )
    _fail("invalid_operation")
    raise AssertionError("unreachable")


def _with_diff(staged: Sequence[StagedChange]) -> StagedChanges:
    diff_parts: list[str] = []
    diff_size = 0
    for change in staged:
        part = _diff_for(change)
        diff_size += len(part)
        if diff_size > MAX_DIFF_CHARS:
            _fail("diff_too_large")
        diff_parts.append(part)
    return StagedChanges(tuple(staged), "".join(diff_parts))
