"""P4-G: skill activations are session/turn-local tool overlays.

A ``Skill`` call records an immutable :class:`SkillActivation` for the invoking
session and turn. The **next** iteration reads it and narrows the catalog to the
current manifest/profile authority intersected with the skill's declaration.
These tests prove the scope rules from the outside:

* a declaration narrows the next iteration but never widens it;
* a declaration naming an unavailable tool fails closed to the empty set;
* a declaration with no tools does not narrow at all;
* bundle declarations expand to the bundle's tools;
* one session's activation never leaks into another session's environment;
* a finished or cancelled turn drops its activation, so it never leaks forward.
"""
from __future__ import annotations

import asyncio

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(*, profile="coding") -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile=profile),
            permissions=PermissionsSection(mode="allow", on_unattended="allow"),
            tools=ToolsSection(),
        ),
    )


def make_runtime(tmp_path, provider) -> Runtime:
    return Runtime(
        tmp_path, config=make_config(), providers={"scripted": provider}
    )


def write_skill(
    tmp_path,
    name="reader",
    description="read things",
    *,
    allowed=None,
    bundles=None,
):
    lines = [f"name: {name}", f"description: {description}"]
    if allowed is not None:
        lines.append(f"allowed-tools: {allowed}")
    if bundles is not None:
        lines.append(f"bundles: {bundles}")
    skill = tmp_path / ".nexus" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\n" + "\n".join(lines) + "\n---\nBODY\n",
        encoding="utf-8",
    )


async def drain(session):
    return [event async for event in session.send("go")]


