from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, PermissionsSection, ToolsSection
from nexus.errors import OperationCancelled
from nexus.tools.builtin import apply_patch
from nexus.tools.builtin._patch_commit import PatchCommitError
from nexus.tools.manager import PreparedBatch, ToolManager
from nexus.tools.permissions import Decision, PermissionEngine, exact_rule
from nexus.tools.spec import ToolCall, ToolContext


def _patch(*operations: str) -> str:
    return "*** Begin Patch\n" + "".join(operations) + "*** End Patch\n"


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="s1",
        turn_id="t1",
        config=Config(
            v2=ConfigV2(
                permissions=PermissionsSection(mode="allow"),
                tools=ToolsSection(),
            )
        ),
    )


@pytest.mark.asyncio
async def test_applies_mixed_add_update_delete_and_move(tmp_path: Path) -> None:
    (tmp_path / "edited.txt").write_bytes(b"keep\nold\n")
    (tmp_path / "deleted.txt").write_bytes(b"remove\n")
    (tmp_path / "moved.txt").write_bytes(b"move\n")
    text = _patch(
        "*** Add File: added.txt\n+new file\n",
        "*** Update File: edited.txt\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n",
        "*** Delete File: deleted.txt\n",
        "*** Move File: moved.txt -> destination.txt\n",
    )

    result = await apply_patch.run({"patch": text}, _context(tmp_path))

    assert not result.is_error
    assert (tmp_path / "added.txt").read_bytes() == b"new file\n"
    assert (tmp_path / "edited.txt").read_bytes() == b"keep\nnew\n"
    assert not (tmp_path / "deleted.txt").exists()
    assert not (tmp_path / "moved.txt").exists()
    assert (tmp_path / "destination.txt").read_bytes() == b"move\n"
    body = result.content[0].text
    assert "added.txt: added" in body
    assert "edited.txt: updated" in body
    assert "deleted.txt: deleted" in body
    assert "destination.txt: added" in body
    assert "--- edited.txt" in body


@pytest.mark.asyncio
async def test_repeated_add_and_move_snapshots_close_directory_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[str] = []
    for index in range(12):
        operations.append(f"*** Add File: added-{index}.txt\n+new\n")
        source = tmp_path / f"source-{index}.txt"
        source.write_bytes(b"move\n")
        operations.append(f"*** Move File: source-{index}.txt -> moved-{index}.txt\n")

    original_directory_fd = apply_patch._directory_fd
    original_close = os.close
    outstanding: set[int] = set()
    opened: list[int] = []
    closed: list[int] = []

    def tracked_directory_fd(parent: Path) -> int:
        fd = original_directory_fd(parent)
        outstanding.add(fd)
        opened.append(fd)
        return fd

    def tracked_close(fd: int) -> None:
        if fd in outstanding:
            outstanding.remove(fd)
            closed.append(fd)
        original_close(fd)

    monkeypatch.setattr(apply_patch, "_directory_fd", tracked_directory_fd)
    monkeypatch.setattr(os, "close", tracked_close)

    result = await apply_patch.run({"patch": _patch(*operations)}, _context(tmp_path))

    assert not result.is_error
    assert len(opened) == len(closed)
    assert not outstanding


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "existing", "message"),
    [
        (_patch("*** Add File: already.txt\n+new\n"), {"already.txt": b"old"}, "destination"),
        (_patch("*** Update File: missing.txt\n@@ -1 +1 @@\n-old\n+new\n"), {}, "source"),
        (_patch("*** Delete File: absent.txt\n"), {}, "source"),
        (_patch("*** Move File: source.txt -> occupied.txt\n"), {"source.txt": b"x\n", "occupied.txt": b"y\n"}, "destination"),
        (_patch("*** Update File: file.txt\n@@ -1 +1 @@\n-expect\n+new\n"), {"file.txt": b"actual\n"}, "context"),
    ],
)
async def test_invalid_conflicts_do_not_mutate(
    tmp_path: Path, patch: str, existing: dict[str, bytes], message: str
) -> None:
    for name, contents in existing.items():
        (tmp_path / name).write_bytes(contents)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    result = await apply_patch.run({"patch": patch}, _context(tmp_path))

    assert result.is_error
    assert message in result.content[0].text.lower()
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert after == before


