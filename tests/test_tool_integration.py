"""Phase 2 Packet E integration: tools + permissions inside the real loop.

These tests drive the whole stack — ``Runtime`` -> ``Session`` -> ``run_turn`` ->
``ContextManager`` + ``ToolManager``/``PermissionEngine``/``ApprovalBroker`` —
with ``ScriptedProvider`` (no network) and the real builtins. The loop's only
dependencies are the protocol adapters in ``nexus.runtime``; no test reaches 
into ``core.loop`` for a concrete manager.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ProviderSection,
    ToolsSection,
)
from nexus.model.capabilities import Capabilities
from nexus.model.message import ToolResult
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime
from nexus.tools.manager import ToolManager
from nexus.tools.permissions import Decision, exact_rule
from nexus.tools.spec import PathTarget, RegisteredTool, ToolExecutionResult, ToolSpec
from nexus.view import fold

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"

TERMINAL = ("turn.completed", "turn.failed", "turn.cancelled")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    *,
    profile: str = "coding",
    mode: str = "allow",
    unattended: str = "deny",
    max_iterations: int = 60,
    model: str = "scripted/m",
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    ask: list[str] | None = None,
) -> Config:
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=model),
            agent=AgentSection(profile=profile, max_iterations=max_iterations),
            permissions=PermissionsSection(
                mode=mode,
                on_unattended=unattended,
                allow=allow or [],
                deny=deny or [],
                ask=ask or [],
            ),
            tools=ToolsSection(),
        ),
    )


async def drain(iterator):
    return [event async for event in iterator]


def types(events) -> list[str]:
    return [event.type for event in events]


async def send_resolving(session, text, decisions):
    """Consume a turn, resolving each permission request from a decision queue."""
    seen = []
    async for event in session.send(text):
        seen.append(event)
        if event.type == "permission.requested":
            decision = decisions.pop(0)
            assert session.resolve_permission(event.data["id"], decision)
    return seen


def make_runtime(tmp_path: Path, provider, config: Config, **kwargs) -> Runtime:
    return Runtime(tmp_path, config=config, providers={"scripted": provider}, **kwargs)


def custom_manager(
    workspace: Path,
    tools,
    names,
    *,
    mode: str = "allow",
    unattended: str = "allow",
) -> tuple[ToolManager, Config]:
    config = make_config(mode=mode, unattended=unattended)
    manager = ToolManager(
        config, workspace=workspace, tools=tools, tool_names=names
    )
    return manager, config


def registered(name, run, *, bundle="task", mutates=False, timeout_s=None):
    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=f"{name} test tool",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            bundle=bundle,
            mutates=mutates,
            timeout_s=timeout_s,
        ),
        run=run,
        origin="builtin",
    )


def multi_target_probe(run):
    return RegisteredTool(
        spec=ToolSpec(
            name="MultiProbe",
            description="A synthetic multi-target test tool",
            input_schema={
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    }
                },
                "required": ["targets"],
                "additionalProperties": False,
            },
            bundle="task",
            mutates=True,
            permission_key=lambda _data: "scalar-key-must-not-authorize",
            multi_path_targets=lambda data: tuple(
                PathTarget(role, path) for role, path in data["targets"]
            ),
        ),
        run=run,
        origin="builtin",
    )


def write_multi_target_extension(workspace: Path) -> None:
    tools = workspace / ".nexus" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "multi_probe.py").write_text(
        '''
from nexus.tools.spec import PathTarget, ToolExecutionResult, ToolSpec

SPEC = ToolSpec(
    name="MultiProbe",
    description="A synthetic multi-target extension tool",
    input_schema={
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 2,
                },
            }
        },
        "required": ["targets"],
        "additionalProperties": False,
    },
    bundle="task",
    mutates=True,
    permission_key=lambda _data: "scalar-key-must-not-authorize",
    multi_path_targets=lambda data: tuple(
        PathTarget(role, path) for role, path in data["targets"]
    ),
)

async def run(args, ctx):
    return ToolExecutionResult.text("executed:" + str(len(args["targets"])))
''',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Native two-iteration flows
# ---------------------------------------------------------------------------


async def test_native_read_write_bash_flow(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("c1", "read", {"path": "a.txt"})),
        tool_response(("c2", "write", {"path": "b.txt", "content": "written"})),
        tool_response(("c3", "bash", {"command": "echo hi > c.txt"})),
        text_response("all done"),
    )
    runtime = make_runtime(tmp_path, provider, make_config())
    session = runtime.session("flow")

    events = await drain(session.send("do work"))

    assert types(events)[-1] == "turn.completed"
    assert (tmp_path / "b.txt").read_text() == "written"
    assert (tmp_path / "c.txt").read_text().strip() == "hi"
    assert [[type(b).__name__ for b in m.content] for m in session.messages] == [
        ["Text"],
        ["ToolUse"],
        ["ToolResult"],
        ["ToolUse"],
        ["ToolResult"],
        ["ToolUse"],
        ["ToolResult"],
        ["Text"],
    ]
    # One result per call, original order, and Read content is real file data.
    results = [m for m in session.messages if m.role == "user" and isinstance(m.content[0], ToolResult)]
    assert [r.content[0].tool_use_id for r in results] == ["c1", "c2", "c3"]
    assert "hello" in results[0].content[0].content[0].text
    # Schemas reached every request in the turn.
    assert [len(req.tools) for req in provider.requests] == [13, 13, 13, 13]
    names = {t.name for t in provider.requests[0].tools}
    assert {"read", "write", "bash"} <= names
    assert all(name == name.lower() for name in names)
    await runtime.aclose()


async def test_root_tool_transcript_replays_redacted_bounded_presentations(tmp_path):
    provider = ScriptedProvider(
        tool_response(("write-call", "write", {"path": "secret.txt", "content": "sk-live-secret-value"})),
        tool_response(("bash-call", "bash", {"command": "printf 'api_key=sk-live-secret-value'"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, make_config())
    session = runtime.session("root-transcript")
    live = await drain(session.send("go"))
    replay = list(session.events)
    assert [event.type for event in live] == [event.type for event in replay]
    assert str(live) == str(replay)
    assert "sk-live-secret-value" not in str(replay)
    view = fold(replay)
    tools = {tool.call_id: tool for tool in view.tools}
    write_call = tools["write-call"]
    assert write_call.input["path"] == "secret.txt"
    assert write_call.input["content"] in {"***", "[write content omitted: 20 chars]"}
    assert "sk-live-secret-value" not in str(write_call)
    assert write_call.result and write_call.display and write_call.metrics
    bash_call = tools["bash-call"]
    assert bash_call.result
    assert "sk-live-secret-value" not in str(bash_call)
    assert fold(live).to_dict() == fold(replay).to_dict()
    await runtime.aclose()


async def test_root_edit_result_carries_durable_transcript_diff(tmp_path):
    (tmp_path / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("edit-call", "edit", {"path": "f.txt", "old_string": "two", "new_string": "three"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, make_config())
    session = runtime.session("root-edit")
    events = await drain(session.send("go"))
    event = next(
        event for event in events
        if event.type == "tool.result" and event.data["tool_use_id"] == "edit-call"
    )
    assert event.data["diff"] == {
        "path": "f.txt",
        "hunk": "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+three",
        "added_lines": 1,
        "removed_lines": 1,
        "truncated": False,
    }
    tool = next(tool for tool in fold(session.events).tools if tool.call_id == "edit-call")
    assert tool.diff == event.data["diff"]
    assert tool.input["old_string"] in {"two", "[edit text omitted: 3 chars]"}
    assert tool.input["new_string"] in {"three", "[edit text omitted: 5 chars]"}
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "one\nthree\n"
    await runtime.aclose()


async def test_anthropic_request_includes_tool_schemas(tmp_path):
    fixture = (FIXTURES / "text_stream.sse").read_bytes()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=fixture)

    config = Config(
        model="anthropic/claude-test",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="anthropic/claude-test"),
            providers={"anthropic": ProviderSection(api_key="test-key")},
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        http_transport=httpx.MockTransport(handler),
    )
    session = runtime.session("anthropic")
    await drain(session.send("hello"))

    body = json.loads(captured[0].content)
    assert "tools" in body
    names = {tool["name"] for tool in body["tools"]}
    assert {"read", "write", "bash", "todowrite"} <= names
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Permission flow: whole batch planned before anything runs
# ---------------------------------------------------------------------------


async def test_full_batch_permission_completion_before_first_tool_started(tmp_path):
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(
            ("c1", "Read", {"path": "a.txt"}),
            ("c2", "Write", {"path": "b.txt", "content": "B"}),
        ),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask"))
    session = runtime.session("batch").mark_attended()

    events = await send_resolving(
        session, "go", [Decision.ALLOW_ONCE, Decision.ALLOW_ONCE]
    )
    kinds = types(events)

    assert kinds.count("permission.requested") == 2
    assert kinds.count("permission.resolved") == 2
    last_resolved = max(i for i, t in enumerate(kinds) if t == "permission.resolved")
    first_started = kinds.index("tool.started")
    assert last_resolved < first_started
    # Both calls resolved before either started; one result message, call order.
    result_messages = [
        m
        for m in session.messages
        if m.role == "user" and isinstance(m.content[0], ToolResult)
    ]
    assert len(result_messages) == 1
    assert [b.tool_use_id for b in result_messages[0].content] == ["c1", "c2"]
    await runtime.aclose()


async def test_public_apply_patch_multitarget_permissions_end_to_end(tmp_path):
    workspace = tmp_path / "apply-patch"
    workspace.mkdir()
    (workspace / "edited.txt").write_text("old\n", encoding="utf-8")

    def patch_update_and_add(
        old: str,
        new: str,
        *,
        added: str | None = None,
        secondary: tuple[str, str] = ("created", "updated"),
    ) -> str:
        operations = (
            "*** Update File: edited.txt\n"
            "@@ -1 +1 @@\n"
            f"-{old}\n"
            f"+{new}\n"
        )
        if added is not None:
            operations += f"*** Add File: {added}\n+created\n"
        else:
            operations += (
                "*** Update File: added.txt\n"
                "@@ -1 +1 @@\n"
                f"-{secondary[0]}\n"
                f"+{secondary[1]}\n"
            )
        return f"*** Begin Patch\n{operations}*** End Patch\n"

    first_patch = patch_update_and_add("old", "once", added="added.txt")
    always_patch = patch_update_and_add("once", "always")
    replay_patch = patch_update_and_add(
        "always", "replayed", secondary=("updated", "replayed")
    )
    new_path_patch = patch_update_and_add(
        "replayed",
        "with-new-path",
        added="new.txt",
        secondary=("replayed", "updated"),
    )
    config = make_config(mode="ask", unattended="deny")
    provider = ScriptedProvider(
        tool_response(("patch-once", "apply_patch", {"patch": first_patch})),
        text_response("patched once"),
        tool_response(("patch-always", "apply_patch", {"patch": always_patch})),
        text_response("granted paths"),
    )
    runtime = make_runtime(workspace, provider, config)
    session = runtime.session("public-apply-patch").mark_attended()

    once_events = await send_resolving(
        session, "apply the patch", [Decision.ALLOW_ONCE]
    )
    request = next(event for event in once_events if event.type == "permission.requested")
    expected_paths = [
        str((workspace / "edited.txt").resolve()),
        str((workspace / "added.txt").resolve()),
    ]
    assert [target["path"] for target in request.data["targets"]] == expected_paths
    assert types(once_events).index("permission.resolved") < types(once_events).index(
        "tool.started"
    )
    assert {tool.name for tool in provider.requests[0].tools} >= {"apply_patch"}
    assert (workspace / "edited.txt").read_text(encoding="utf-8") == "once\n"
    assert (workspace / "added.txt").read_text(encoding="utf-8") == "created\n"

    always_events = await send_resolving(
        session, "apply the next patch", [Decision.ALLOW_ALWAYS]
    )
    always_request = next(
        event for event in always_events if event.type == "permission.requested"
    )
    assert [target["path"] for target in always_request.data["targets"]] == expected_paths
    resolved = next(
        event for event in always_events if event.type == "permission.resolved"
    )
    assert [item["path"] for item in resolved.data["target_grants"]] == expected_paths
    assert [item["grant"]["rule"] for item in resolved.data["target_grants"]] == [
        exact_rule("apply_patch", path) for path in expected_paths
    ]
    assert (workspace / "edited.txt").read_text(encoding="utf-8") == "always\n"
    assert (workspace / "added.txt").read_text(encoding="utf-8") == "updated\n"
    await runtime.aclose()

    # Reopen the session so the next call must reconstruct its exact path grants
    # from the durable permission.resolved event.
    reopened_provider = ScriptedProvider(
        tool_response(("patch-replay", "apply_patch", {"patch": replay_patch})),
        text_response("replayed"),
        tool_response(("patch-new-path", "apply_patch", {"patch": new_path_patch})),
        text_response("approved new path"),
    )
    reopened = make_runtime(workspace, reopened_provider, config)
    reopened_session = reopened.session("public-apply-patch").mark_attended()
    replay_events = await drain(reopened_session.send("repeat on granted paths"))
    assert "permission.requested" not in types(replay_events)
    assert (workspace / "edited.txt").read_text(encoding="utf-8") == "replayed\n"
    assert (workspace / "added.txt").read_text(encoding="utf-8") == "replayed\n"

    new_path_events = await send_resolving(
        reopened_session, "also create a new path", [Decision.ALLOW_ONCE]
    )
    new_request = next(
        event for event in new_path_events if event.type == "permission.requested"
    )
    assert [target["path"] for target in new_request.data["targets"]] == [
        str((workspace / "new.txt").resolve())
    ]
    assert (workspace / "edited.txt").read_text(encoding="utf-8") == "with-new-path\n"
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "created\n"
    await reopened.aclose()

    # A deny on either path independently blocks the whole patch before any
    # mutation; exercise these through fresh public runtimes and the built-in.
    for index, denied_name in enumerate(("edited.txt", "added.txt")):
        denied_workspace = tmp_path / f"deny-{index}"
        denied_workspace.mkdir()
        (denied_workspace / "edited.txt").write_text("old\n", encoding="utf-8")
        denied_provider = ScriptedProvider(
            tool_response(
                (
                    f"denied-patch-{index}",
                    "apply_patch",
                    {"patch": first_patch},
                )
            ),
            text_response("denied"),
        )
        denied_config = make_config(
            mode="allow",
            deny=[
                exact_rule(
                    "apply_patch", str((denied_workspace / denied_name).resolve())
                )
            ],
        )
        denied_runtime = make_runtime(
            denied_workspace, denied_provider, denied_config
        )
        denied_session = denied_runtime.session(f"denied-{index}")
        denied_events = await drain(denied_session.send("apply a denied patch"))
        assert "permission.requested" not in types(denied_events)
        assert "tool.started" not in types(denied_events)
        assert (denied_workspace / "edited.txt").read_text(encoding="utf-8") == "old\n"
        assert not (denied_workspace / "added.txt").exists()
        await denied_runtime.aclose()


async def test_multi_target_approval_handoff_runs_only_after_real_approval(tmp_path):
    ran = []

    async def run(args, _ctx):
        ran.append(tuple(tuple(item) for item in args["targets"]))
        return ToolExecutionResult.text("ran")

    manager, config = custom_manager(
        tmp_path,
        [multi_target_probe(run)],
        ["MultiProbe"],
        mode="ask",
        unattended="deny",
    )
    runtime = make_runtime(
        tmp_path,
        ScriptedProvider(
            tool_response(
                (
                    "multi-call",
                    "MultiProbe",
                    {"targets": [["source", "one.txt"], ["destination", "two.txt"]]},
                )
            ),
            text_response("done"),
            tool_response(
                (
                    "multi-call-again",
                    "MultiProbe",
                    {"targets": [["source", "one.txt"], ["destination", "two.txt"]]},
                )
            ),
            text_response("done again"),
        ),
        config,
        tools=manager,
    )
    session = runtime.session("multi-approval").mark_attended()
    seen = []
    async for event in session.send("move files"):
        seen.append(event.type)
        if event.type == "permission.requested":
            assert len(event.data["targets"]) == 2
            assert session.resolve_permission(event.data["id"], Decision.ALLOW_ALWAYS)

    assert ran == [(('source', 'one.txt'), ('destination', 'two.txt'))]
    assert seen.index("permission.resolved") < seen.index("tool.started")
    resolved = next(event for event in session.events if event.type == "permission.resolved")
    assert len(resolved.data["target_grants"]) == 2
    replay_events = await drain(session.send("move same files again"))
    assert ran == [
        (("source", "one.txt"), ("destination", "two.txt")),
        (("source", "one.txt"), ("destination", "two.txt")),
    ]
    assert "permission.requested" not in types(replay_events)
    await runtime.aclose()


async def test_multi_target_allow_once_uses_broker_evidence(tmp_path):
    ran = []

    async def run(args, _ctx):
        ran.append(True)
        return ToolExecutionResult.text("ran")

    manager, config = custom_manager(
        tmp_path,
        [multi_target_probe(run)],
        ["MultiProbe"],
        mode="ask",
        unattended="deny",
    )
    runtime = make_runtime(
        tmp_path,
        ScriptedProvider(
            tool_response(
                (
                    "multi-call",
                    "MultiProbe",
                    {"targets": [["source", "one.txt"], ["destination", "two.txt"]]},
                )
            ),
            text_response("done"),
        ),
        config,
        tools=manager,
    )
    session = runtime.session("multi-once").mark_attended()
    events = []
    async for event in session.send("move files"):
        events.append(event.type)
        if event.type == "permission.requested":
            assert session.resolve_permission(event.data["id"], Decision.ALLOW_ONCE)

    assert ran == [True]
    assert events.index("permission.resolved") < events.index("tool.started")
    await runtime.aclose()


async def test_multi_target_denial_never_starts_tool(tmp_path):
    denied_path = str((tmp_path / "two.txt").resolve())
    ran = []

    async def run(args, _ctx):
        ran.append(True)
        return ToolExecutionResult.text("ran")

    config = make_config(mode="ask", deny=[f"MultiProbe({denied_path})"])
    manager = ToolManager(
        config,
        workspace=tmp_path,
        tools=[multi_target_probe(run)],
        tool_names=["MultiProbe"],
    )
    runtime = make_runtime(
        tmp_path,
        ScriptedProvider(
            tool_response(
                (
                    "multi-call",
                    "MultiProbe",
                    {"targets": [["source", "one.txt"], ["destination", "two.txt"]]},
                )
            ),
            text_response("done"),
        ),
        config,
        tools=manager,
    )
    session = runtime.session("multi-denied").mark_attended()
    events = await drain(session.send("move files"))

    assert ran == []
    assert "tool.started" not in types(events)
    assert "permission.requested" not in types(events)
    assert not (tmp_path / "two.txt").exists()
    await runtime.aclose()


async def test_multi_target_path_swap_after_approval_is_refused(tmp_path):
    link = tmp_path / "link"
    link.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    ran = []

    async def run(args, _ctx):
        ran.append(True)
        return ToolExecutionResult.text("ran")

    config = make_config(mode="ask")
    manager = ToolManager(
        config,
        workspace=tmp_path,
        tools=[multi_target_probe(run)],
        tool_names=["MultiProbe"],
    )
    runtime = make_runtime(
        tmp_path,
        ScriptedProvider(
            tool_response(
                (
                    "multi-call",
                    "MultiProbe",
                    {"targets": [["source", "link/from.txt"], ["destination", "to.txt"]]},
                )
            ),
            text_response("done"),
        ),
        config,
        tools=manager,
    )
    session = runtime.session("multi-swapped").mark_attended()
    events = []
    async for event in session.send("move files"):
        events.append(event)
        if event.type == "permission.requested":
            link.rmdir()
            link.symlink_to(outside, target_is_directory=True)
            assert session.resolve_permission(event.data["id"], Decision.ALLOW_ONCE)

    assert ran == []
    assert not any(event.type == "tool.started" for event in events)
    assert outside.is_dir()
    await runtime.aclose()


async def test_default_extension_runtime_multi_target_gate_uses_iteration_manager(
    tmp_path, monkeypatch
):
    import nexus.runtime as runtime_module

    manager_bindings = []
    original = runtime_module._ManifestEnvironmentFactory.for_iteration

    def capture_environment(factory, *args, **kwargs):
        environment = original(factory, *args, **kwargs)
        manager_bindings.append((
            environment.gate._manager,
            environment.manager,
            environment.dispatcher.manager,
        ))
        return environment

    monkeypatch.setattr(
        runtime_module._ManifestEnvironmentFactory,
        "for_iteration",
        capture_environment,
    )

    async def run_case(name, *, mode, allow=None, deny=None, approve=False):
        workspace = tmp_path / name
        workspace.mkdir()
        write_multi_target_extension(workspace)
        config = make_config(
            mode=mode,
            unattended="deny",
            allow=allow,
            deny=deny,
        )
        provider = ScriptedProvider(
            tool_response(
                (
                    f"{name}-call",
                    "MultiProbe",
                    {"targets": [["source", "source.txt"], ["destination", "dest.txt"]]},
                )
            ),
            text_response("done"),
        )
        runtime = make_runtime(workspace, provider, config)
        session = runtime.session(name)
        if approve:
            session.mark_attended()
        events = []
        async for event in session.send("run the extension tool"):
            events.append(event)
            if event.type == "permission.requested":
                assert approve
                assert session.resolve_permission(
                    event.data["id"], Decision.ALLOW_ONCE
                )
        await runtime.aclose()
        return events

    allowed = await run_case(
        "extension-allowed",
        mode="deny",
        allow=["MultiProbe"],
    )
    denied = await run_case(
        "extension-denied",
        mode="deny",
    )
    approved = await run_case("extension-approved", mode="ask", approve=True)

    assert "tool.started" in types(allowed)
    allowed_result = next(
        event
        for event in allowed
        if event.type == "tool.result"
        and event.data["tool_use_id"] == "extension-allowed-call"
    )
    assert "executed:2" in str(allowed_result.data)
    assert "tool.started" not in types(denied)
    assert not any(event.type == "permission.requested" for event in denied)
    assert "tool.started" in types(approved)
    assert types(approved).index("permission.resolved") < types(approved).index(
        "tool.started"
    )
    assert manager_bindings
    assert all(gate is manager is dispatcher for gate, manager, dispatcher in manager_bindings), [
        tuple(id(item) for item in binding) for binding in manager_bindings
    ]


async def test_all_decisions_and_absolute_deny(tmp_path):
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")

    # DENY_ONCE -> ordered error result, turn continues.
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("recovered"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask"))
    session = runtime.session("deny-once").mark_attended()
    await send_resolving(session, "go", [Decision.DENY_ONCE])
    result = session.messages[2].content[0]
    assert result.is_error is True
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()

    # Absolute config deny cannot be overridden even by a session ALLOW_ALWAYS.
    provider2 = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    config = make_config(mode="allow", deny=["Write(**)"])
    runtime2 = make_runtime(tmp_path, provider2, config)
    session2 = runtime2.session("abs").mark_attended()
    await drain(session2.send("go"))
    assert not (tmp_path / "b.txt").exists()
    denied = session2.messages[2].content[0]
    assert denied.is_error is True
    assert "Denied by rule" in denied.content[0].text
    await runtime2.aclose()


async def test_allow_always_replays_within_session_and_after_reopen(tmp_path):
    scripts = [
        tool_response(("c1", "Write", {"path": "out.txt", "content": "one"})),
        text_response("done-1"),
        tool_response(("c2", "Write", {"path": "out.txt", "content": "two"})),
        text_response("done-2"),
    ]
    provider = ScriptedProvider(*scripts)
    config = make_config(mode="ask")
    runtime = make_runtime(tmp_path, provider, config)
    session = runtime.session("always").mark_attended()

    first = await send_resolving(session, "write", [Decision.ALLOW_ALWAYS])
    second = await drain(session.send("write again"))

    assert types(first).count("permission.requested") == 1
    # The grant is session-scoped and reconstructs before the next turn.
    assert "permission.requested" not in types(second)
    assert (tmp_path / "out.txt").read_text() == "two"
    await runtime.aclose()

    # A fresh runtime over the same session dir rebuilds grants from events.
    provider2 = ScriptedProvider(
        tool_response(("c3", "Write", {"path": "out.txt", "content": "three"})),
        text_response("done-3"),
    )
    runtime2 = make_runtime(tmp_path, provider2, config)
    session2 = runtime2.session("always").mark_attended()
    third = await drain(session2.send("write once more"))
    assert "permission.requested" not in types(third)
    assert (tmp_path / "out.txt").read_text() == "three"
    await runtime2.aclose()


async def test_once_decision_is_not_replayed(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "one"})),
        text_response("done-1"),
        tool_response(("c2", "Write", {"path": "out.txt", "content": "two"})),
        text_response("done-2"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask"))
    session = runtime.session("once").mark_attended()

    await send_resolving(session, "write", [Decision.ALLOW_ONCE])
    second = await send_resolving(session, "write again", [Decision.ALLOW_ONCE])
    assert types(second).count("permission.requested") == 1
    await runtime.aclose()


async def test_deny_always_replays(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "one"})),
        text_response("done-1"),
        tool_response(("c2", "Write", {"path": "out.txt", "content": "two"})),
        text_response("done-2"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask"))
    session = runtime.session("deny-always").mark_attended()

    await send_resolving(session, "write", [Decision.DENY_ALWAYS])
    assert not (tmp_path / "out.txt").exists()
    second = await drain(session.send("write again"))
    assert "permission.requested" not in types(second)
    assert not (tmp_path / "out.txt").exists()
    result = session.messages[-2].content[0]
    assert result.is_error is True
    await runtime.aclose()


async def test_resolve_permission_rejects_unknown_and_stale_ids(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask"))
    session = runtime.session("stale").mark_attended()

    # No active turn yet: safely False.
    assert session.resolve_permission("nope", Decision.ALLOW_ONCE) is False

    seen = []
    async for event in session.send("go"):
        seen.append(event)
        if event.type == "permission.requested":
            assert session.resolve_permission(event.data["id"], Decision.ALLOW_ONCE)
            # Re-resolving the same (now stale) id is safely rejected.
            assert session.resolve_permission(event.data["id"], Decision.ALLOW_ONCE) is False
    assert types(seen)[-1] == "turn.completed"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Headless / unattended policy
# ---------------------------------------------------------------------------


async def test_unattended_deny_produces_error_result(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask", unattended="deny"))
    session = runtime.session("u-deny")  # not marked attended
    events = await drain(session.send("go"))

    assert not (tmp_path / "out.txt").exists()
    result = session.messages[2].content[0]
    assert result.is_error is True
    assert "unattended policy denies" in result.content[0].text
    resolved = [e for e in events if e.type == "permission.resolved"]
    assert resolved and resolved[0].data["code"] == "unattended_deny"
    assert types(events)[-1] == "turn.completed"
    await runtime.aclose()


async def test_unattended_allow_executes_and_is_audited(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask", unattended="allow"))
    session = runtime.session("u-allow")
    events = await drain(session.send("go"))

    assert (tmp_path / "out.txt").read_text() == "x"
    assert "permission.requested" not in types(events)
    resolved = [e for e in events if e.type == "permission.resolved"]
    assert resolved and resolved[0].data["code"] == "unattended_allow"
    await runtime.aclose()


async def test_unattended_fail_turn_ends_before_tool_start(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("unreached"),
    )
    runtime = make_runtime(
        tmp_path, provider, make_config(mode="ask", unattended="fail_turn")
    )
    session = runtime.session("u-fail")
    events = await drain(session.send("go"))

    assert types(events)[-1] == "turn.failed"
    assert "tool.started" not in types(events)
    assert not (tmp_path / "out.txt").exists()
    assert "permission.requested" not in types(events)
    assert session.messages[1].content[0].input  # assistant tool_use persisted
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Capability degradation
# ---------------------------------------------------------------------------


async def test_provider_without_tools_omits_schemas_and_never_dispatches(tmp_path):
    caps = Capabilities(tools=False, streaming=True)
    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "a.txt"})),
        text_response("ok"),
        capabilities=caps,
    )
    runtime = make_runtime(tmp_path, provider, make_config())
    session = runtime.session("no-tools")
    events = await drain(session.send("read"))

    assert [len(req.tools) for req in provider.requests] == [0, 0]
    assert "tool.started" not in types(events)
    result = session.messages[2].content[0]
    assert result.is_error is True
    assert "does not support tool calls" in result.content[0].text
    await runtime.aclose()


async def test_serial_provider_serializes_parallel_calls(tmp_path):
    probe = {"active": 0, "max": 0}

    async def sleeper(args, ctx):
        probe["active"] += 1
        probe["max"] = max(probe["max"], probe["active"])
        try:
            await asyncio.sleep(0.02)
            return ToolExecutionResult.text("ok")
        finally:
            probe["active"] -= 1

    tools = [registered("A", sleeper), registered("B", sleeper)]
    manager, config = custom_manager(tmp_path, tools, ["A", "B"])
    caps = Capabilities(tools=True, parallel_tool_calls=False, streaming=True)
    provider = ScriptedProvider(
        tool_response(("c1", "A", {}), ("c2", "B", {})),
        text_response("done"),
        capabilities=caps,
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider}, tools=manager)
    session = runtime.session("serial")
    await drain(session.send("go"))
    assert probe["max"] == 1
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Errors, ordering, timeouts
# ---------------------------------------------------------------------------


async def test_unknown_and_schema_invalid_are_ordered_error_results(tmp_path):
    provider = ScriptedProvider(
        tool_response(
            ("c1", "Nope", {}),
            ("c2", "Read", {}),  # Read requires a path
            ("c3", "Read", {"path": "missing.txt"}),
        ),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, make_config())
    session = runtime.session("errors")
    events = await drain(session.send("go"))

    results = session.messages[2].content
    assert [r.tool_use_id for r in results] == ["c1", "c2", "c3"]
    assert all(r.is_error for r in results)
    joined = " ".join(r.content[0].text for r in results)
    assert "Unknown tool" in joined
    assert "Invalid input" in joined
    assert types(events)[-1] == "turn.completed"
    await runtime.aclose()


async def test_parallel_results_preserve_order(tmp_path):
    async def slow(args, ctx):
        await asyncio.sleep(0.03)
        return ToolExecutionResult.text("slow")

    async def fast(args, ctx):
        await asyncio.sleep(0)
        return ToolExecutionResult.text("fast")

    tools = [registered("Slow", slow), registered("Fast", fast)]
    manager, config = custom_manager(tmp_path, tools, ["Slow", "Fast"])
    provider = ScriptedProvider(
        tool_response(("c1", "Slow", {}), ("c2", "Fast", {})),
        text_response("done"),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider}, tools=manager)
    session = runtime.session("ordered")
    events = await drain(session.send("go"))

    started = [e.data["tool"] for e in events if e.type == "tool.started"]
    completed = [e.data["tool"] for e in events if e.type == "tool.completed"]
    assert started == ["Slow", "Fast"]
    # The fast tool finishes first, but the persisted result order is call order.
    assert [r.tool_use_id for r in session.messages[2].content] == ["c1", "c2"]
    assert completed == ["Fast", "Slow"]
    await runtime.aclose()


async def test_tool_timeout_and_exception_continue_the_turn(tmp_path):
    async def slow(args, ctx):
        await asyncio.sleep(1.0)
        return ToolExecutionResult.text("late")

    async def boom(args, ctx):
        raise RuntimeError("kaboom")

    tools = [
        registered("Slow", slow, timeout_s=0.05),
        registered("Boom", boom),
    ]
    manager, config = custom_manager(tmp_path, tools, ["Slow", "Boom"])
    provider = ScriptedProvider(
        tool_response(("c1", "Slow", {}), ("c2", "Boom", {})),
        text_response("recovered"),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider}, tools=manager)
    session = runtime.session("timeout")
    events = await drain(session.send("go"))

    results = session.messages[2].content
    assert [r.tool_use_id for r in results] == ["c1", "c2"]
    assert "timed out" in results[0].content[0].text
    assert "kaboom" in results[1].content[0].text
    assert types(events)[-1] == "turn.completed"
    assert session.messages[-1].content[0].text == "recovered"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancellation_during_approval_cancels_pending_and_releases(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask"))
    session = runtime.session("cancel-approval").mark_attended()
    seen = []

    async def consume():
        async for event in session.send("go"):
            seen.append(event)

    task = asyncio.create_task(consume())
    for _ in range(500):
        await asyncio.sleep(0)
        if any(event.type == "permission.requested" for event in seen):
            break
    assert any(event.type == "permission.requested" for event in seen)

    session.cancel("user said no")
    await asyncio.wait_for(task, timeout=3)

    assert types(seen)[-1] == "turn.cancelled"
    assert types(seen).count("turn.cancelled") == 1
    assert not (tmp_path / "out.txt").exists()
    assert session.active is False
    # The lock is genuinely free for a fresh turn.
    fresh = await drain(session.send("again"))
    assert types(fresh)[-1] == "turn.completed"
    await runtime.aclose()


async def test_cancellation_during_bash_dispatch_cleans_up_process(tmp_path):
    marker = tmp_path / "pid.txt"
    provider = ScriptedProvider(
        tool_response(
            (
                "c1",
                "Bash",
                {"command": f"echo $$ > {marker}; sleep 30"},
            )
        ),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="allow"))
    session = runtime.session("cancel-bash")
    seen = []

    async def consume():
        async for event in session.send("go"):
            seen.append(event)

    task = asyncio.create_task(consume())
    for _ in range(1000):
        await asyncio.sleep(0.005)
        if any(event.type == "tool.started" for event in seen) and marker.exists():
            break
    assert any(event.type == "tool.started" for event in seen)

    pid = int(marker.read_text().strip())
    session.cancel("stop")
    await asyncio.wait_for(task, timeout=10)
    await asyncio.sleep(0.2)

    assert types(seen)[-1] == "turn.cancelled"
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(pid, 0)
    assert session.active is False
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Exact persisted order and crash recovery
# ---------------------------------------------------------------------------


async def test_exact_persisted_event_and_message_order(tmp_path):
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "a.txt"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, make_config())
    session = runtime.session("order")
    events = await drain(session.send("go"))

    assert types(events) == [
        "turn.started",
        "input.started",
        "context.assembled",
        "model.started",
        "model.stopped",
        "tool.requested",
        "tool.input",
        "tool.started",
        "tool.completed",
        "tool.result",
        "context.assembled",
        "model.started",
        "text.delta",
        "text",
        "model.stopped",
        "turn.completed",
    ]
    # Persisted events match yielded events exactly, in order.
    assert [e.type for e in session.events] == types(events)
    # Messages are appended in the tool-safe order: assistant(tool_use) precedes
    # the user(tool_result) message.
    assert [[type(b).__name__ for b in m.content] for m in session.messages] == [
        ["Text"],
        ["ToolUse"],
        ["ToolResult"],
        ["Text"],
    ]
    await runtime.aclose()


async def test_crash_recovery_does_not_reexecute(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(mode="ask"))
    session = runtime.session("crash").mark_attended()
    seen = []

    async def consume():
        async for event in session.send("go"):
            seen.append(event)

    task = asyncio.create_task(consume())
    for _ in range(500):
        await asyncio.sleep(0)
        if any(event.type == "permission.requested" for event in seen):
            break
    assert any(event.type == "permission.requested" for event in seen)

    # Close the consumer mid-approval: the assistant tool_use is durable, no
    # result exists yet, and nothing ran. This is the crash shape.
    task.cancel()
    import contextlib

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert session.active is False

    # Reopen the same log without auto-recovery and recover explicitly.
    reopened = runtime.session("crash", recover=False)
    recovered = reopened.recover_dangling_tool_uses()
    assert recovered
    result = next(
        m for m in reopened.messages if isinstance(m.content[0], ToolResult)
    ).content[0]
    assert result.tool_use_id == "c1"
    assert result.is_error is True
    assert "interrupted" in result.content[0].text.lower()
    assert not (tmp_path / "out.txt").exists()

    # Recovery is idempotent: a second call executes nothing and appends nothing.
    before = len(reopened.messages)
    assert reopened.recover_dangling_tool_uses() == []
    assert len(reopened.messages) == before
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Frozen per-turn environment
# ---------------------------------------------------------------------------


async def test_config_profile_and_policy_freeze_within_turn(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL-A", encoding="utf-8")
    holder = {"config": make_config(profile="coding", mode="allow")}

    def mutate(req):
        holder["config"] = make_config(profile="research", mode="deny")
        (tmp_path / "SOUL.md").write_text("SOUL-B", encoding="utf-8")
        return tool_response(("c1", "Write", {"path": "out.txt", "content": "z"}))

    provider = ScriptedProvider([mutate], text_response("done"), text_response("again"))
    runtime = Runtime(
        tmp_path,
        config_loader=lambda: holder["config"],
        providers={"scripted": provider},
    )
    session = runtime.session("frozen")
    events = await drain(session.send("go"))

    # Within the turn the catalog and policy are frozen: the second iteration
    # still sees the coding tool set and allow policy, and SOUL-A.
    assert (tmp_path / "out.txt").exists()
    assert [len(req.tools) for req in provider.requests] == [13, 13]
    assert all("SOUL-A" in (req.system or "") for req in provider.requests)
    assert types(events)[-1] == "turn.completed"

    # Between turns the edit takes effect: research has fewer tools.
    await drain(session.send("again"))
    assert len(provider.requests[-1].tools) == 9
    await runtime.aclose()


async def test_research_profile_is_read_only_and_unknown_profile_fails_closed(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider, make_config(profile="research"))
    session = runtime.session("research")
    await drain(session.send("write"))
    result = session.messages[2].content[0]
    assert result.is_error is True
    assert "Unknown tool" in result.content[0].text
    assert not (tmp_path / "out.txt").exists()
    await runtime.aclose()

    # Unknown profile fails before any log mutation.
    bad = ScriptedProvider(text_response("nope"))
    runtime2 = make_runtime(tmp_path, bad, make_config(profile="does-not-exist"))
    session2 = runtime2.session("bad-profile")
    from nexus.errors import ConfigError

    with pytest.raises(ConfigError):
        await session2.send("go").__anext__()
    assert session2.messages == []
    assert session2.events == []
    assert session2.active is False
    await runtime2.aclose()


# ---------------------------------------------------------------------------
# Todo durable seam
# ---------------------------------------------------------------------------


async def test_todo_result_persisted_and_state_shared_across_turns(tmp_path):
    provider = ScriptedProvider(
        tool_response(
            (
                "c1",
                "TodoWrite",
                {
                    "todos": [
                        {"id": "t1", "content": "ship it", "status": "in_progress"}
                    ]
                },
            )
        ),
        text_response("one"),
        text_response("two"),
    )
    runtime = make_runtime(tmp_path, provider, make_config())
    session = runtime.session("todo")
    await drain(session.send("plan"))

    result = session.messages[2].content[0]
    assert result.is_error is False
    assert "ship it" in result.content[0].text
    # The manager-owned store is shared across turns of this runtime.
    assert runtime.todo_store.get("todo")[0].content == "ship it"

    # The full list is durable, alongside the existing progress payload.
    assert any(m for m in session.messages if isinstance(m.content[0], ToolResult))
    updated = [e for e in session.events if e.type == "todo.updated"]
    assert updated and updated[0].data["todos"][0]["content"] == "ship it"
    assert updated[0].data["revision"] == 1
    assert updated[0].data["agent_id"] == "root"
    progress = [e for e in session.events if e.type == "tool.progress"]
    assert progress and progress[0].data["todos"][0]["content"] == "ship it"

    await drain(session.send("next"))
    assert runtime.todo_store.get("todo")[0].content == "ship it"
    await runtime.aclose()


async def test_todo_state_replays_after_runtime_restart_and_direct_session_open(tmp_path):
    first_runtime = make_runtime(
        tmp_path,
        ScriptedProvider(
            tool_response(
                (
                    "todo-call",
                    "todowrite",
                    {"todos": [{"id": "t1", "content": "durable", "status": "pending"}]},
                )
            ),
            text_response("saved"),
        ),
        make_config(),
    )
    first_session = first_runtime.session("todo-restart")
    await drain(first_session.send("save todos"))
    await first_runtime.aclose()

    second_runtime = make_runtime(
        tmp_path, ScriptedProvider(text_response("reopened")), make_config()
    )
    # The public session API restores immediately on reconnect.
    second_runtime.session("todo-restart", create=False)
    assert second_runtime.todo_store.get("todo-restart")[0].content == "durable"
    assert second_runtime.todo_store.revision("todo-restart") == 1

    # Callers may open through Runtime.sessions directly; the turn boundary
    # still restores before constructing/dispatching tools.
    third_runtime = make_runtime(
        tmp_path, ScriptedProvider(text_response("direct open")), make_config()
    )
    direct = third_runtime.sessions.open("todo-restart", create=False)
    assert third_runtime.todo_store.get("todo-restart") == ()
    third_runtime._make_tool_turn(
        config=make_config(), session=direct, turn_id="direct-turn", attended=False
    )
    assert third_runtime.todo_store.get("todo-restart")[0].content == "durable"
    await second_runtime.aclose()
    await third_runtime.aclose()


# ---------------------------------------------------------------------------
# ScriptedProvider is not the only path: core stays protocol-only
# ---------------------------------------------------------------------------


def test_core_loop_has_no_tools_dependency():
    import ast

    path = Path(__file__).resolve().parents[1] / "nexus" / "core" / "loop.py"
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                modules.add(node.module or "")
            else:
                # relative import inside nexus/core
                modules.add("nexus.core." + (node.module or ""))
    assert not any(m.startswith("nexus.tools") for m in modules)
    assert not any(m.startswith("nexus.session") for m in modules)
    assert not any(m.startswith("nexus.runtime") for m in modules)


async def test_slow_chatty_command_needs_two_model_calls_not_a_polling_loop(tmp_path):
    """Regression (plans/BASH_WAIT_PLAN.md): yield, then one wait; not 9+ calls."""
    import re

    def wait_for_yielded_job(req):
        text = str(req.messages[-1].content)
        match = re.search(r"job_id: (job_[0-9a-f]+)", text)
        assert match, text
        assert "moved to background" in text
        return tool_response(("w1", "bash", {"action": "wait", "job_id": match.group(1)}))

    provider = ScriptedProvider(
        tool_response((
            "b1",
            "bash",
            {"command": "for i in 1 2 3 4 5 6; do echo tick-$i; sleep 0.3; done; echo ALL-DONE"},
        )),
        [wait_for_yielded_job],
        text_response("finished"),
    )
    config = make_config()
    config = Config(
        model=config.model,
        version=2,
        v2=ConfigV2(
            model=config.v2.model,
            agent=config.v2.agent,
            permissions=config.v2.permissions,
            tools=ToolsSection(bash_yield_s=0.5),
        ),
    )
    runtime = make_runtime(tmp_path, provider, config)
    session = runtime.session("slow-chatty")
    events = await drain(session.send("run the slow build"))
    assert types(events)[-1] == "turn.completed"
    assert provider.calls == 3  # bash, one wait, final answer
    results = [
        block
        for m in session.messages
        for block in m.content
        if isinstance(block, ToolResult)
    ]
    assert len(results) == 2
    assert "ALL-DONE" in str(results[1].content)
    assert "status: completed" in str(results[1].content)
    await runtime.aclose()
