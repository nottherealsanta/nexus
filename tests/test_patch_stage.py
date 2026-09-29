from __future__ import annotations

import pytest

from nexus.tools.builtin import _patch_parse as patch_parse
from nexus.tools.builtin import _patch_stage as patch_stage


def _parse(body: str) -> tuple[patch_parse.PatchOperation, ...]:
    return patch_parse.parse_patch(f"*** Begin Patch\n{body}*** End Patch\n")


def test_stages_mixed_operations_and_move_as_two_file_changes() -> None:
    operations = _parse(
        "*** Add File: added.txt\n+first\n+second\n"
        "*** Update File: edited.txt\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n"
        "*** Delete File: deleted.txt\n"
        "*** Move File: moved.txt -> destination.txt\n"
    )

    staged = patch_stage.stage_patch(
        operations,
        {
            "added.txt": None,
            "edited.txt": b"keep\nold\n",
            "deleted.txt": b"remove me\n",
            "moved.txt": b"move me\n",
            "destination.txt": None,
        },
    )

    assert [(change.path, change.operation_status) for change in staged.changes] == [
        ("added.txt", "add"),
        ("edited.txt", "update"),
        ("deleted.txt", "delete"),
        ("moved.txt", "move"),
        ("destination.txt", "move"),
    ]
    assert [(change.old_bytes, change.new_bytes) for change in staged.changes] == [
        (None, b"first\nsecond\n"),
        (b"keep\nold\n", b"keep\nnew\n"),
        (b"remove me\n", None),
        (b"move me\n", None),
        (None, b"move me\n"),
    ]
    assert "--- edited.txt\n+++ edited.txt\n" in staged.diff
    assert "--- moved.txt\n+++ /dev/null\n" in staged.diff
    assert "--- /dev/null\n+++ destination.txt\n" in staged.diff
    with pytest.raises(AttributeError):
        staged.changes[0].path = "changed.txt"  # type: ignore[misc]


def test_checks_exact_positions_across_multiple_hunks() -> None:
    operations = _parse(
        "*** Update File: file.txt\n"
        "@@ -2 +2 @@\n-b\n+B\n"
        "@@ -4,0 +5,1 @@\n+inserted\n"
    )

    staged = patch_stage.stage_patch(
        operations,
        {"file.txt": b"a\nb\nc\nd\ne\n"},
    )

    assert staged.changes[0].new_bytes == b"a\nB\nc\nd\ninserted\ne\n"


def test_rejects_position_mismatch_and_context_without_partial_result() -> None:
    operations = _parse(
        "*** Update File: first.txt\n@@ -1 +1 @@\n-before\n+after\n"
        "*** Update File: second.txt\n@@ -1 +3 @@\n-old\n+new\n"
    )

    with pytest.raises(patch_stage.PatchStageError) as caught:
        patch_stage.stage_patch(
            operations,
            {"first.txt": b"before\n", "second.txt": b"old\n"},
        )
    assert caught.value.code == "invalid_ranges"

    mismatch = _parse("*** Update File: file.txt\n@@ -1 +1 @@\n-expected\n+new\n")
    with pytest.raises(patch_stage.PatchStageError) as caught:
        patch_stage.stage_patch(mismatch, {"file.txt": b"actual\n"})
    assert caught.value.code == "context_mismatch"


@pytest.mark.parametrize("snapshot", [b"\xff\xfe", b"one\r\ntwo\n", b"nul\x00byte\n"])
def test_update_refuses_binary_or_non_lf_snapshots(snapshot: bytes) -> None:
    operations = _parse("*** Update File: file.txt\n@@ -1 +1 @@\n-one\n+two\n")

    with pytest.raises(patch_stage.PatchStageError) as caught:
        patch_stage.stage_patch(operations, {"file.txt": snapshot})

    assert caught.value.code == "binary_context"


@pytest.mark.parametrize(
    ("body", "snapshots", "code"),
    [
        ("*** Add File: new.txt\n+x\n", {"new.txt": b"occupied"}, "destination_exists"),
        ("*** Update File: absent.txt\n@@ -1 +1 @@\n-a\n+b\n", {"absent.txt": None}, "source_missing"),
        ("*** Delete File: absent.txt\n", {}, "missing_snapshot"),
        (
            "*** Move File: source.txt -> target.txt\n",
            {"source.txt": b"contents", "target.txt": b"occupied"},
            "destination_exists",
        ),
        (
            "*** Move File: absent.txt -> target.txt\n",
            {"absent.txt": None, "target.txt": None},
            "source_missing",
        ),
    ],
)
def test_validates_snapshot_existence_and_destinations(
    body: str, snapshots: dict[str, bytes | None], code: str
) -> None:
    with pytest.raises(patch_stage.PatchStageError) as caught:
        patch_stage.stage_patch(_parse(body), snapshots)

    assert caught.value.code == code


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"first\nold\nlast\n", b"first\nnew\nlast\n"),
        (b"first\nold\nlast", b"first\nnew\nlast"),
        (b"old", b"new"),
        (b"old\n", b"new\n"),
    ],
)
def test_preserves_lf_and_trailing_newline_semantics(source: bytes, expected: bytes) -> None:
    operations = _parse(
        "*** Update File: file.txt\n@@ -2 +2 @@\n-old\n+new\n"
        if source.startswith(b"first")
        else "*** Update File: file.txt\n@@ -1 +1 @@\n-old\n+new\n"
    )

    staged = patch_stage.stage_patch(operations, {"file.txt": source})

    assert staged.changes[0].new_bytes == expected


