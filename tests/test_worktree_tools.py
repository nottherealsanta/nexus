from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, PermissionsSection, ToolsSection
from nexus.tools.builtin.worktree import build_checkout_tools
from nexus.tools.permissions import Outcome, PermissionEngine, parse_rule
from nexus.tools.spec import ToolCall, ToolContext


def _ctx(root: Path) -> ToolContext:
    return ToolContext(
        workspace=root, session_id="s1", turn_id="t1",
        config=Config(v2=ConfigV2(
            permissions=PermissionsSection(mode="allow"), tools=ToolsSection(),
        )),
    )


_STATUS = {
    "current": "main", "max_worktrees": 16, "present": 1, "clipped": 0,
    "main": {"current": True, "branch": "main", "path": "/p", "dirty": 2, "sessions": []},
    "worktrees": [{"name": "brave-otter", "branch": "nexus/wt/brave-otter", "base_branch": "main",
                   "base_commit": "1a2b3c4d", "present": True, "dirty": 0, "merged": False,
                   "has_work": True, "unmerged_commits": 3,
                   "sessions": [{"title": "refactor", "active": True}], "current": False}],
    "external": [],
    "here": {"kind": "main", "checkout": "main checkout", "branch": "main",
             "others": [{"id": "s2", "title": "auth fix", "active": True}],
             "mine": ["src/a.py"], "not_mine": ["docs/b.md"], "mine_count": 1, "not_mine_count": 1,
             "unmerged_commits": 0},
}


@pytest.mark.asyncio
async def test_checkout_is_one_tool_with_actions_bound_to_callbacks(tmp_path: Path) -> None:
    seen = []

    async def status(args, ctx):
        seen.append(("status", args, ctx.session_id))
        return _STATUS

    def commit(args, ctx):
        seen.append(("commit", args["paths"], ctx.turn_id))
        return "committed"

    def isolate(args, ctx):
        seen.append(("isolate", args["reason"], args.get("name")))
        return "queued isolate"

    def switch(args, ctx):
        seen.append(("switch", args["to"], args["reason"]))
        return "queued switch"

    tools = build_checkout_tools(status, commit, isolate, switch)
    assert [item.spec.name for item in tools] == ["checkout"]
    tool = tools[0]
    assert tool.spec.mutates and tool.spec.concurrency == "exclusive"
    assert tool.spec.input_schema["properties"]["action"]["enum"] == ["status", "isolate", "switch", "commit"]
    assert "after the current step" in tool.spec.description

    text = (await tool.run({"action": "status", "reason": "ignored"}, _ctx(tmp_path))).content[0].text
    assert text.startswith("you are in: main checkout (branch main)")
    assert "other sessions here: auth fix (active)" in text
    assert "1 written by you, 1 not yours" in text and "yours: src/a.py" in text and "not yours: docs/b.md" in text
    assert "isolate before editing" in text
    assert "- main  ← you are here" in text
    assert "- brave-otter" in text and "unmerged (3 commits not in the base)" in text and "{" not in text
    assert (await tool.run({"action": "commit", "message": "fix", "paths": ["src/a.py"]}, _ctx(tmp_path))).content[0].text == "committed"
    assert (await tool.run({"action": "isolate", "reason": "refactor", "name": "x1", "to": "main"}, _ctx(tmp_path))).content[0].text == "queued isolate"
    assert (await tool.run({"action": "switch", "to": "main", "reason": "done"}, _ctx(tmp_path))).content[0].text == "queued switch"
    # each callback gets only the fields its action reads
    assert seen == [
        ("status", {}, "s1"), ("commit", ["src/a.py"], "t1"),
        ("isolate", "refactor", "x1"), ("switch", "main", "done"),
    ]


def test_checkout_definition_never_depends_on_where_the_session_is() -> None:
    """The prompt-cache rule: the same schema and description before and after a move."""
    first = build_checkout_tools(lambda *_: {}, None, None, None)[0].spec
    second = build_checkout_tools(lambda *_: {}, lambda *_: "x", lambda *_: "x", lambda *_: "x")[0].spec
    assert first.input_schema == second.input_schema and first.description == second.description


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "allow", "deny"])
async def test_status_isolate_and_switch_never_ask_but_commit_follows_the_rules(tmp_path: Path, mode: str) -> None:
    spec = build_checkout_tools(lambda *_: {}, None, lambda *_: "ok", lambda *_: "ok")[0].spec
    engine = PermissionEngine(mode=mode)
    for args in ({"action": "status"}, {"action": "isolate", "reason": "r"}, {"action": "switch", "to": "main", "reason": "r"}):
        verdict = engine.evaluate(ToolCall(id="c", name="checkout", input=args), spec)
        assert verdict.outcome is Outcome.ALLOW, (mode, args, verdict)
    commit = engine.evaluate(ToolCall(id="c", name="checkout", input={"action": "commit", "message": "m"}), spec)
    assert commit.outcome is {"ask": Outcome.ASK, "allow": Outcome.ALLOW, "deny": Outcome.DENY}[mode]
    assert spec.resolve_permission_key({"action": "commit", "message": "m"}) == "commit"
    assert parse_rule("checkout(commit)")
    allowed = PermissionEngine(mode="ask", allow=("checkout(commit)",))
    assert allowed.evaluate(ToolCall(id="c", name="checkout", input={"action": "commit", "message": "m"}), spec).outcome is Outcome.ALLOW
    # an explicit deny rule still wins
    denied = PermissionEngine(mode="allow", deny=("checkout",))
    assert denied.evaluate(ToolCall(id="d", name="checkout", input={"action": "switch", "to": "main", "reason": "r"}),
                           spec).outcome is Outcome.DENY


@pytest.mark.asyncio
async def test_checkout_bounds_arguments_and_requires_bound_callbacks(tmp_path: Path) -> None:
    calls = []
    record = lambda args, _ctx: calls.append(args) or "ok"  # noqa: E731
    tool = build_checkout_tools(lambda *_: "ok", record, record, record)[0]
    assert (await tool.run({}, _ctx(tmp_path))).is_error
    assert (await tool.run({"action": "merge"}, _ctx(tmp_path))).is_error
    assert (await tool.run({"action": "commit", "message": "\x00"}, _ctx(tmp_path))).is_error
    assert (await tool.run({"action": "commit", "message": "ok", "paths": ["a"] * 501}, _ctx(tmp_path))).is_error
    assert (await tool.run({"action": "isolate", "reason": " "}, _ctx(tmp_path))).is_error
    assert (await tool.run({"action": "isolate", "reason": "r", "name": "n" * 61}, _ctx(tmp_path))).is_error
    assert (await tool.run({"action": "switch", "to": "", "reason": "r"}, _ctx(tmp_path))).is_error
    assert calls == []

    unbound = build_checkout_tools(lambda *_: "ok", None, None, None)[0]
    result = await unbound.run({"action": "isolate", "reason": "move"}, _ctx(tmp_path))
    assert result.is_error and "checkout isolate" in result.content[0].text
    assert (await unbound.run({"action": "switch", "to": "main", "reason": "move"}, _ctx(tmp_path))).is_error


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
