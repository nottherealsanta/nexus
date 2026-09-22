"""Phase 1 ContextManager tests (plan sections 4, 5.2)."""
import os
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import AgentSection, ConfigV2, ModelParams, ModelSection
from nexus.context import ContextManager, Exchange, build_context
from nexus.errors import ConfigError
from nexus.model.message import Message, Text, ToolUse
from nexus.model.request import ToolSchema


class FakeSession:
    """The only surface ``assemble`` reads: structured history."""

    def __init__(self, *messages):
        self.id = "s1"
        self._messages = list(messages)

    @property
    def messages(self):
        return list(self._messages)


def v2_config(*, model="anthropic/claude-test", temperature=None, max_output_tokens=None):
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(
                default=model,
                params=ModelParams(
                    temperature=temperature, max_output_tokens=max_output_tokens
                ),
            )
        ),
    )


def write_workspace(tmp_path: Path, soul="SOUL-BODY", memory="MEMORY-BODY") -> Path:
    if soul is not None:
        (tmp_path / "SOUL.md").write_text(soul, encoding="utf-8")
    if memory is not None:
        (tmp_path / "MEMORY.md").write_text(memory, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# System text
# ---------------------------------------------------------------------------


def test_system_text_is_identity_then_soul_then_memory(tmp_path):
    write_workspace(tmp_path)
    manager = ContextManager(tmp_path, config=v2_config())

    request = manager.assemble(FakeSession())

    system = request.system
    assert system.startswith("You are Nexus")
    assert system.index("SOUL-BODY") < system.index("MEMORY-BODY")


def test_missing_system_files_contribute_nothing(tmp_path):
    manager = ContextManager(tmp_path, config=v2_config())
    request = manager.assemble(FakeSession())
    assert request.system.startswith("You are Nexus")
    assert "SOUL" not in request.system
    assert "MEMORY" not in request.system


def test_configured_filenames_are_used(tmp_path):
    (tmp_path / "RULES.md").write_text("CUSTOM-SOUL", encoding="utf-8")
    (tmp_path / "NOTES.md").write_text("CUSTOM-MEMORY", encoding="utf-8")
    config = Config(
        instructions_file="RULES.md", memory_file="NOTES.md", model="anthropic/x"
    )
    manager = ContextManager(tmp_path, config=config)

    system = manager.assemble(FakeSession()).system
    assert "CUSTOM-SOUL" in system
    assert "CUSTOM-MEMORY" in system


def test_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside-soul.md"
    outside.write_text("OUTSIDE", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os.symlink(outside, workspace / "SOUL.md")
    manager = ContextManager(workspace, config=v2_config())

    with pytest.raises(ConfigError):
        manager.assemble(FakeSession())


def test_parent_directory_escape_is_rejected(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = Config(instructions_file="../escape.md", memory_file="MEMORY.md")
    manager = ContextManager(workspace, config=config)
    with pytest.raises(ConfigError):
        manager.assemble(FakeSession())


def test_oversized_system_file_is_rejected(tmp_path):
    (tmp_path / "SOUL.md").write_text("x" * 64, encoding="utf-8")
    manager = ContextManager(tmp_path, config=v2_config(), max_file_bytes=10)
    with pytest.raises(ConfigError, match="too large"):
        manager.assemble(FakeSession())


def test_manager_requires_a_config_source(tmp_path):
    with pytest.raises(ConfigError):
        ContextManager(tmp_path)


# ---------------------------------------------------------------------------
# Structured history
# ---------------------------------------------------------------------------


def test_history_is_complete_structured_and_user_appears_once(tmp_path):
    messages = [
        Message(role="user", content=[Text(text="old question")]),
        Message(
            role="assistant",
            content=[
                Text(text="using a tool"),
                ToolUse(id="call-1", name="Read", input={"path": "a.txt"}),
            ],
        ),
        Message(role="user", content=[Text(text="current request")]),
    ]
    manager = ContextManager(tmp_path, config=v2_config())

    request = manager.assemble(FakeSession(*messages))

    assert request.messages == messages
    assert [m.role for m in request.messages] == ["user", "assistant", "user"]
    assert [type(b).__name__ for b in request.messages[1].content] == [
        "Text",
        "ToolUse",
    ]
    current_users = [
        m for m in request.messages if m.role == "user" and m.content[0].text == "current request"
    ]
    assert len(current_users) == 1


def test_no_tool_schemas_are_sent(tmp_path):
    manager = ContextManager(tmp_path, config=v2_config())
    assert manager.assemble(FakeSession(Message(role="user", content=[Text("hi")]))).tools == []


def test_frozen_tool_schemas_reach_the_request_not_the_system(tmp_path):
    schema = ToolSchema(
        name="Read",
        description="Read a file from disk",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    manager = ContextManager(tmp_path, config=v2_config())
    snapshot = manager.for_turn()
    snapshot.freeze_tools([schema])

    request = snapshot.assemble(FakeSession(Message(role="user", content=[Text("hi")])))

    assert request.tools == [schema]
    # Descriptions live on the schemas only, never interpolated into system text.
    assert "Read a file from disk" not in (request.system or "")


def test_for_turn_copies_existing_tool_schemas(tmp_path):
    schema = ToolSchema(name="Glob", description="glob", input_schema={"type": "object"})
    manager = ContextManager(tmp_path, config=v2_config())
    manager.freeze_tools([schema])
    assert manager.for_turn().assemble(FakeSession()).tools == [schema]


def test_freeze_tools_rejects_non_schemas(tmp_path):
    manager = ContextManager(tmp_path, config=v2_config())
    with pytest.raises(TypeError):
        manager.freeze_tools([object()])


# ---------------------------------------------------------------------------
# Model / sampling mapping
# ---------------------------------------------------------------------------


def test_v2_model_and_sampling_map_onto_request(tmp_path):
    config = v2_config(
        model="anthropic/claude-opus-5", temperature=0.25, max_output_tokens=4096
    )
    manager = ContextManager(tmp_path, config=config)

    request = manager.assemble(FakeSession())

    assert request.provider == "anthropic"
    assert request.model == "claude-opus-5"
    assert request.params.temperature == 0.25
    assert request.params.max_output_tokens == 4096
    assert request.params.thinking_budget is None


def test_legacy_v1_bridge_uses_defaults(tmp_path):
    config = Config()  # v1 defaults: no model, no v2 section
    manager = ContextManager(tmp_path, config=config)

    request = manager.assemble(FakeSession())

    assert request.model is None
    assert request.provider is None
    assert request.params.temperature is None
    assert request.params.max_output_tokens is None


# ---------------------------------------------------------------------------
# Per-turn reload
# ---------------------------------------------------------------------------


def test_config_loader_is_called_on_every_assemble(tmp_path):
    calls = {"n": 0}
    first = v2_config(model="scripted/a", temperature=0.1)
    second = v2_config(model="scripted/b", temperature=0.9)

    def loader():
        calls["n"] += 1
        return first if calls["n"] == 1 else second

    manager = ContextManager(tmp_path, config_loader=loader)

    assert manager.assemble(FakeSession()).model == "a"
    assert manager.assemble(FakeSession()).model == "b"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Legacy builder preserved under the new package
# ---------------------------------------------------------------------------


def test_legacy_build_context_still_works():
    context = build_context("rules", "notes", [Exchange("a", "b")], "new", 1000)
    assert context.omitted_exchanges == 0
    assert "rules" in context.prompt


# ---------------------------------------------------------------------------
# Per-turn snapshot (config frozen for the whole turn)
# ---------------------------------------------------------------------------


def test_for_turn_freezes_config_and_system_text(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL-A", encoding="utf-8")
    holder = {"config": v2_config(model="scripted/a", temperature=0.1)}
    manager = ContextManager(tmp_path, config_loader=lambda: holder["config"])

    snapshot = manager.for_turn()

    # Config and system text change underneath the snapshot...
    holder["config"] = v2_config(model="scripted/b", temperature=0.9)
    (tmp_path / "SOUL.md").write_text("SOUL-B", encoding="utf-8")

    frozen = snapshot.assemble(FakeSession())
    assert frozen.model == "a"
    assert frozen.params.temperature == 0.1
    assert "SOUL-A" in frozen.system
    assert "SOUL-B" not in frozen.system

    # ...but a fresh turn sees the reload.
    fresh = manager.for_turn().assemble(FakeSession())
    assert fresh.model == "b"
    assert fresh.params.temperature == 0.9
    assert "SOUL-B" in fresh.system


def test_snapshot_assembles_are_stable_across_calls(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL-A", encoding="utf-8")
    holder = {"config": v2_config(model="scripted/a", temperature=0.1)}
    manager = ContextManager(tmp_path, config_loader=lambda: holder["config"])
    snapshot = manager.for_turn()

    first = snapshot.assemble(FakeSession(Message(role="user", content=[Text("x")])))
    holder["config"] = v2_config(model="scripted/b", temperature=0.9)
    second = snapshot.assemble(FakeSession(Message(role="user", content=[Text("x")])))

    assert first.model == second.model == "a"
    assert first.params == second.params
    assert first.system == second.system


def test_turn_limits_come_from_the_effective_snapshot(tmp_path):
    config = Config(
        model="x",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="x"),
            agent=AgentSection(max_iterations=7, max_turn_seconds=12.5),
        ),
    )
    manager = ContextManager(tmp_path, config=config)

    limits = manager.for_turn().turn_limits()

    assert limits.max_iterations == 7
    assert limits.max_seconds == 12.5


def test_null_byte_filename_is_a_config_error(tmp_path):
    config = Config(instructions_file="bad\x00name", memory_file="MEMORY.md")
    manager = ContextManager(tmp_path, config=config)
    with pytest.raises(ConfigError):
        manager.assemble(FakeSession())


def test_directory_as_context_file_is_a_config_error(tmp_path):
    (tmp_path / "adir").mkdir()
    config = Config(instructions_file="adir", memory_file="MEMORY.md")
    manager = ContextManager(tmp_path, config=config)
    with pytest.raises(ConfigError):
        manager.assemble(FakeSession())