async def wait_for(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


async def wait_until_idle(session, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.active or session.queue_depth:
        if loop.time() > deadline:
            raise AssertionError("session did not settle within the timeout")
        await asyncio.sleep(0.001)


def names(request):
    return [tool.name for tool in request.tools]


# ---------------------------------------------------------------------------
# Narrowing
# ---------------------------------------------------------------------------


async def test_declared_tools_narrow_the_next_iteration(tmp_path):
    write_skill(tmp_path, "reader", allowed="[read]")
    provider = ScriptedProvider(
        tool_response(("s1", "skill", {"name": "reader"})),
        text_response("used skill"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("narrow")

    await drain(session)

    assert len(names(provider.requests[0])) == 12  # includes runtime webfetch
    assert names(provider.requests[1]) == ["read"]
    await runtime.aclose()


async def test_legacy_skill_tool_name_remains_accepted(tmp_path):
    write_skill(tmp_path, "reader", allowed="[Read]")
    provider = ScriptedProvider(
        tool_response(("s1", "Skill", {"name": "reader"})),
        text_response("used skill"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("legacy-name")

    await drain(session)

    assert names(provider.requests[1]) == ["read"]
    await runtime.aclose()


async def test_declaration_naming_an_unavailable_tool_fails_closed(tmp_path):
    write_skill(tmp_path, "reader", allowed="[Nope]")
    provider = ScriptedProvider(
        tool_response(("s1", "skill", {"name": "reader"})),
        text_response("used skill"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("closed")

    await drain(session)

    assert names(provider.requests[1]) == []
    await runtime.aclose()


async def test_declaration_of_nothing_does_not_narrow(tmp_path):
    write_skill(tmp_path, "reader")  # no allowed-tools / bundles
    provider = ScriptedProvider(
        tool_response(("s1", "skill", {"name": "reader"})),
        text_response("used skill"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("open")

    await drain(session)

    assert len(names(provider.requests[1])) == 12  # no skill declaration narrows
    await runtime.aclose()


async def test_bundle_declaration_expands_to_the_bundle_tools(tmp_path):
    write_skill(tmp_path, "reader", bundles="[shell]")
    provider = ScriptedProvider(
        tool_response(("s1", "skill", {"name": "reader"})),
        text_response("used skill"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("bundle")

    await drain(session)

    assert names(provider.requests[1]) == ["bash"]
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Scope: no leak across sessions or turns
# ---------------------------------------------------------------------------


async def test_activation_does_not_leak_to_another_session(tmp_path):
    write_skill(tmp_path, "reader", allowed="[Read]")
    release = asyncio.Event()

    def a_skill(request):
        # Iteration 1 of A returns the Skill call.
        return tool_response(("s1", "skill", {"name": "reader"}))

    async def a_park(request):
        # A's second iteration parks so the activation stays live while B runs.
        await release.wait()
        return text_response("a-done")

    def b_respond(request):
        recorded["b_tools"] = names(request)
        return text_response("b-done")

    recorded = {}
    provider = ScriptedProvider([a_skill], [a_park], [b_respond])
    runtime = make_runtime(tmp_path, provider)
    a = runtime.session("session-a")
    b = runtime.session("session-b")

    await a.start_turn("go")
    await wait_for(lambda: len(runtime._activations) > 0)

    await b.start_turn("go")
    await wait_until_idle(b)
    assert len(recorded["b_tools"]) == 12  # includes webfetch; B is unaffected by A

    release.set()
    await wait_until_idle(a)
    await runtime.aclose()


async def test_bundled_skill_tool_candidates_are_not_auto_registered(tmp_path):
    write_skill(tmp_path, "reader")
    bundled = tmp_path / ".nexus" / "skills" / "reader" / "tools"
    bundled.mkdir(parents=True)
    (bundled / "skill_ping.py").write_text(
        "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
        "SPEC = ToolSpec(name='SkillPing', description='ping', "
        "input_schema={'type': 'object', 'properties': {}, "
        "'additionalProperties': False}, bundle='ext')\n"
        "async def run(args, ctx):\n"
        "    return ToolExecutionResult.text('skill-pong')\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("s1", "skill", {"name": "reader"})),
        tool_response(("c1", "SkillPing", {})),
        text_response("used skill"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("bundled")

    await drain(session)

    # The bundled tool is quarantined and loaded during the rebuild, but it is
    # never registered in the global manifest tool map...
    assert "SkillPing" not in runtime.manifest.tools
    assert "SkillPing" in runtime.manifest.skill_tools["reader"].tool_names
    # ...so it is not advertised before the Skill call, and is exposed on the
    # next iteration once the skill is active.
    assert "SkillPing" not in names(provider.requests[0])
    assert "SkillPing" in names(provider.requests[1])
    await runtime.aclose()


async def test_bundle_declaration_cannot_widen_read_only_child_catalog(tmp_path):
    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "explore.md").write_text(
        "---\nname: explore\ndescription: read-only child\n"
        "contexts: [subagent]\n---\nexplore\n",
        encoding="utf-8",
    )
    write_skill(tmp_path, "shell-helper", bundles="[shell]")
    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {
            "prompt": "inspect safely",
            "subagent_type": "explore",
        })),
        tool_response(("child-skill", "skill", {"name": "shell-helper"})),
        text_response("read-only result"),
        text_response("root report"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("bundle-readonly-ceiling")

    await drain(session)

    child_catalog = {
        "read", "glob", "grep", "subagent", "todowrite", "question", "webfetch", "skill"
    }
    assert len(provider.requests) == 4
    assert {tool.name for tool in provider.requests[1].tools} == child_catalog
    assert {tool.name for tool in provider.requests[2].tools} == child_catalog
    assert "bash" not in {tool.name for tool in provider.requests[2].tools}
    await runtime.aclose()


async def test_activation_is_cleared_when_the_turn_finishes(tmp_path):
    write_skill(tmp_path, "reader", allowed="[read]")
    provider = ScriptedProvider(
        tool_response(("s1", "skill", {"name": "reader"})),
        text_response("used skill"),
        text_response("second turn"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("turns")

    await drain(session)
    assert runtime._activations.get(session.id) is None

    await drain(session)
    assert len(names(provider.requests[2])) == 12  # fresh turn restores base catalog
    await runtime.aclose()


async def test_activation_is_cleared_when_the_turn_is_cancelled(tmp_path):
    write_skill(tmp_path, "reader", allowed="[read]")
    release = asyncio.Event()

    def a_skill(request):
        return tool_response(("s1", "skill", {"name": "reader"}))

    async def a_park(request):
        await release.wait()
        return text_response("late")

    provider = ScriptedProvider([a_skill], [a_park])
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("cancel")

    await session.start_turn("go")
    await wait_for(lambda: len(runtime._activations) > 0)

    session.cancel("stop")
    await wait_until_idle(session)

    assert runtime._activations.get(session.id) is None
    await runtime.aclose()
