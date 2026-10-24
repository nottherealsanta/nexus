from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, PermissionsSection, ToolsSection
from nexus.tools.builtin.worktree import build_worktree_tools
from nexus.tools.permissions import Outcome, PermissionEngine, parse_rule
from nexus.tools.spec import ToolCall, ToolContext


def _ctx(root: Path) -> ToolContext:
    return ToolContext(
        workspace=root, session_id="s1", turn_id="t1",
        config=Config(v2=ConfigV2(
            permissions=PermissionsSection(mode="allow"), tools=ToolsSection(),
        )),
    )


@pytest.mark.asyncio
async def test_dynamic_tools_are_bound_and_root_specific(tmp_path: Path) -> None:
    seen = []

    async def listing(args, ctx):
        seen.append(("list", args, ctx.session_id))
        return {
            "current": "main", "max_worktrees": 16, "present": 1, "clipped": 0,
            "main": {"current": True, "branch": "main", "path": "/p", "dirty": 0, "sessions": []},
            "worktrees": [{"name": "brave-otter", "branch": "nexus/wt/brave-otter", "base_branch": "main",
                           "base_commit": "1a2b3c4d", "present": True, "dirty": 0, "merged": False,
                           "has_work": True, "unmerged_commits": 3,
                           "sessions": [{"title": "refactor", "active": True}], "current": False}],
            "external": [],
        }

    def commit(args, ctx):
        seen.append(("commit", args["paths"], ctx.turn_id))
        return "committed"

    def create(args, ctx):
        seen.append(("create", args["reason"], args.get("name")))
        return "queued create"

    def move(args, ctx):
        seen.append(("move", args["to"], args["reason"]))
        return "queued move"

    inside = build_worktree_tools(listing, commit, create, move, in_worktree=True)
    outside = build_worktree_tools(listing, commit, create, move, in_worktree=False)
    assert [item.spec.name for item in inside] == ["worktree_list", "worktree_create", "worktree_move", "worktree_commit"]
    assert [item.spec.name for item in outside] == ["worktree_list", "worktree_create", "worktree_move"]
    by_name = {item.spec.name: item for item in inside}
    assert not by_name["worktree_list"].spec.mutates
    for name in ("worktree_create", "worktree_move", "worktree_commit"):
        assert by_name[name].spec.mutates and by_name[name].spec.concurrency == "exclusive"
    for name in ("worktree_create", "worktree_move"):
        assert "after this step" in by_name[name].spec.description

    text = (await by_name["worktree_list"].run({}, _ctx(tmp_path))).content[0].text
    assert "- main  ← you are here" in text
    assert "- brave-otter" in text and "unmerged (3 commits not in the base)" in text
    assert "refactor (active)" in text and "{" not in text
    assert (await by_name["worktree_commit"].run({"message": "fix", "paths": ["src/a.py"]}, _ctx(tmp_path))).content[0].text == "committed"
    assert (await by_name["worktree_create"].run({"reason": "refactor", "name": "x1"}, _ctx(tmp_path))).content[0].text == "queued create"
    assert (await by_name["worktree_move"].run({"to": "main", "reason": "done"}, _ctx(tmp_path))).content[0].text == "queued move"
    assert seen == [
        ("list", {}, "s1"), ("commit", ["src/a.py"], "t1"),
        ("create", "refactor", "x1"), ("move", "main", "done"),
    ]
    commit_key = by_name["worktree_commit"].spec.resolve_permission_key({"message": "x"})
    assert commit_key == "worktree_commit"
    assert parse_rule(f"worktree_commit({commit_key})")
    allowed = PermissionEngine(mode="ask", allow=(f"worktree_commit({commit_key})",))
    assert allowed.evaluate(ToolCall(id="c", name="worktree_commit", input={"message": "x"}), by_name["worktree_commit"].spec).outcome is Outcome.ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "allow", "deny"])
async def test_list_create_and_move_never_ask(tmp_path: Path, mode: str) -> None:
    tools = {item.spec.name: item for item in build_worktree_tools(
        lambda *_: {}, None, lambda *_: "ok", lambda *_: "ok", in_worktree=False)}
    engine = PermissionEngine(mode=mode)
    for name, args in (("worktree_list", {}), ("worktree_create", {"reason": "r"}), ("worktree_move", {"to": "main", "reason": "r"})):
        verdict = engine.evaluate(ToolCall(id=name, name=name, input=args), tools[name].spec)
        assert verdict.outcome is Outcome.ALLOW, (mode, name, verdict)
    # an explicit deny rule still wins
    denied = PermissionEngine(mode="allow", deny=("worktree_move",))
    assert denied.evaluate(ToolCall(id="d", name="worktree_move", input={"to": "main", "reason": "r"}),
                           tools["worktree_move"].spec).outcome is Outcome.DENY


