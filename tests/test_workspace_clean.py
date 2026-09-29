"""STATE_PLAN §6: a scripted turn must not resurrect ``<workspace>/.nexus``.

Session persistence moved out of per-project files and into one shared SQLite
database under ``NEXUS_HOME`` (STATE_PLAN §2, §4). A fresh workspace must never
grow a ``.nexus/sessions`` or ``.nexus/trash`` directory again; the durable
record of a turn lives in the state database instead.
"""
from __future__ import annotations

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


def _config() -> Config:
    return Config(
        model="scripted/sm", version=2, v2=ConfigV2(model=ModelSection(default="scripted/sm"))
    )


async def test_scripted_turn_leaves_no_legacy_sessions_directory(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(workspace, config=_config(), providers={"scripted": provider})
    try:
        session = runtime.session("main")
        events = [event async for event in session.send("hello")]
        assert events[-1].type == "turn.completed"
        # The turn's record is durable -- but in the state database, not a file.
        assert runtime.sessions.exists("main")
        assert runtime.sessions.summary("main").message_count == 1
    finally:
        await runtime.aclose()

    assert not (workspace / ".nexus" / "sessions").exists()
    assert not (workspace / ".nexus" / "trash").exists()


async def test_fresh_workspace_writes_no_session_state_before_any_turn(tmp_path):
    workspace = tmp_path / "untouched"
    workspace.mkdir()
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(workspace, config=_config(), providers={"scripted": provider})
    try:
        pass
    finally:
        await runtime.aclose()

    assert not (workspace / ".nexus" / "sessions").exists()
    assert not (workspace / ".nexus" / "trash").exists()


async def test_deleted_and_restored_session_still_leaves_no_sessions_directory(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(workspace, config=_config(), providers={"scripted": provider})
    try:
        session = runtime.session("main")
        async for _ in session.send("hello"):
            pass
        record = runtime.sessions.delete("main")
        assert not runtime.sessions.exists("main")
        restored = runtime.sessions.restore(record.trash_id)
        assert restored == "main"
        assert runtime.sessions.exists("main")
    finally:
        await runtime.aclose()

    assert not (workspace / ".nexus" / "sessions").exists()
    assert not (workspace / ".nexus" / "trash").exists()
