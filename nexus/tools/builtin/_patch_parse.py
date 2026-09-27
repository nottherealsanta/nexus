"""Pure, bounded parser for the strict Nexus multi-file patch format.

This module deliberately does not inspect or modify the filesystem. In
particular, update hunk context is retained for a later runner to verify.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "MAX_HUNKS",
    "MAX_LINE_CHARS",
    "MAX_OPERATIONS",
    "MAX_PATCH_CHARS",
    "PatchHunk",
    "PatchLine",
    "PatchOperation",
    "PatchParseError",
    "operation_path_refs",
    "parse_patch",
]

MAX_PATCH_CHARS = 1_000_000
MAX_OPERATIONS = 1_000
MAX_HUNKS = 10_000
MAX_LINE_CHARS = 100_000

_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@$")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_REJECTED_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}
_ERRORS = {
    "invalid_input": "patch must be text",
    "patch_too_large": "patch exceeds character limit",
    "line_too_long": "patch line exceeds character limit",
    "nul_byte": "patch contains a NUL byte",
    "binary_patch": "binary patches are not supported",
    "missing_newline": "patch and patch lines must end with LF",
    "invalid_boundary": "patch must start with Begin Patch and end with End Patch",
    "unexpected_entry": "unexpected patch entry",
    "invalid_operation": "invalid patch operation header",
    "invalid_path": "invalid relative path",
    "invalid_content": "patch content contains a disallowed character",
    "duplicate_path": "patch operations contain conflicting paths",
    "too_many_operations": "patch exceeds operation limit",
    "too_many_hunks": "patch exceeds hunk limit",
    "invalid_hunk_header": "invalid hunk header",
    "invalid_hunk_position": "invalid or overlapping hunk positions",
    "invalid_hunk_line": "invalid hunk line",
    "hunk_count_mismatch": "hunk line counts do not match header",
    "empty_update": "update operation must contain at least one hunk",
    "unsupported_no_newline": "no-final-newline markers are not supported",
}


class PatchParseError(ValueError):
    """A bounded, content-free parse error suitable for safe reporting."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERRORS else "unexpected_entry"
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True, slots=True)
class PatchLine:
    """One unified-diff line; ``kind`` is context, removal, or addition."""

    kind: Literal["context", "remove", "add"]
    text: str


