"""Manager catalog (plan 02 M3): exactly six tools, fresh approvals, bounded inputs."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nexus.agents.model import AgentParseError, parse_frontmatter
from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection
from nexus.errors import ConfigError
from nexus.runtime import Runtime
from nexus.tools.bundles import (
    BUNDLES,
    MANAGER_BUNDLE,
    MANAGER_PROFILE,
    MANAGER_TOOL_NAMES,
    get_profile,
    profile_names,
)
from nexus.tools.manager_tools import (
    MANAGER_TOOLS,
    MAX_CANCEL_REASON_CHARS,
    MAX_INSTRUCTION_BYTES,
    MAX_WAIT_IDS,
    MAX_WAIT_SECONDS,
    build_manager_catalog,
)
from nexus.tools.permissions import Decision, Grant, Outcome
from nexus.tools.spec import ToolCall

SIX = ("sessions_find", "session_launch", "session_status", "session_wait", "session_result", "session_cancel")


class FakePort:
    def __init__(self):
        self.calls = []

    async def find(self, *, scope, query, limit):
        self.calls.append(("find", scope, query, limit))
        return SimpleNamespace(rows=(), truncated=False)

    async def status(self, ids):
        self.calls.append(("status", ids))
        return ()

    async def result(self, session_id):
        raise AssertionError("not used")

    async def launch(self, instruction, origin_message_id, agent=None, title=None):
        self.calls.append(("launch", instruction, origin_message_id, agent, title))
        return SimpleNamespace(id="s-new", title=title or "", state="queued")

    async def wait(self, ids, until, max_wait_seconds):
        self.calls.append(("wait", ids, until, max_wait_seconds))
        return SimpleNamespace(completed=("a",), running=("b",), interrupted=False, waited_seconds=1.5)

    async def cancel(self, session_id, reason):
        self.calls.append(("cancel", session_id, reason))
        return SimpleNamespace(id=session_id, cancelled=True)


def catalog(port=None):
    return {tool.name: tool for tool in build_manager_catalog(port or FakePort())}


def call(tool, **args):
    return tool.run(args, SimpleNamespace())


def text(result):
    return result.content[0].text


def test_catalog_is_exactly_the_six_tools_in_order():
    tools = build_manager_catalog(FakePort())
    assert tuple(tool.name for tool in tools) == SIX == MANAGER_TOOL_NAMES
    assert {tool.bundle for tool in tools} == {MANAGER_BUNDLE}
    assert BUNDLES[MANAGER_BUNDLE].tools == SIX
    assert tuple(tool.name for tool in MANAGER_TOOLS) == SIX
    for forbidden in ("escalate", "subagent", "read", "bash", "write", "edit", "skill", "webfetch"):
        assert forbidden not in {tool.name for tool in tools}


def test_manager_profile_is_dedicated_and_reserved():
    assert get_profile(MANAGER_PROFILE).bundles == (MANAGER_BUNDLE,)
    assert MANAGER_PROFILE not in profile_names()
    assert "manager" not in get_profile("coding").bundles


@pytest.mark.parametrize(
    "line",
    ["contexts: [manager]", "contexts: [root, manager]", "bundles: [manager]", "profile: manager"],
)
def test_roles_cannot_declare_the_manager(line):
    source = f"---\nname: sneaky\ndescription: tries to be the manager\n{line}\n---\nbody\n"
    with pytest.raises(AgentParseError):
        parse_frontmatter(source)


def _runtime(tmp_path, **kwargs):
    config = Config(model="scripted/sm", version=2, v2=ConfigV2(model=ModelSection(default="scripted/sm")))
    return Runtime(tmp_path, home=tmp_path / "home", config=config, providers={}, **kwargs)


def _turn(runtime, attended=True):
    session = SimpleNamespace(kind="manager", id="m1")
    return session, runtime._make_tool_turn(config=None, session=session, turn_id="t1", attended=attended)


def test_runtime_builds_the_structural_catalog_only(tmp_path):
    def boom(**kwargs):  # an injected factory must never serve a manager session
        raise AssertionError("tool_factory used for a manager")

    port = FakePort()
    runtime = _runtime(tmp_path, manager_port_factory=lambda session: port, tool_factory=boom)
    session, turn = _turn(runtime)
    assert turn.manager.names == SIX
    assert tuple(schema.name for schema in turn.schemas) == SIX
    assert turn.manifest_ref is None and turn.environment_for is None and turn.hooks is None
    assert turn.manager.profile == MANAGER_PROFILE
    assert session._turn_unattended == "deny"


def test_manager_without_a_port_fails_closed(tmp_path):
    runtime = _runtime(tmp_path)
    with pytest.raises(ConfigError, match="manager"):
        _turn(runtime)
    runtime.manager_port_factory = lambda session: None
    with pytest.raises(ConfigError, match="manager"):
        _turn(runtime)


def _evaluate(turn, name, attended, **args):
    prepared = turn.manager.get(name)
    return turn.engine.evaluate(ToolCall(id="c1", name=name, input=args), prepared.spec, attended=attended)


def test_launch_and_cancel_always_ask_and_unattended_denies(tmp_path):
    runtime = _runtime(tmp_path, manager_port_factory=lambda session: FakePort())
    _, turn = _turn(runtime)
    launch = {"instruction": "fix the flaky test", "origin_message_id": "u1"}
    assert _evaluate(turn, "session_launch", True, **launch).outcome is Outcome.ASK
    assert _evaluate(turn, "session_cancel", True, id="s1").outcome is Outcome.ASK
    unattended = _evaluate(turn, "session_launch", False, **launch)
    assert unattended.outcome is Outcome.DENY and unattended.code == "unattended_deny"
    assert _evaluate(turn, "session_cancel", False, id="s1").outcome is Outcome.DENY


def test_no_grant_pre_approves_a_launch(tmp_path):
    runtime = _runtime(tmp_path, manager_port_factory=lambda session: FakePort())
    _, turn = _turn(runtime)
    grants = (Grant("allow", "session_launch"), Grant("allow", "Bundle:manager"))
    prepared = turn.manager.get("session_launch")
    call_ = ToolCall(id="c1", name="session_launch", input={"instruction": "x", "origin_message_id": "u1"})
    evaluation = turn.engine.evaluate(call_, prepared.spec, grants=grants, attended=True)
    assert evaluation.outcome is Outcome.ASK
    assert evaluation.decision is None or evaluation.decision is not Decision.ALLOW_ALWAYS


def test_reads_are_allowed_and_anything_else_is_denied(tmp_path):
    runtime = _runtime(tmp_path, manager_port_factory=lambda session: FakePort())
    _, turn = _turn(runtime)
    for name, args in (
        ("sessions_find", {}), ("session_status", {"ids": ["a"]}),
        ("session_wait", {"ids": ["a"]}), ("session_result", {"id": "a"}),
    ):
        assert _evaluate(turn, name, False, **args).outcome is Outcome.ALLOW
    outsider = turn.engine.evaluate(ToolCall(id="c2", name="bash", input={"command": "ls"}), None, attended=True)
    assert outsider.outcome is Outcome.DENY


def test_each_turn_gets_a_fresh_engine(tmp_path):
    runtime = _runtime(tmp_path, manager_port_factory=lambda session: FakePort())
    _, first = _turn(runtime)
    _, second = _turn(runtime)
    assert first.engine is not second.engine


def test_launch_schema_has_no_model_and_bounds_every_input():
    launch = catalog()["session_launch"].spec.input_schema
    assert set(launch["properties"]) == {"instruction", "origin_message_id", "agent", "title"}
    assert launch["required"] == ["instruction", "origin_message_id"]
    assert launch["additionalProperties"] is False
    assert catalog()["session_wait"].spec.input_schema["properties"]["max_wait_seconds"]["maximum"] == MAX_WAIT_SECONDS == 600
    assert catalog()["session_wait"].spec.input_schema["properties"]["ids"]["maxItems"] == MAX_WAIT_IDS == 20
    assert catalog()["session_cancel"].spec.input_schema["properties"]["reason"]["maxLength"] == MAX_CANCEL_REASON_CHARS


async def test_launch_passes_provenance_and_refuses_a_model_override():
    port = FakePort()
    tools = catalog(port)
    ok = await call(tools["session_launch"], instruction="fix it", origin_message_id="u1", agent="build", title="Fix")
    assert not ok.is_error
    assert json.loads(text(ok)) == {"id": "s-new", "title": "Fix", "state": "queued"}
    assert port.calls == [("launch", "fix it", "u1", "build", "Fix")]

    port.calls.clear()
    bad = await call(tools["session_launch"], instruction="fix it", origin_message_id="u1", model="openai/gpt-5")
    assert bad.is_error and "model" in text(bad)
    assert port.calls == []


async def test_launch_input_bounds():
    port = FakePort()
    launch = catalog(port)["session_launch"]
    too_big = "x" * (MAX_INSTRUCTION_BYTES + 1)
    assert (await call(launch, instruction=too_big, origin_message_id="u1")).is_error
    assert (await call(launch, instruction="é" * (MAX_INSTRUCTION_BYTES // 2 + 1), origin_message_id="u1")).is_error
    assert (await call(launch, instruction="   ", origin_message_id="u1")).is_error
    assert (await call(launch, instruction="go")).is_error  # no origin_message_id
    assert (await call(launch, instruction="go", origin_message_id="")).is_error
    assert not (await call(launch, instruction="x" * MAX_INSTRUCTION_BYTES, origin_message_id="u1")).is_error
    assert len(port.calls) == 1


async def test_wait_and_cancel_bounds():
    port = FakePort()
    tools = catalog(port)
    wait, cancel = tools["session_wait"], tools["session_cancel"]
    assert (await call(wait, ids=[f"s{i}" for i in range(MAX_WAIT_IDS + 1)])).is_error
    assert (await call(wait, ids=[])).is_error
    assert (await call(wait, ids=["a"], max_wait_seconds=MAX_WAIT_SECONDS + 1)).is_error
    assert (await call(wait, ids=["a"], max_wait_seconds=0)).is_error
    assert (await call(wait, ids=["a"], until="some")).is_error
    assert (await call(cancel, id="a", reason="r" * (MAX_CANCEL_REASON_CHARS + 1))).is_error
    assert port.calls == []

    done = await call(wait, ids=["a", "a", "b"], until="all", max_wait_seconds=MAX_WAIT_SECONDS)
    assert json.loads(text(done)) == {"completed": ["a"], "running": ["b"], "interrupted": False, "waited_seconds": 1.5}
    cancelled = await call(cancel, id="a", reason="user asked")
    assert json.loads(text(cancelled)) == {"id": "a", "cancelled": True}
    assert port.calls == [("wait", ("a", "b"), "all", 600), ("cancel", "a", "user asked")]


async def test_port_refusals_come_back_as_tool_errors():
    from nexus.tools.manager_port import ResultNotAuthorized

    class Refusing(FakePort):
        async def launch(self, *args, **kwargs):
            raise ResultNotAuthorized("origin_message_id is not a user message in this turn")

    refused = await call(catalog(Refusing())["session_launch"], instruction="go", origin_message_id="u9")
    assert refused.is_error and "origin_message_id" in text(refused)


def test_permission_key_shows_the_exact_instruction_and_target():
    tools = catalog()
    launch_key = tools["session_launch"].spec.resolve_permission_key(
        {"instruction": "ship it", "origin_message_id": "u1", "agent": "build"}
    )
    assert launch_key == "[build] ship it"
    assert tools["session_cancel"].spec.resolve_permission_key({"id": "s1", "reason": "stuck"}) == "s1: stuck"
    assert tools["session_launch"].spec.mutates and tools["session_cancel"].spec.mutates
    assert not tools["session_wait"].spec.mutates
