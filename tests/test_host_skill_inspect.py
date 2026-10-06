"""Skill inspection is a redacted, read-only view of pinned snapshot bytes."""
from __future__ import annotations

import msgspec

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.runtime import Runtime
from nexus.model.providers.scripted import ScriptedProvider, text_response
from test_agent_selection import _config


def test_protocol_roundtrip():
    command = p.SkillInspect(session="s", name="demo", max_body_bytes=12)
    assert p.decode_command(msgspec.json.encode(command)) == command
    result = p.SkillInspectResult(session="s", name="demo", body="text", truncated=True)
    assert p.decode_result(msgspec.json.encode(result)) == result


async def test_disabled_skill_snapshot_is_read_only(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    folder = workspace / ".agents/skills/demo"
    folder.mkdir(parents=True)
    source = folder / "SKILL.md"
    secret = "sk-" + "a" * 40
    declaration = f"name: demo\ndescription:  Inspect me {secret}\n"
    source.write_text(f"---\n{declaration}---\nBody {secret}\n" + "é" * 100)
    runtime = Runtime(workspace, home=home, config=_config(), providers={"scripted": ScriptedProvider(text_response("unused"))})
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        selected = await facade.handle(p.ContextExtensionSelect(
            session="s", category="skills", name="demo", enabled=False,
        ))
        assert isinstance(selected, p.ContextInspectResult), selected
        before = runtime.sessions.export("s", format="jsonl")
        source.write_text("changed on disk")
        result = await facade.handle(p.SkillInspect(session="s", name="DEMO"))
        assert isinstance(result, p.SkillInspectResult), result
        assert result.status == "ok", result
        assert not result.enabled
        assert result.scope == "project"
        assert "description:  Inspect me" in result.frontmatter_text
        assert "changed on disk" not in result.body
        assert secret not in msgspec.json.encode(result).decode()
        assert result.redacted_for_display
        bounded = await facade.handle(p.SkillInspect(session="s", name="demo", max_body_bytes=9))
        assert bounded.truncated
        assert len(bounded.body.encode()) <= 9
        empty = await facade.handle(p.SkillInspect(session="s", name="demo", max_body_bytes=0))
        assert empty.body == "" and empty.truncated
        for command in (
            p.SkillInspect(session="s", name="missing"),
            p.SkillInspect(session="missing", name="demo"),
            p.SkillInspect(session="s", name="demo", max_body_bytes=-1),
            p.SkillInspect(session="s", name="demo", max_body_bytes=262145),
        ):
            failed = await facade.handle(command)
            assert isinstance(failed, p.SkillInspectResult), failed
            assert failed.status == "error" and failed.error
        assert runtime.sessions.export("s", format="jsonl") == before
        handle = runtime.sessions._handles["s"]
        assert handle.active_turn_id is None
        assert handle.disabled_extensions["skills"] == frozenset({"demo"})
        lease = runtime.manifest_ref.pin()
        try:
            # An old/external manifest lacking exact source is an explicit error.
            original = lease.manifest.skills["demo"]
            object.__setattr__(original, "parsed", None)
            result = await facade.handle(p.SkillInspect(session="s", name="demo"))
            assert result.status == "error" and "snapshot" in result.error
        finally:
            lease.release()
    finally:
        await runtime.aclose()