def test_combined_diff_is_bounded() -> None:
    operations = _parse("*** Add File: file.txt\n+line\n")
    old_limit = patch_stage.MAX_DIFF_CHARS
    patch_stage.MAX_DIFF_CHARS = 1
    try:
        with pytest.raises(patch_stage.PatchStageError) as caught:
            patch_stage.stage_patch(operations, {"file.txt": None})
        assert caught.value.code == "diff_too_large"
    finally:
        patch_stage.MAX_DIFF_CHARS = old_limit


def test_hand_built_surrogate_content_raises_typed_stage_error() -> None:
    operation = patch_parse.PatchOperation(
        "add",
        "file.txt",
        None,
        (patch_parse.PatchHunk(0, 0, 0, 1, (patch_parse.PatchLine("add", "\ud800"),)),),
    )

    with pytest.raises(patch_stage.PatchStageError) as caught:
        patch_stage.stage_patch((operation,), {"file.txt": None})

    assert caught.value.code == "invalid_content"


HAIKU = (
    b"Autumn leaves drift down\nMoonlight rests on quiet streams\n"
    b"Night holds its breath still\n\nSoft rain taps the roof\n"
    b"A small bird sings into dawn\nClouds wander away"
)
ADDED = b"\n\nSnow hushes the pines\nFootprints fade beneath the dusk\nStars kindle the sky"


@pytest.mark.parametrize(
    "patch",
    [
        # The exact patches gpt models sent in a real session, all once rejected.
        (
            "*** Begin Patch\n*** Update File: test.md\n@@\n Clouds wander away\n+\n"
            "+Snow hushes the pines\n+Footprints fade beneath the dusk\n+Stars kindle the sky\n"
            "*** End Patch"
        ),
        (
            "*** Begin Patch\n*** Update File: test.md\n@@\n Soft rain taps the roof\n"
            " A small bird sings into dawn\n Clouds wander away\n+\n+Snow hushes the pines\n"
            "+Footprints fade beneath the dusk\n+Stars kindle the sky\n*** End Patch\n"
        ),
        (
            "*** Begin Patch\n*** Update File: test.md\n@@ -4,3 +4,7 @@\n Soft rain taps the roof\n"
            " A small bird sings into dawn\n Clouds wander away\n+\n+Snow hushes the pines\n"
            "+Footprints fade beneath the dusk\n+Stars kindle the sky\n*** End Patch\n"
        ),
    ],
)
def test_codex_style_and_miscounted_hunks_locate_by_context(patch: str) -> None:
    staged = patch_stage.stage_patch(patch_parse.parse_patch(patch), {"test.md": HAIKU})

    assert staged.changes[0].new_bytes == HAIKU + ADDED


def test_context_hunks_honour_anchor_whitespace_and_end_of_file() -> None:
    operations = _parse(
        "*** Update File: file.py\n"
        "@@ def two():\n"
        "-    return 1\n"
        "+    return 2\n"
        "@@\n"
        " end  \n"
        "+appended\n"
        "*** End of File\n"
    )
    source = b"def one():\n    return 1\ndef two():\n    return 1\nend\n"

    staged = patch_stage.stage_patch(operations, {"file.py": source})

    assert staged.changes[0].new_bytes == (
        b"def one():\n    return 1\ndef two():\n    return 2\nend\nappended\n"
    )


def test_update_with_move_to_applies_hunks_at_destination() -> None:
    operations = _parse("*** Update File: a.txt\n*** Move to: b.txt\n@@\n-old\n+new\n")

    staged = patch_stage.stage_patch(operations, {"a.txt": b"old\n", "b.txt": None})

    assert [(c.path, c.old_bytes, c.new_bytes) for c in staged.changes] == [
        ("a.txt", b"old\n", None),
        ("b.txt", None, b"new\n"),
    ]


def test_context_mismatch_names_the_file() -> None:
    operations = _parse("*** Update File: file.txt\n@@\n-missing\n+new\n")

    with pytest.raises(patch_stage.PatchStageError) as caught:
        patch_stage.stage_patch(operations, {"file.txt": b"actual\n"})

    assert (caught.value.code, caught.value.path) == ("context_mismatch", "file.txt")
