"""Main-checkout checkpoints are gone: per-turn history exists only in worktrees (plan 3.5.1)."""
from __future__ import annotations

import pytest

from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from test_worktree_sessions_runtime import drain, git, project as _project, runtime


@pytest.fixture
def project(tmp_path):
    return _project.__wrapped__(tmp_path)


async def test_a_turn_in_the_main_checkout_records_nothing(project, tmp_path):
    provider = ScriptedProvider(tool_response(("write", "write", {"path": "result.txt", "content": "x\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("main-turn")
        events = await drain(handle, "write a file in main")
        assert events[-1].type == "turn.completed", events[-1]
        assert not any(e.type.startswith("checkpoint.") for e in handle.events)
        assert not any(e.type in {"worktree.committed", "worktree.turn_started"} for e in handle.events)
        assert git(project, "for-each-ref", "refs/nexus/") == ""
        assert (project / "result.txt").exists()
    finally:
        await rt.aclose()


async def test_a_turn_in_a_worktree_records_a_commit_and_a_hidden_ref(project, tmp_path):
    provider = ScriptedProvider(tool_response(("write", "write", {"path": "result.txt", "content": "x\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("wt-turn")
        await rt.promote_session(handle.id, reason="isolate")
        events = await drain(handle, "write a file in the worktree")
        assert events[-1].type == "turn.completed", events[-1]
        commit = next(e for e in handle.events if e.type == "worktree.committed")
        refs = git(project, "for-each-ref", "--format=%(objectname)", "refs/nexus/turns/")
        assert commit.data["sha"] in refs.split()
        assert not any(e.type.startswith("checkpoint.") for e in handle.events)
    finally:
        await rt.aclose()


async def test_the_checkpoint_config_keys_are_removed():
    from nexus.config.schema import WorktreesSection
    assert not set(WorktreesSection.__struct_fields__) & {"sessions", "checkpoints"}
