from __future__ import annotations

import pytest

from nexus.tools.builtin import _patch_parse as patch_parse


def test_parses_add_update_delete_and_move_with_immutable_results() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.txt\n"
        "+first\n"
        "+\n"
        "*** Update File: edit.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " keep\n"
        "-old\n"
        "+new\n"
        "*** Delete File: gone.txt\n"
        "*** Move File: before.txt -> after.txt\n"
        "*** End Patch\n"
    )

    operations = patch_parse.parse_patch(patch)

    assert tuple(operation.kind for operation in operations) == (
        "add",
        "update",
        "delete",
        "move",
    )
    assert operations[0].hunks[0].lines == (
        patch_parse.PatchLine("add", "first"),
        patch_parse.PatchLine("add", ""),
    )
    assert operations[1].hunks[0].lines == (
        patch_parse.PatchLine("context", "keep"),
        patch_parse.PatchLine("remove", "old"),
        patch_parse.PatchLine("add", "new"),
    )
    assert patch_parse.operation_path_refs(operations[3]) == (
        "before.txt",
        "after.txt",
    )
    with pytest.raises(AttributeError):
        operations[0].source = "changed"  # type: ignore[misc]


def test_parses_multiple_counted_hunks_and_default_single_line_counts() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Update File: file.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "@@ -5,0 +6,1 @@\n"
        "+last\n"
        "*** End Patch\n"
    )

    hunks = patch_parse.parse_patch(patch)[0].hunks

    assert [(h.old_start, h.old_count, h.new_start, h.new_count) for h in hunks] == [
        (1, 1, 1, 1),
        (5, 0, 6, 1),
    ]


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ("*** Begin Patch\n*** Add File: a\n+x\n", "invalid_boundary"),
        (
            "*** Begin Patch\n*** Add File: a\n+x",
            "missing_newline",
        ),
        (
            "*** Begin Patch\n*** Add File: ../secret\n+x\n*** End Patch\n",
            "invalid_path",
        ),
        (
            "*** Begin Patch\n*** Add File: /absolute\n+x\n*** End Patch\n",
            "invalid_path",
        ),
        (
            "*** Begin Patch\n*** Move File: same -> same\n*** End Patch\n",
            "duplicate_path",
        ),
        (
            "*** Begin Patch\n*** Add File: a\n+x\n*** Delete File: a\n*** End Patch\n",
            "duplicate_path",
        ),
        (
            "*** Begin Patch\n*** Move File: a -> b\n*** Add File: b\n+x\n*** End Patch\n",
            "duplicate_path",
        ),
        (
            "*** Begin Patch\n*** Update File: a\n@@ -1,2 +1,1 @@\n-old\n+new\n*** End Patch\n",
            "hunk_count_mismatch",
        ),
        (
            "*** Begin Patch\n*** Update File: a\n@@ -0,1 +1,1 @@\n-old\n+new\n*** End Patch\n",
            "invalid_hunk_position",
        ),
        (
            "*** Begin Patch\n*** Update File: a\n@@ -1,1 +1,1 @@ trailing\n-old\n+new\n*** End Patch\n",
            "invalid_hunk_header",
        ),
        (
            "*** Begin Patch\n*** Update File: a\n@@ -1,1 +1,1 @@\n?bad\n*** End Patch\n",
            "invalid_hunk_line",
        ),
        (
            "*** Begin Patch\n*** Update File: a\n@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n*** End Patch\n",
            "unsupported_no_newline",
        ),
        (
            "*** Begin Patch\n*** Add File: bad\x85name\n+x\n*** End Patch\n",
            "invalid_path",
        ),
        (
            "*** Begin Patch\n*** Add File: bad\u202ename\n+x\n*** End Patch\n",
            "invalid_path",
        ),
        (
            "*** Begin Patch\n*** Add File: bad\ud800name\n+x\n*** End Patch\n",
            "invalid_path",
        ),
        (
            "*** Begin Patch\n*** Add File: a\n+bad\u2028content\n*** End Patch\n",
            "invalid_content",
        ),
        (
            "*** Begin Patch\n*** Add File: a\n+bad\ud800content\n*** End Patch\n",
            "invalid_content",
        ),
        (
            "*** Begin Patch\nGIT binary patch\n*** End Patch\n",
            "binary_patch",
        ),
        (
            "*** Begin Patch\n*** Add File: a\n+\x00\n*** End Patch\n",
            "nul_byte",
        ),
    ],
)
def test_rejects_invalid_patch_and_returns_bounded_content_free_error(
    patch: str, code: str
) -> None:
    with pytest.raises(patch_parse.PatchParseError) as caught:
        patch_parse.parse_patch(patch)

    assert caught.value.code == code
    assert len(str(caught.value)) <= 100
    assert "secret" not in str(caught.value)


def test_rejects_resource_limit_overruns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(patch_parse, "MAX_PATCH_CHARS", 30)
    with pytest.raises(patch_parse.PatchParseError, match="character limit") as caught:
        patch_parse.parse_patch("*** Begin Patch\n" + "x" * 31 + "\n")
    assert caught.value.code == "patch_too_large"

    monkeypatch.setattr(patch_parse, "MAX_PATCH_CHARS", 1_000_000)
    monkeypatch.setattr(patch_parse, "MAX_LINE_CHARS", 8)
    with pytest.raises(patch_parse.PatchParseError) as caught:
        patch_parse.parse_patch(
            "*** Begin Patch\n*** Add File: a\n+12345678\n*** End Patch\n"
        )
    assert caught.value.code == "line_too_long"

    monkeypatch.setattr(patch_parse, "MAX_LINE_CHARS", 100_000)
    monkeypatch.setattr(patch_parse, "MAX_OPERATIONS", 1)
    with pytest.raises(patch_parse.PatchParseError) as caught:
        patch_parse.parse_patch(
            "*** Begin Patch\n"
            "*** Delete File: a\n"
            "*** Delete File: b\n"
            "*** End Patch\n"
        )
    assert caught.value.code == "too_many_operations"

    monkeypatch.setattr(patch_parse, "MAX_OPERATIONS", 1_000)
    monkeypatch.setattr(patch_parse, "MAX_HUNKS", 1)
    with pytest.raises(patch_parse.PatchParseError) as caught:
        patch_parse.parse_patch(
            "*** Begin Patch\n"
            "*** Update File: a\n"
            "@@ -1,0 +1,1 @@\n+x\n"
            "@@ -3,0 +4,1 @@\n+y\n"
            "*** End Patch\n"
        )
    assert caught.value.code == "too_many_hunks"


@pytest.mark.parametrize(
    "paths",
    [
        ("A.txt", "a.txt"),
        ("caf\u00e9.txt", "cafe\u0301.txt"),
        ("a", "a/b"),
        ("a/b", "a"),
    ],
)
def test_rejects_portable_path_aliases_and_prefix_conflicts(
    paths: tuple[str, str],
) -> None:
    patch = (
        "*** Begin Patch\n"
        f"*** Delete File: {paths[0]}\n"
        f"*** Delete File: {paths[1]}\n"
        "*** End Patch\n"
    )

    with pytest.raises(patch_parse.PatchParseError) as caught:
        patch_parse.parse_patch(patch)

    assert caught.value.code == "duplicate_path"


def test_normalizes_path_components_to_nfc() -> None:
    operation = patch_parse.parse_patch(
        "*** Begin Patch\n*** Delete File: cafe\u0301/file.txt\n*** End Patch\n"
    )[0]

    assert operation.source == "caf\u00e9/file.txt"
