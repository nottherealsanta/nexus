"""Tests for the Phase 4 ``Skill`` builtin (progressive disclosure + scoping).

Covers the plan section 5.4 invocation contract:

* the body/resource is returned only on invocation, delimited, bounded, and
  read from the manager's refresh-time snapshot (a later edit is invisible);
* provenance and content hashes are present and the whole document respects the
  configured byte budget;
* the session/turn-local ``SkillActivation`` is a subset of the live authority
  (``available & profile``), so a declaration narrows but never widens, and an
  unknown tool/bundle contributes nothing;
* ``skill.invoked``/``skill.completed`` are emitted through the context seam and
  the overlay is recorded into the activation sink;
* invoking a skill never mutates the manifest.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import AgentSection, ConfigV2, ExtSection, ToolsSection
from nexus.ext import ExtensionManager
from nexus.skills.activation import SkillActivation
from nexus.skills.errors import SkillActivationError
from nexus.skills.invocation import SKILL_BEGIN_DELIMITER, SKILL_END_DELIMITER
from nexus.skills.manager import SkillManager
from nexus.tools.builtin import skill as skillmod
from nexus.tools.builtin.skill import (
    SKILL_SPEC,
    SkillActivationLog,
)
from nexus.tools.spec import (
    RegisteredTool,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool(name: str) -> RegisteredTool:
    async def _run(args, ctx):
        return ToolExecutionResult.text("ok")

    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=f"{name} test tool",
            input_schema={"type": "object"},
            bundle="fs",
        ),
        run=_run,
        origin="builtin",
    )


def write_skill(
    root: Path,
    name: str,
    *,
    description: str = "a skill",
    body: str = "BODY",
    frontmatter: str = "",
    scripts: dict[str, str] | None = None,
    references: dict[str, str] | None = None,
) -> Path:
    directory = root / ".nexus" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{frontmatter}---\n{body}\n",
        encoding="utf-8",
    )
    for sub, files in (("scripts", scripts), ("references", references)):
        for filename, content in (files or {}).items():
            target = directory / sub / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    return directory


def make_config(*, profile: str = "coding", max_result_tokens: int = 25_000) -> Config:
    return Config(
        v2=ConfigV2(
            agent=AgentSection(profile=profile),
            tools=ToolsSection(max_result_tokens=max_result_tokens),
            ext=ExtSection(enabled=True, dirs=[]),
        )
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.activations: list[object] = []

    async def emit(self, event_type: str, data: dict | None = None) -> None:
        self.events.append((event_type, dict(data or {})))

    def record(self, activation: object) -> object:
        self.activations.append(activation)
        return activation

    def types(self) -> list[str]:
        return [event_type for event_type, _data in self.events]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "home").mkdir()
    return ws


def make_ctx(
    workspace: Path,
    *,
    skills: object,
    extensions: object | None = None,
    config: Config | None = None,
    recorder: Recorder | None = None,
    session_id: str = "s1",
    turn_id: str = "t1",
) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id=session_id,
        turn_id=turn_id,
        config=config or make_config(),
        skills=skills,
        extensions=extensions,
        activations=recorder,
        emit=getattr(recorder, "emit", None),
    )


def make_extension_manager(workspace: Path, tool_names: list[str]) -> ExtensionManager:
    return ExtensionManager(
        workspace,
        home=workspace / "home",
        builtin_tools=[_tool(name) for name in tool_names],
    )


# ---------------------------------------------------------------------------
# Spec and service absence
# ---------------------------------------------------------------------------


def test_skill_spec_shape():
    assert SKILL_SPEC.name == "Skill"
    assert SKILL_SPEC.bundle == "ext"
    assert SKILL_SPEC.mutates is False
    assert SKILL_SPEC.concurrency == "parallel"
    assert SKILL_SPEC.resolve_permission_key({}) is None
    schema = SKILL_SPEC.input_schema
    assert schema["required"] == ["name"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"name", "resource"}


async def test_missing_skill_service_is_actionable(workspace: Path):
    ctx = ToolContext(
        workspace=workspace,
        session_id="s",
        turn_id="t",
        config=make_config(),
    )
    result = await skillmod.run({"name": "anything"}, ctx)
    assert result.is_error is True
    assert "no skill service" in result.content[0].text


async def test_unknown_skill_is_actionable(workspace: Path):
    manager = SkillManager.for_workspace(workspace, home=workspace / "home")
    ctx = make_ctx(workspace, skills=manager)
    result = await skillmod.run({"name": "nope"}, ctx)
    assert result.is_error is True
    assert "unknown skill" in result.content[0].text


# ---------------------------------------------------------------------------
# Progressive disclosure: body and resource
# ---------------------------------------------------------------------------


async def test_body_is_returned_only_on_invocation_delimited_and_hashed(
    workspace: Path,
):
    write_skill(workspace, "demo", description="does demo things", body="SECRET BODY")
    manager = SkillManager.for_workspace(workspace, home=workspace / "home")
    # The idle index carries only name: description, never the body.
    assert "SECRET BODY" not in manager.render_index()

    ctx = make_ctx(workspace, skills=manager)
    result = await skillmod.run({"name": "demo"}, ctx)
    assert result.is_error is False
    text = result.content[0].text
    assert SKILL_BEGIN_DELIMITER in text
    assert SKILL_END_DELIMITER in text
    assert "SECRET BODY" in text
    assert '<skill name="demo" source="workspace"' in text
    assert result.metrics["body_sha256"]
    assert result.metrics["body_bytes"] == len("SECRET BODY\n")
    assert result.metrics["fingerprint"]


async def test_snapshot_is_generation_stable(workspace: Path):
    directory = write_skill(workspace, "demo", body="ORIGINAL")
    manager = SkillManager.for_workspace(workspace, home=workspace / "home")
    (directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: changed\n---\nEDITED\n", encoding="utf-8"
    )
    ctx = make_ctx(workspace, skills=manager)
    result = await skillmod.run({"name": "demo"}, ctx)
    assert "ORIGINAL" in result.content[0].text
    assert "EDITED" not in result.content[0].text


async def test_resource_invocation_is_delimited_and_hashed(workspace: Path):
    write_skill(
        workspace,
        "demo",
        scripts={"run.py": "print('hi')\n"},
        references={"notes.md": "# Notes\n"},
    )
    manager = SkillManager.for_workspace(workspace, home=workspace / "home")
    ctx = make_ctx(workspace, skills=manager)
    result = await skillmod.run({"name": "demo", "resource": "scripts/run.py"}, ctx)
    assert result.is_error is False
    text = result.content[0].text
    assert skillmod.RESOURCE_BEGIN_DELIMITER in text
    assert "print('hi')" in text
    assert 'resource="scripts/run.py"' in text
    assert result.metrics["resource_sha256"]
    assert result.metrics["resource_bytes"] == len("print('hi')\n")


async def test_resource_escape_is_refused(workspace: Path):
    write_skill(workspace, "demo")
    manager = SkillManager.for_workspace(workspace, home=workspace / "home")
    ctx = make_ctx(workspace, skills=manager)
    for bad in ("../SKILL.md", "/etc/passwd", "scripts/../../x", "other/x.txt"):
        result = await skillmod.run({"name": "demo", "resource": bad}, ctx)
        assert result.is_error is True


async def test_body_is_bounded_by_config_budget(workspace: Path):
    write_skill(workspace, "demo", body="A" * 5000)
    manager = SkillManager.for_workspace(workspace, home=workspace / "home")
    ctx = make_ctx(
        workspace, skills=manager, config=make_config(max_result_tokens=200)
    )
    result = await skillmod.run({"name": "demo"}, ctx)
    assert result.is_error is False
    assert result.metrics["truncated"] is True
    assert "A" * 600 not in result.content[0].text
    assert len(result.content[0].text.encode("utf-8")) <= 200 * 4


# ---------------------------------------------------------------------------
# Activation: scope intersection and no authority expansion
# ---------------------------------------------------------------------------


async def test_activation_narrows_to_declared_tools(workspace: Path):
    write_skill(
        workspace, "demo", frontmatter="allowed-tools: [Read]\n"
    )
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    extensions = make_extension_manager(workspace, ["Read", "Write"])
    ctx = make_ctx(workspace, skills=skills, extensions=extensions)
    result = await skillmod.run({"name": "demo"}, ctx)
    assert result.is_error is False
    activation = result.metrics["activation"]
    assert activation["active"] == ["Read"]
    assert activation["authority"] == ["Read", "Write"]
    assert activation["narrowed"] is True


async def test_activation_never_expands_authority(workspace: Path):
    write_skill(
        workspace, "demo", frontmatter="allowed-tools: [Read, Ghost]\n"
    )
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    extensions = make_extension_manager(workspace, ["Read"])
    ctx = make_ctx(workspace, skills=skills, extensions=extensions)
    result = await skillmod.run({"name": "demo"}, ctx)
    activation = result.metrics["activation"]
    assert activation["active"] == ["Read"]
    assert "Ghost" in activation["unavailable"]
    assert set(activation["active"]) <= set(activation["authority"])


async def test_unknown_bundle_fails_closed_to_empty(workspace: Path):
    write_skill(workspace, "demo", frontmatter="bundles: [nope]\n")
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    extensions = make_extension_manager(workspace, ["Read", "Write"])
    ctx = make_ctx(workspace, skills=skills, extensions=extensions)
    result = await skillmod.run({"name": "demo"}, ctx)
    activation = result.metrics["activation"]
    assert activation["active"] == []
    assert activation["unknown_bundles"] == ["nope"]


async def test_no_declaration_does_not_narrow(workspace: Path):
    write_skill(workspace, "demo")
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    extensions = make_extension_manager(workspace, ["Read", "Write"])
    ctx = make_ctx(workspace, skills=skills, extensions=extensions)
    result = await skillmod.run({"name": "demo"}, ctx)
    activation = result.metrics["activation"]
    assert activation["active"] == activation["authority"]
    assert activation["narrowed"] is False


async def test_absent_extension_service_keeps_authority_empty(workspace: Path):
    write_skill(workspace, "demo", frontmatter="allowed-tools: [Read]\n")
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    ctx = make_ctx(workspace, skills=skills, extensions=None)
    result = await skillmod.run({"name": "demo"}, ctx)
    assert result.is_error is False
    activation = result.metrics["activation"]
    assert activation["active"] == []
    assert activation["authority"] == []


async def test_activation_object_is_an_immutable_subset(workspace: Path):
    write_skill(workspace, "demo", frontmatter="allowed-tools: [Read]\n")
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    extensions = make_extension_manager(workspace, ["Read", "Write"])
    ctx = make_ctx(workspace, skills=skills, extensions=extensions)
    await skillmod.run({"name": "demo"}, ctx)
    # A hand-built activation outside the authority is refused by the contract.
    with pytest.raises(SkillActivationError):
        SkillActivation(
            skill="x",
            active=frozenset({"Ghost"}),
            available=frozenset({"Read"}),
            profile=frozenset({"Read"}),
        )


# ---------------------------------------------------------------------------
# Events, sink, and manifest immutability
# ---------------------------------------------------------------------------


async def test_emits_invoked_and_completed(workspace: Path):
    write_skill(workspace, "demo")
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    recorder = Recorder()
    ctx = make_ctx(workspace, skills=skills, recorder=recorder)
    await skillmod.run({"name": "demo"}, ctx)
    assert recorder.types() == ["skill.invoked", "skill.completed"]
    completed = recorder.events[1][1]
    assert completed["skill"] == "demo"
    assert completed["ok"] is True


async def test_records_activation_into_sink(workspace: Path):
    write_skill(workspace, "demo", frontmatter="allowed-tools: [Read]\n")
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    extensions = make_extension_manager(workspace, ["Read", "Write"])
    recorder = Recorder()
    ctx = make_ctx(
        workspace, skills=skills, extensions=extensions, recorder=recorder
    )
    await skillmod.run({"name": "demo"}, ctx)
    assert len(recorder.activations) == 1
    activation = recorder.activations[0]
    assert isinstance(activation, SkillActivation)
    assert activation.active == frozenset({"Read"})
    assert activation.session == "s1"
    assert activation.turn == 1


async def test_activation_log_is_session_and_turn_scoped(workspace: Path):
    write_skill(workspace, "demo", frontmatter="allowed-tools: [Read]\n")
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    extensions = make_extension_manager(workspace, ["Read", "Write"])
    log = SkillActivationLog()
    await skillmod.run(
        {"name": "demo"},
        make_ctx(
            workspace, skills=skills, extensions=extensions, recorder=log,
            session_id="s1", turn_id="t1",
        ),
    )
    await skillmod.run(
        {"name": "demo"},
        make_ctx(
            workspace, skills=skills, extensions=extensions, recorder=log,
            session_id="s2", turn_id="t9",
        ),
    )
    assert set(log.to_dict()) == {"s1:1", "s2:9"}
    assert len(log.active("s1")) == 1
    assert len(log.active("s2")) == 1
    assert log.get("s1", 1).active == frozenset({"Read"})


async def test_invoking_a_skill_never_mutates_the_manifest(workspace: Path):
    write_skill(workspace, "demo", frontmatter="allowed-tools: [Read]\n")
    skills = SkillManager.for_workspace(workspace, home=workspace / "home")
    extensions = make_extension_manager(workspace, ["Read", "Write"])
    before = extensions.manifest
    generation = extensions.generation
    ctx = make_ctx(workspace, skills=skills, extensions=extensions)
    await skillmod.run({"name": "demo"}, ctx)
    assert extensions.manifest is before
    assert extensions.generation == generation
    assert set(extensions.manifest.tools) == {"Read", "Write"}