@pytest.mark.asyncio
async def test_rejects_symlinked_target_without_touching_referent(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-referent.txt"
    outside.write_bytes(b"old\n")
    (tmp_path / "link.txt").symlink_to(outside)

    result = await apply_patch.run(
        {"patch": _patch("*** Update File: link.txt\n@@ -1 +1 @@\n-old\n+new\n")},
        _context(tmp_path),
    )

    assert result.is_error
    assert "symlink" in result.content[0].text.lower()
    assert outside.read_bytes() == b"old\n"
    assert (tmp_path / "link.txt").is_symlink()


@pytest.mark.asyncio
async def test_snapshot_size_limit_rejects_before_mutation(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    with target.open("wb") as handle:
        handle.truncate(apply_patch.MAX_FILE_BYTES + 1)

    result = await apply_patch.run(
        {"patch": _patch("*** Delete File: large.txt\n")}, _context(tmp_path)
    )

    assert result.is_error
    assert "byte limit" in result.content[0].text
    assert target.stat().st_size == apply_patch.MAX_FILE_BYTES + 1


@pytest.mark.asyncio
async def test_cancellation_propagates_before_snapshot_or_commit(tmp_path: Path) -> None:
    class Cancel:
        def raise_if_cancelled(self) -> None:
            raise OperationCancelled("stop")

    target = tmp_path / "file.txt"
    target.write_bytes(b"old\n")
    ctx = dataclasses.replace(_context(tmp_path), cancel_token=Cancel())

    with pytest.raises(OperationCancelled):
        await apply_patch.run(
            {"patch": _patch("*** Delete File: file.txt\n")}, ctx
        )

    assert target.read_bytes() == b"old\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("move_role", ["source", "destination"])
async def test_manager_denies_every_move_target_before_runner(
    tmp_path: Path, move_role: str
) -> None:
    (tmp_path / "source.txt").write_bytes(b"move me\n")
    started: list[bool] = []

    async def runner(args, ctx):
        started.append(True)
        return await apply_patch.run(args, ctx)

    from nexus.tools.spec import RegisteredTool

    manager = ToolManager(
        _context(tmp_path).config,
        workspace=tmp_path,
        tools=(RegisteredTool(apply_patch.SPEC, runner),),
        tool_names=("apply_patch",),
    )
    patch = _patch("*** Move File: source.txt -> destination.txt\n")
    batch = manager.prepare([ToolCall(id="call", name="apply_patch", input={"patch": patch})])
    denied_path = tmp_path / ("source.txt" if move_role == "source" else "destination.txt")
    engine = PermissionEngine(
        mode="allow",
        deny=[exact_rule("apply_patch", str(denied_path))],
        path_guard=manager.path_guard,
    )
    plan = engine.plan(batch.calls(), batch.spec_map())
    assert plan.evaluations[0].outcome.value == "deny"
    assert any(item.key == str(denied_path) and item.outcome.value == "deny" for item in plan.evaluations[0].target_evaluations)

    result = await manager.dispatch(batch.apply_plan(plan), lambda *_: _context(tmp_path))

    assert started == []
    assert result[0].is_error
    assert (tmp_path / "source.txt").read_bytes() == b"move me\n"
    assert not (tmp_path / "destination.txt").exists()


@pytest.mark.asyncio
async def test_authorization_cannot_be_reused_for_altered_patch(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_bytes(b"move me\n")
    started: list[bool] = []

    async def runner(args, ctx):
        started.append(True)
        return await apply_patch.run(args, ctx)

    from nexus.tools.spec import RegisteredTool

    manager = ToolManager(
        _context(tmp_path).config,
        workspace=tmp_path,
        tools=(RegisteredTool(apply_patch.SPEC, runner),),
        tool_names=("apply_patch",),
    )
    first = _patch("*** Move File: source.txt -> destination.txt\n")
    batch = manager.prepare([ToolCall(id="call", name="apply_patch", input={"patch": first})])
    plan = PermissionEngine(mode="allow", path_guard=manager.path_guard).plan(
        batch.calls(), batch.spec_map()
    )
    authorized = manager._authorize_multi_target(
        batch,
        plan.evaluations[0],
        Decision.ALLOW_ONCE,
        authority=manager._multi_target_authority,
    )
    entry = authorized.entries[0]
    altered = ToolCall(
        id="call",
        name="apply_patch",
        input={"patch": _patch("*** Move File: source.txt -> elsewhere.txt\n")},
    )
    forged = PreparedBatch((dataclasses.replace(entry, call=altered, decision=Decision.ALLOW_ONCE),))

    result = await manager.dispatch(forged, lambda *_: _context(tmp_path))

    assert started == []
    assert result[0].is_error
    assert (tmp_path / "source.txt").read_bytes() == b"move me\n"
    assert not (tmp_path / "destination.txt").exists()
    assert not (tmp_path / "elsewhere.txt").exists()


@pytest.mark.asyncio
async def test_reports_unrestored_file_when_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_bytes(b"old-a\n")
    (tmp_path / "b.txt").write_bytes(b"old-b\n")
    original = apply_patch.commit_staged

    async def fail_commit(staged, guard, workspace=None, *, ctx=None, cancellation=None):
        (tmp_path / "a.txt").write_bytes(b"new-a\n")
        raise PatchCommitError(
            "injected rollback failure",
            failure_path="b.txt",
            committed_paths=("a.txt",),
            restored_paths=(),
            unrestored_paths=("a.txt",),
        )

    monkeypatch.setattr(apply_patch, "commit_staged", fail_commit)
    result = await apply_patch.run(
        {
            "patch": _patch(
                "*** Update File: a.txt\n@@ -1 +1 @@\n-old-a\n+new-a\n",
                "*** Update File: b.txt\n@@ -1 +1 @@\n-old-b\n+new-b\n",
            )
        },
        _context(tmp_path),
    )
    monkeypatch.setattr(apply_patch, "commit_staged", original)

    assert result.is_error
    assert "a.txt=unrestored" in result.content[0].text
    assert "b.txt=unchanged" in result.content[0].text
    assert "exact partial state" in result.content[0].text
    assert (tmp_path / "a.txt").read_bytes() == b"new-a\n"
    assert (tmp_path / "b.txt").read_bytes() == b"old-b\n"


def test_apply_patch_spec_and_coding_catalog_registration() -> None:
    from nexus.tools.builtin import BUILTIN_TOOLS

    assert apply_patch.SPEC.name == "apply_patch"
    assert apply_patch.SPEC.input_schema == {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "The full patch text, from *** Begin Patch to *** End Patch.",
            }
        },
        "required": ["patch"],
        "additionalProperties": False,
    }
    assert apply_patch.SPEC.mutates and apply_patch.SPEC.concurrency == "exclusive"
    assert any(tool.name == "apply_patch" for tool in BUILTIN_TOOLS)
    assert apply_patch.SPEC.bundle == "patch"
    cfg = _context(Path.cwd()).config
    coding = ToolManager(cfg, workspace=Path.cwd())
    research = ToolManager(cfg, workspace=Path.cwd(), profile="research")
    assert "apply_patch" in coding.names
    assert "apply_patch" not in research.names


@pytest.mark.asyncio
async def test_applies_codex_context_patch_and_reports_a_per_file_diff(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\nthree")
    (tmp_path / "b.txt").write_bytes(b"keep\nold\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n@@\n three\n+four\n"
        "*** Update File: b.txt\n*** Move to: c.txt\n@@ keep\n-old\n+new\n"
        "*** End Patch"
    )

    result = await apply_patch.run({"patch": patch}, _context(tmp_path))

    assert not result.is_error, result.display
    assert (tmp_path / "a.txt").read_bytes() == b"one\ntwo\nthree\nfour"
    assert not (tmp_path / "b.txt").exists()
    assert (tmp_path / "c.txt").read_bytes() == b"keep\nnew\n"
    diff = result.diff
    assert diff is not None
    assert (diff["path"], diff["added_lines"], diff["removed_lines"]) == ("2 files", 2, 1)
    assert "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,4 @@" in diff["hunk"]
    assert "--- a/b.txt\n+++ b/c.txt\n" in diff["hunk"]


@pytest.mark.asyncio
async def test_context_mismatch_names_the_file_and_hints_a_reread(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"actual\n")

    result = await apply_patch.run(
        {"patch": _patch("*** Update File: a.txt\n@@\n-expected\n+new\n")}, _context(tmp_path)
    )

    assert result.is_error
    assert result.display == (
        "apply_patch failed in a.txt: hunk context does not match the source snapshot; "
        "re-read the file and copy the context lines exactly"
    )