@pytest.mark.asyncio
async def test_worktree_tool_bounds_arguments_and_requires_bound_callbacks(tmp_path: Path) -> None:
    calls = []
    tools = {item.spec.name: item for item in build_worktree_tools(
        lambda *_: "ok", lambda args, _ctx: calls.append(args) or "ok",
        lambda args, _ctx: calls.append(args) or "ok", lambda args, _ctx: calls.append(args) or "ok", in_worktree=True)}
    assert (await tools["worktree_commit"].run({"message": "\x00"}, _ctx(tmp_path))).is_error
    assert (await tools["worktree_commit"].run({"message": "ok", "paths": ["a"] * 501}, _ctx(tmp_path))).is_error
    assert (await tools["worktree_create"].run({"reason": " "}, _ctx(tmp_path))).is_error
    assert (await tools["worktree_create"].run({"reason": "r", "name": "n" * 61}, _ctx(tmp_path))).is_error
    assert (await tools["worktree_move"].run({"to": "", "reason": "r"}, _ctx(tmp_path))).is_error
    assert calls == []

    unbound = {item.spec.name: item for item in build_worktree_tools(lambda *_: "ok", None, None, None, in_worktree=False)}
    assert (await unbound["worktree_create"].run({"reason": "move"}, _ctx(tmp_path))).is_error
    assert (await unbound["worktree_move"].run({"to": "main", "reason": "move"}, _ctx(tmp_path))).is_error


def test_worktree_tools_do_not_import_runtime() -> None:
    source = Path(__file__).resolve().parents[1] / "nexus" / "tools" / "builtin" / "worktree.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assert all(
        not (isinstance(node, ast.Import) and any(alias.name == "nexus.runtime" for alias in node.names))
        and not (isinstance(node, ast.ImportFrom) and node.module == "nexus.runtime")
        for node in ast.walk(tree)
    )


@pytest.mark.asyncio
async def test_write_and_patch_emit_receipts_only_for_successful_changes(tmp_path: Path) -> None:
    from nexus.tools.builtin import apply_patch, edit, multiedit, write
    from nexus.tools.builtin.write import git_blob_oids

    assert git_blob_oids(b"before") == {
        "sha1": "ee2363a4dfc456efa66b2e71979a1f8f8f07a208",
        "sha256": "0d1a1bcb9127456ebaaa1eb55b82ef2b65f5cd9ce97f156446ed4b2476b9d3ea",
    }
    receipts: list[tuple[Path, bytes | None, dict[str, str] | None]] = []
    ctx = dataclasses.replace(
        _ctx(tmp_path), record_write=lambda path, data, before: receipts.append((path, data, before))
    )

    result = await write.run({"path": "written.txt", "content": "hello"}, ctx)
    assert not result.is_error
    assert receipts == [(tmp_path / "written.txt", b"hello", None)]

    (tmp_path / "edited.txt").write_text("before", encoding="utf-8")
    result = await edit.run({
        "path": "edited.txt", "old_string": "before", "new_string": "after",
    }, ctx)
    assert not result.is_error
    assert receipts[-1] == (tmp_path / "edited.txt", b"after", git_blob_oids(b"before"))

    (tmp_path / "multi.txt").write_text("one", encoding="utf-8")
    result = await multiedit.run({
        "path": "multi.txt", "edits": [{"old_string": "one", "new_string": "two"}],
    }, ctx)
    assert not result.is_error
    assert receipts[-1] == (tmp_path / "multi.txt", b"two", git_blob_oids(b"one"))

    patch = (
        "*** Begin Patch\n"
        "*** Add File: added.txt\n+new\n"
        "*** Delete File: written.txt\n"
        "*** End Patch\n"
    )
    result = await apply_patch.run({"patch": patch}, ctx)
    assert not result.is_error
    assert receipts[-2:] == [
        (tmp_path / "added.txt", b"new\n", None),
        (tmp_path / "written.txt", None, git_blob_oids(b"hello")),
    ]
    assert not (tmp_path / "written.txt").exists()
    assert (tmp_path / "added.txt").read_bytes() == b"new\n"


@pytest.mark.asyncio
async def test_write_receipt_marks_unbounded_before_image_unknown(tmp_path: Path) -> None:
    from nexus.tools.builtin import write

    (tmp_path / "large.txt").write_bytes(b"x" * (50 * 1024 * 1024 + 1))
    receipts = []
    ctx = dataclasses.replace(
        _ctx(tmp_path), record_write=lambda path, data, before: receipts.append(before)
    )
    result = await write.run({"path": "large.txt", "content": "replacement"}, ctx)
    assert not result.is_error
    assert receipts == [{"unknown": "too_large"}]


@pytest.mark.asyncio
async def test_patch_rollback_does_not_leave_write_receipts(tmp_path: Path, monkeypatch) -> None:
    from nexus.tools.builtin import apply_patch
    from nexus.tools.builtin import _patch_commit

    (tmp_path / "a.txt").write_bytes(b"old-a\n")
    (tmp_path / "b.txt").write_bytes(b"old-b\n")
    receipts: list[tuple[Path, bytes | None, dict[str, str] | None]] = []
    ctx = dataclasses.replace(
        _ctx(tmp_path), record_write=lambda path, data, before: receipts.append((path, data, before))
    )
    real_write = _patch_commit.atomic_write_bytes
    failed = False

    async def fail_second_write(write_ctx, target, data, *, create_parents):
        nonlocal failed
        if target.name == "b.txt" and not failed:
            failed = True
            raise OSError("injected commit failure")
        await real_write(write_ctx, target, data, create_parents=create_parents)

    monkeypatch.setattr(_patch_commit, "atomic_write_bytes", fail_second_write)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n@@ -1 +1 @@\n-old-a\n+new-a\n"
        "*** Update File: b.txt\n@@ -1 +1 @@\n-old-b\n+new-b\n"
        "*** End Patch\n"
    )
    result = await apply_patch.run({"patch": patch}, ctx)
    assert result.is_error
    assert (tmp_path / "a.txt").read_bytes() == b"old-a\n"
    assert (tmp_path / "b.txt").read_bytes() == b"old-b\n"
    assert receipts == []
