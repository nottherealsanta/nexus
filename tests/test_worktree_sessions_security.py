"""Session-bound worktree tools never cross into child or sibling authority."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import AgentsSection, ConfigV2, ModelSection, PermissionsSection, WorktreesSection
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.tools.spec import ToolContext


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


async def _turn(handle, prompt: str) -> None:
    async for _event in handle.send(prompt):
        pass


async def test_root_worktree_tools_are_not_in_child_authority_and_reject_sibling_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.name", "Tests")
    _git(project, "config", "user.email", "tests@example.invalid")
    (project / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(project, "add", "seed.txt")
    _git(project, "commit", "-m", "initial")

    config = Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            permissions=PermissionsSection(mode="allow"),
            agents=AgentsSection(enabled=True, max_depth=1),
            worktrees=WorktreesSection(),
        ),
    )
    provider = ScriptedProvider(text_response("done"), text_response("done"))
    runtime = Runtime(
        project,
        home=tmp_path / "home",
        config=config,
        providers={"scripted": provider},
    )
    captured_runners = []
    original = Runtime._make_subagent_runner

    def capture_runner(self, **kwargs):
        runner = original(self, **kwargs)
        if self is runtime and runner is not None:
            captured_runners.append(runner)
        return runner

    monkeypatch.setattr(Runtime, "_make_subagent_runner", capture_runner)
    try:
        parent = runtime.session("parent")
        await runtime.promote_session(parent.id, reason="isolate the parent")
        await _turn(parent, "work in the owned worktree")
        parent_root = runtime.session_root(parent.id)
        assert parent_root.kind == "worktree"
        root_tools = {tool.name for tool in provider.requests[0].tools}
        assert {"worktree_list", "worktree_create", "worktree_move", "worktree_commit"} <= root_tools
        assert captured_runners
        assert all(
            not {"worktree_list", "worktree_create", "worktree_move", "worktree_commit"} & runner.parent_tools
            for runner in captured_runners
        )

        sibling = runtime.session("sibling")
        await runtime.promote_session(sibling.id, reason="isolate the sibling")
        await _turn(sibling, "work in a separate worktree")
        sibling_root = runtime.session_root(sibling.id)
        assert sibling_root.kind == "worktree"
        assert sibling_root.path != parent_root.path

        # A closure built for the parent must reject a context carrying the
        # sibling session ID before it can commit the parent's dirty file.
        (parent_root.path / "parent-only.txt").write_text("uncommitted\n", encoding="utf-8")
        original_head = _git(parent_root.path, "rev-parse", "HEAD")
        commit = next(
            tool for tool in runtime._session_worktree_tools(parent.id)
            if tool.spec.name == "worktree_commit"
        )
        context = ToolContext(
            workspace=sibling_root.path,
            session_id=sibling.id,
            turn_id="sibling-turn",
            call_id="cross-session-call",
            config=config,
        )
        result = await commit.run({"message": "should not execute"}, context)
        assert result.is_error
        assert _git(parent_root.path, "rev-parse", "HEAD") == original_head
        assert _git(parent_root.path, "status", "--porcelain") == "?? parent-only.txt"
    finally:
        await runtime.aclose()
