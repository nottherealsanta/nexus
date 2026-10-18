"""``context.on_overflow`` defaults to ``handoff``; ``compact`` stays a supported opt-out."""
from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection
from nexus.context.manager import ContextManager
from nexus.model.message import Message, Text
from nexus.session import SessionManager


def _load(tmp_path, body: str | None) -> Config:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    if body is not None:
        (project / "nexus.toml").write_text(body, encoding="utf-8")
    return Config.load(project, home=home, environ={})


def test_schema_default_is_handoff_and_compact_is_accepted():
    assert ContextSection().on_overflow == "handoff"
    assert ContextSection(on_overflow="compact").on_overflow == "compact"
    with pytest.raises(ValueError):
        ContextSection(on_overflow="nope")


def test_loaded_v2_config_defaults_to_handoff_and_honors_explicit_compact(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    assert _load(tmp_path / "a", "config_version = 2\n").v2.context.on_overflow == "handoff"
    explicit = _load(tmp_path / "b", 'config_version = 2\n[context]\non_overflow = "compact"\n')
    assert explicit.v2.context.on_overflow == "compact"
    handoff = _load(tmp_path / "c", 'config_version = 2\n[context]\non_overflow = "handoff"\n')
    assert handoff.v2.context.on_overflow == "handoff"


def test_legacy_v1_config_has_no_v2_section_and_so_stays_on_compaction(tmp_path):
    (tmp_path / "legacy").mkdir()
    config = _load(tmp_path / "legacy", None)
    assert config.v2 is None
    overflow = getattr(getattr(getattr(config, "v2", None), "context", None), "on_overflow", "compact")
    assert overflow == "compact"


def test_any_v2_layer_promotes_a_v1_workspace_to_handoff(tmp_path):
    home = tmp_path / "home"
    (home / ".nexus").mkdir(parents=True)
    (home / ".nexus" / "config.toml").write_text("config_version = 2\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "nexus.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    config = Config.load(project, home=home, environ={})
    assert config.v2 is not None
    assert config.v2.context.on_overflow == "handoff"


def test_env_alone_does_not_promote_a_legacy_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "nexus.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    config = Config.load(project, home=home, environ={"NEXUS_CONTEXT__ON_OVERFLOW": "handoff"})
    assert config.v2 is None
    assert config.version == 1


def _long_session(root):
    manager = SessionManager(root)
    session = manager.open("demo")
    for i in range(8):
        session.append_message(Message(role="user" if i % 2 == 0 else "assistant",
            content=[Text(text=f"original-{i} " + "x" * 150)]))
    return manager, session


def _assemble(root, context_section):
    manager, session = _long_session(root)
    config = Config(v2=ConfigV2(context=context_section))
    context = ContextManager(root, config=config, counter=len, identity="")
    with session.begin_turn():
        context.assemble(session)
    return manager.store.session_row("demo")


def test_default_config_hands_off_on_overflow(tmp_path):
    row = _assemble(tmp_path, ContextSection(
        max_tokens=500, safety_margin_tokens=0, compact_at_fraction=1, compaction="drop_oldest"))
    assert row["handoff_count"] == 1


def test_explicit_compact_never_hands_off(tmp_path):
    row = _assemble(tmp_path, ContextSection(
        max_tokens=500, safety_margin_tokens=0, compact_at_fraction=1,
        on_overflow="compact", compaction="drop_oldest"))
    assert row["handoff_count"] == 0 and not row["context_head_seq"]