@dataclass(frozen=True, slots=True)
class PatchHunk:
    """A counted unified-diff hunk with its source and destination positions."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[PatchLine, ...]


@dataclass(frozen=True, slots=True)
class PatchOperation:
    """One immutable add, update, delete, or move instruction."""

    kind: Literal["add", "update", "delete", "move"]
    source: str
    destination: str | None
    hunks: tuple[PatchHunk, ...]


def operation_path_refs(operation: PatchOperation) -> tuple[str, ...]:
    """Return every raw path an operation touches, for authorization checks."""

    if operation.kind == "move":
        # A parsed move always has a destination.
        assert operation.destination is not None
        return operation.source, operation.destination
    return (operation.source,)


def _fail(code: str) -> None:
    raise PatchParseError(code)


def _validate_path(path: str) -> str:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or _DRIVE_PATH.match(path)
        or any(unicodedata.category(char) in _REJECTED_CATEGORIES for char in path)
    ):
        _fail("invalid_path")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail("invalid_path")
    return "/".join(unicodedata.normalize("NFC", part) for part in parts)


def _validate_content(text: str) -> None:
    if any(unicodedata.category(char) in _REJECTED_CATEGORIES for char in text):
        _fail("invalid_content")


def _path_key(path: str) -> tuple[str, ...]:
    """Return a conservative key for case- and normalization-insensitive filesystems."""

    return tuple(
        unicodedata.normalize("NFC", part.casefold()) for part in path.split("/")
    )


def _conflicts_with_touched(
    path: tuple[str, ...],
    touched_paths: set[tuple[str, ...]],
    touched_prefixes: set[tuple[str, ...]],
) -> bool:
    return (
        path in touched_paths
        or path in touched_prefixes
        or any(path[:length] in touched_paths for length in range(1, len(path)))
    )


def _operation_header(line: str) -> tuple[str, str, str | None] | None:
    for label, kind in (("Add File: ", "add"), ("Update File: ", "update"), ("Delete File: ", "delete")):
        prefix = f"*** {label}"
        if line.startswith(prefix):
            path = line[len(prefix) :]
            if not path or path.strip() != path:
                _fail("invalid_operation")
            return kind, _validate_path(path), None
    prefix = "*** Move File: "
    if line.startswith(prefix):
        paths = line[len(prefix) :].split(" -> ")
        if len(paths) != 2 or any(not path or path.strip() != path for path in paths):
            _fail("invalid_operation")
        return "move", _validate_path(paths[0]), _validate_path(paths[1])
    return None


def _parse_hunk_header(line: str) -> tuple[int, int, int, int]:
    match = _HEADER.fullmatch(line)
    if match is None:
        _fail("invalid_hunk_header")
    old_start, old_count, new_start, new_count = match.groups()
    # Unified diff omits a count only for a one-line range.
    old_start_value = int(old_start)
    old_count_value = 1 if old_count is None else int(old_count)
    new_start_value = int(new_start)
    new_count_value = 1 if new_count is None else int(new_count)
    if (old_count_value and old_start_value == 0) or (new_count_value and new_start_value == 0):
        _fail("invalid_hunk_position")
    return old_start_value, old_count_value, new_start_value, new_count_value


def _parse_update(lines: list[str], index: int) -> tuple[tuple[PatchHunk, ...], int]:
    hunks: list[PatchHunk] = []
    previous_old_end = 0
    previous_new_end = 0
    while index < len(lines) and lines[index].startswith("@@"):
        if len(hunks) >= MAX_HUNKS:
            _fail("too_many_hunks")
        old_start, old_count, new_start, new_count = _parse_hunk_header(lines[index])
        if old_start < previous_old_end or new_start < previous_new_end:
            _fail("invalid_hunk_position")
        index += 1
        hunk_lines: list[PatchLine] = []
        seen_old = 0
        seen_new = 0
        while index < len(lines) and not lines[index].startswith(("@@", "*** ")):
            line = lines[index]
            if line == r"\ No newline at end of file":
                _fail("unsupported_no_newline")
            if not line:
                _fail("invalid_hunk_line")
            prefix, text = line[0], line[1:]
            if prefix == " ":
                hunk_lines.append(PatchLine("context", text))
                seen_old += 1
                seen_new += 1
            elif prefix == "-":
                hunk_lines.append(PatchLine("remove", text))
                seen_old += 1
            elif prefix == "+":
                hunk_lines.append(PatchLine("add", text))
                seen_new += 1
            else:
                _fail("invalid_hunk_line")
            index += 1
        if seen_old != old_count or seen_new != new_count:
            _fail("hunk_count_mismatch")
        if not hunk_lines:
            _fail("invalid_hunk_line")
        hunks.append(
            PatchHunk(old_start, old_count, new_start, new_count, tuple(hunk_lines))
        )
        previous_old_end = old_start + old_count
        previous_new_end = new_start + new_count
    if not hunks:
        _fail("empty_update")
    return tuple(hunks), index


def parse_patch(text: str) -> tuple[PatchOperation, ...]:
    """Parse a strict, bounded multi-file patch without performing I/O.

    All patch records, including the terminal marker, must use LF line endings.
    A missing final LF and unified-diff no-final-newline markers are rejected.
    """

    if not isinstance(text, str):
        _fail("invalid_input")
    if len(text) > MAX_PATCH_CHARS:
        _fail("patch_too_large")
    if "\x00" in text:
        _fail("nul_byte")
    if "\r" in text:
        _fail("missing_newline")
    if not text.endswith("\n"):
        _fail("missing_newline")

    records = text[:-1].split("\n")
    for record in records:
        if len(record) > MAX_LINE_CHARS:
            _fail("line_too_long")
        if record == "GIT binary patch" or record.startswith("Binary files "):
            _fail("binary_patch")
    if len(records) < 2 or records[0] != "*** Begin Patch" or records[-1] != "*** End Patch":
        _fail("invalid_boundary")

    operations: list[PatchOperation] = []
    touched_paths: set[tuple[str, ...]] = set()
    touched_prefixes: set[tuple[str, ...]] = set()
    index = 1
    end_index = len(records) - 1
    while index < end_index:
        operation = _operation_header(records[index])
        if operation is None:
            _fail("unexpected_entry")
        kind, source, destination = operation
        if len(operations) >= MAX_OPERATIONS:
            _fail("too_many_operations")
        refs = (source, destination) if destination is not None else (source,)
        ref_keys = tuple(_path_key(path) for path in refs)
        local_paths = set(touched_paths)
        local_prefixes = set(touched_prefixes)
        for path_key in ref_keys:
            if _conflicts_with_touched(path_key, local_paths, local_prefixes):
                _fail("duplicate_path")
            local_paths.add(path_key)
            local_prefixes.update(
                path_key[:length] for length in range(1, len(path_key))
            )
        touched_paths = local_paths
        touched_prefixes = local_prefixes
        index += 1

        if kind == "add":
            contents: list[PatchLine] = []
            while index < end_index and not records[index].startswith("*** "):
                line = records[index]
                if not line.startswith("+"):
                    _fail("unexpected_entry")
                content = line[1:]
                _validate_content(content)
                contents.append(PatchLine("add", content))
                index += 1
            # Add bodies use the same immutable line representation as hunks.
            hunks = (PatchHunk(0, 0, 0, len(contents), tuple(contents)),)
        elif kind == "update":
            hunk_lines: list[str] = []
            while index < end_index and not records[index].startswith("*** "):
                hunk_lines.append(records[index])
                index += 1
            for line in hunk_lines:
                if line and line[0] in " +-":
                    _validate_content(line[1:])
            hunks, consumed = _parse_update(hunk_lines, 0)
            if consumed != len(hunk_lines):
                _fail("unexpected_entry")
        else:
            hunks = ()
            if index < end_index and not records[index].startswith("*** "):
                _fail("unexpected_entry")

        operations.append(PatchOperation(kind, source, destination, hunks))  # type: ignore[arg-type]

    if not operations:
        _fail("unexpected_entry")
    return tuple(operations)
