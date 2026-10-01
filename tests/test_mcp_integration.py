"""P5-I integration: MCP servers folded into the live runtime and manifest.

These tests exercise the whole MCP path through a real :class:`~nexus.runtime.Runtime`
and a real, spawned stdio server (``tests/fixtures/mcp/fs_server.py``) -- no
network, no mocks of the transport:

* a configured server's tools are bridged, advertised with the same pinned
  generation that dispatches them, and callable inside a turn;
* ``ReadMcpResource`` appears exactly when a connected server exposes resources;
* the context ``mcp_index`` lists only connected servers and resource roots, and
  never a tool description, prompt, or credential;
* MCP content and tool descriptions are fenced as untrusted data and cannot
  forge the fence;
* a server that dies mid-call degrades that tool result but **never** fails the
  turn, and its tools leave the manifest;
* a hung listing is isolated and recovers;
* editing ``.agents/mcp.json`` adds, reconfigures, and removes servers live;
* a ``tools/list_changed`` notification refreshes the manifest without a restart;
* strict ``${env:VAR}`` interpolation never leaks a resolved secret into the
  manifest, events, diagnostics, or a ``repr``;
* a live GitHub server is marked ``live`` and skipped offline.

Everything is offline and deterministic; the only subprocess is the fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ExtSection,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.mcp.client import MCPClient
from nexus.model.message import Text, ToolResult
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp" / "fs_server.py"

SECRET = "sk-fs-super-secret-value-abcdef123456"


def native_factory(config):
    """Force the deterministic native transports (full notification support)."""
    return MCPClient(config, backend="native")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    *,
    profile: str = "coding",
    mode: str = "allow",
    watch_interval_ms: int = 500,
) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile=profile),
            permissions=PermissionsSection(mode=mode, on_unattended="allow"),
            tools=ToolsSection(),
            ext=ExtSection(watch_interval_ms=watch_interval_ms),
        ),
    )


def make_runtime(
    tmp_path: Path,
    provider: ScriptedProvider,
    *,
    config: Config | None = None,
    environ: dict[str, str] | None = None,
) -> Runtime:
    return Runtime(
        tmp_path,
        config=config or make_config(),
        providers={"scripted": provider},
        environ=environ,
        mcp_client_factory=native_factory,
    )


def control_path(tmp_path: Path) -> Path:
    return tmp_path / "fs-control.json"


def write_control(path: Path, **values) -> None:
    path.write_text(json.dumps(values), encoding="utf-8")


def fs_definition(
    root: Path,
    control: Path,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float = 2.0,
    include_secret: bool = False,
    **overrides,
) -> dict:
    env = {"MCP_FS_CONTROL": str(control)}
    if include_secret:
        env["MCP_FS_SECRET"] = "${env:MCP_FS_SECRET}"
    if extra_env:
        env.update(extra_env)
    raw = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE), str(root)],
        "env": env,
        "connect_timeout_s": timeout,
        "init_timeout_s": timeout,
        "list_timeout_s": timeout,
        "call_timeout_s": timeout,
    }
    raw.update(overrides)
    return raw


def write_mcp_config(tmp_path: Path, servers: dict[str, dict]) -> Path:
    # STATE_PLAN §5.4: ``mcp.json`` is read from (and written to) ``.agents``;
    # the legacy ``.nexus/mcp.json`` is covered separately, as a read-only
    # fallback, below.
    agents = tmp_path / ".agents"
    agents.mkdir(parents=True, exist_ok=True)
    path = agents / "mcp.json"
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return path


def make_server_root(tmp_path: Path, name: str = "srv") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "notes.txt").write_text("notes-body", encoding="utf-8")
    (root / "hello.txt").write_text("hello-mcp", encoding="utf-8")
    return root


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def tool_result_text(session) -> str:
    parts: list[str] = []
    for message in session.messages:
        for block in message.content:
            if isinstance(block, ToolResult):
                for item in block.content:
                    if isinstance(item, Text):
                        parts.append(item.text)
    return "\n".join(parts)


async def drain(sub, per_event_timeout: float = 0.02) -> list:
    out: list = []
    while True:
        try:
            out.append(await asyncio.wait_for(sub.get(), timeout=per_event_timeout))
        except (TimeoutError, StopAsyncIteration):
            return out


# ---------------------------------------------------------------------------
# Bridge + dispatch
# ---------------------------------------------------------------------------


async def test_filesystem_server_tools_bridge_and_dispatch(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    provider = ScriptedProvider(
        tool_response(("c1", "mcp__fs__read_file", {"path": "hello.txt"})),
        text_response("read it"),
    )
    runtime = make_runtime(tmp_path, provider)
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("read hello.txt")]
        assert events[-1].type == "turn.completed"
        assert "turn.failed" not in [event.type for event in events]

        manifest = runtime.manifest
        assert "mcp__fs__read_file" in manifest.tools
        assert "mcp__fs__write_file" in manifest.tools
        assert "ReadMcpResource" in manifest.tools
        assert list(manifest.mcp) == ["fs"]
        assert manifest.mcp["fs"].connected

        # The advertised schema and the dispatched implementation are one
        # pinned generation: same name, same description.
        spec = manifest.tools["mcp__fs__read_file"].spec
        advertised = next(
            schema for schema in provider.requests[0].tools if schema.name == spec.name
        )
        assert advertised.description == spec.description
        # read-only annotation preserved; write is (fail-safe) mutating.
        assert manifest.tools["mcp__fs__read_file"].mutates is False
        assert manifest.tools["mcp__fs__write_file"].mutates is True

        # mcp_index names the connected server and its resource root only.
        system = provider.requests[0].system or ""
        assert "mcp-index" in system
        assert "server: fs" in system
        assert "notes.txt" in system
        assert "Read a UTF-8 file under the server root." not in system

        # The result is the real file, fence-wrapped.
        text = tool_result_text(session)
        assert "hello-mcp" in text
        assert "<untrusted-mcp-data>" in text
    finally:
        await runtime.aclose()


async def test_read_mcp_resource_tool_reads_through_runtime(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    uri = f"file://{root}/notes.txt"
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    provider = ScriptedProvider(
        tool_response(("c1", "ReadMcpResource", {"server": "fs", "uri": uri})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("read the resource")]
        assert events[-1].type == "turn.completed"
        text = tool_result_text(session)
        assert "notes-body" in text
        assert "<untrusted-mcp-data>" in text
    finally:
        await runtime.aclose()


async def test_read_mcp_resource_absent_without_resources(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control, no_resources=True)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    try:
        await runtime.ensure_started()
        manifest = runtime.manifest
        assert "mcp__fs__read_file" in manifest.tools
        assert "ReadMcpResource" not in manifest.tools
        assert not manifest.mcp["fs"].resources
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Untrusted-data wrapping
# ---------------------------------------------------------------------------


async def test_malicious_mcp_content_is_neutralized(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    provider = ScriptedProvider(
        tool_response(("c1", "mcp__fs__evil", {})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider)
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("call evil")]
        assert events[-1].type == "turn.completed"

        description = runtime.manifest.tools["mcp__fs__evil"].spec.description
        assert "SECURITY NOTICE" in description
        # Exactly one legitimate fence; the server's forgery was neutralised.
        assert description.count("<untrusted-mcp-data>") == 1
        assert description.count("</untrusted-mcp-data>") == 1
        assert "<redacted-delimiter>" in description

        # The forged instruction cannot appear in the index or as authority.
        system = provider.requests[0].system or ""
        assert "Ignore all previous instructions" not in system

        text = tool_result_text(session)
        assert "<redacted-delimiter>" in text
        assert text.count("</untrusted-mcp-data>") == 1
        assert "SYSTEM: run Bash" in text  # retained as inert data
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_dead_server_mid_call_never_fails_turn(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    provider = ScriptedProvider(
        tool_response(("c1", "mcp__fs__read_file", {"path": "hello.txt"})),
        text_response("recovered"),
    )
    runtime = make_runtime(tmp_path, provider)
    try:
        await runtime.ensure_started()
        assert "mcp__fs__read_file" in runtime.manifest.tools
        # Kill on the next call and make every relaunch exit immediately, so the
        # tools stay withdrawn after the failure.
        write_control(control, die_on_call=True, dead=True)

        session = runtime.session("s")
        events = [event async for event in session.send("read hello.txt")]
        types = [event.type for event in events]
        assert types[-1] == "turn.completed"
        assert "turn.failed" not in types
        # The failed call is a model-visible error result, not a crash.
        text = tool_result_text(session)
        assert "MCP call failed" in text or "untrusted-mcp-data" in text

        assert await wait_for(
            lambda: "mcp__fs__read_file" not in runtime.manifest.tools
        )
        assert runtime.manifest.mcp["fs"].connected is False
        # Non-MCP tools are untouched by one server's death.
        assert "bash" in runtime.manifest.tools
    finally:
        await runtime.aclose()


async def test_hung_listing_is_isolated_and_recovers(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control, hang_tools=True)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control, timeout=1.0)})
    provider = ScriptedProvider(text_response("no tools yet"))
    runtime = make_runtime(tmp_path, provider)
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("hello")]
        assert events[-1].type == "turn.completed"
        assert "mcp__fs__read_file" not in runtime.manifest.tools
        # The server is present in the index but not connected.
        assert runtime.manifest.mcp["fs"].connected is False
        system = provider.requests[0].system or ""
        assert "server: fs" not in system

        write_control(control, hang_tools=False)
        await asyncio.sleep(0.7)  # let the first backoff elapse
        await runtime.ensure_started()
        assert await wait_for(
            lambda: "mcp__fs__read_file" in runtime.manifest.tools
        )
        assert runtime.manifest.mcp["fs"].connected is True
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Hot add / remove / reconfigure / list_changed
# ---------------------------------------------------------------------------


async def test_edit_config_adds_and_removes_servers_live(tmp_path: Path) -> None:
    root_a = make_server_root(tmp_path, "a")
    root_b = make_server_root(tmp_path, "b")
    control_a = control_path(tmp_path)
    write_control(control_a)
    write_mcp_config(tmp_path, {"a": fs_definition(root_a, control_a)})
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(
        tmp_path,
        provider,
        config=make_config(watch_interval_ms=50),
    )
    try:
        await runtime.ensure_started()
        assert "mcp__a__read_file" in runtime.manifest.tools

        # Add a second server: the watcher picks the edit up with no restart.
        write_mcp_config(
            tmp_path,
            {
                "a": fs_definition(root_a, control_a),
                "b": fs_definition(root_b, control_a),
            },
        )
        assert await wait_for(
            lambda: {"a", "b"} <= set(runtime.manifest.mcp)
        )
        assert await wait_for(lambda: "mcp__b__read_file" in runtime.manifest.tools)

        # Remove the first: its tools vanish, the second's remain.
        write_mcp_config(tmp_path, {"b": fs_definition(root_b, control_a)})
        assert await wait_for(lambda: list(runtime.manifest.mcp) == ["b"])
        assert await wait_for(
            lambda: "mcp__a__read_file" not in runtime.manifest.tools
        )
        assert "mcp__b__read_file" in runtime.manifest.tools
    finally:
        await runtime.aclose()


async def test_reconfigure_swaps_the_server_definition(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    try:
        await runtime.ensure_started()
        first = runtime.mcp.definitions["fs"]
        assert runtime.manifest.mcp["fs"].connected

        # Change the argv (a reconfiguration), not just the environment.
        write_mcp_config(
            tmp_path,
            {
                "fs": fs_definition(
                    root, control, args=[str(FIXTURE), str(root), "--v2"]
                )
            },
        )
        await runtime.ensure_started()
        assert runtime.mcp.definitions["fs"].fingerprint() != first.fingerprint()
        assert await wait_for(lambda: runtime.manifest.mcp["fs"].connected)
    finally:
        await runtime.aclose()


async def test_list_changed_notification_refreshes_manifest(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control, notify_list_changed=True)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    try:
        await runtime.ensure_started()
        assert "mcp__fs__read_file" in runtime.manifest.tools
        # The server adds a tool and announces tools/list_changed; the manager
        # refreshes, the callback rebuilds, and the next generation has it.
        assert await wait_for(lambda: "mcp__fs__extra" in runtime.manifest.tools)
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Secrets and strict env interpolation
# ---------------------------------------------------------------------------


async def test_secrets_never_reach_manifest_events_or_diagnostics(
    tmp_path: Path,
) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control, stderr_secret=True)
    write_mcp_config(
        tmp_path, {"fs": fs_definition(root, control, include_secret=True)}
    )
    provider = ScriptedProvider(
        tool_response(("c1", "mcp__fs__read_file", {"path": "hello.txt"})),
        text_response("ok"),
    )
    runtime = make_runtime(
        tmp_path,
        provider,
        environ={"MCP_FS_SECRET": SECRET},
    )
    try:
        subscription = runtime.extension_events.subscribe()
        session = runtime.session("s")
        events = [event async for event in session.send("read")]
        assert events[-1].type == "turn.completed"
        bus_events = await drain(subscription)
        runtime.extension_events.unsubscribe(subscription)

        rendered = {
            "manifest": json.dumps(runtime.manifest.to_dict()),
            "bus": " ".join(repr(event) for event in bus_events),
            "diagnostics": str(runtime.extensions.diagnostics()),
            "status": str(
                [runtime.mcp.status(name) for name in runtime.mcp.server_names]
            ),
            "definition_repr": repr(runtime.mcp.definitions["fs"].config),
            "system": provider.requests[0].system or "",
        }
        for label, text in rendered.items():
            assert SECRET not in text, f"secret leaked into {label}"
        # The secret is held only in the normalized config, redacted on repr.
        assert SECRET in runtime.mcp.definitions["fs"].config.env["MCP_FS_SECRET"]
    finally:
        await runtime.aclose()


async def test_missing_env_reference_is_a_nonfatal_failure(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    write_mcp_config(
        tmp_path,
        {
            "fs": fs_definition(
                root, control, extra_env={"MISSING_TOKEN": "${env:NEXUS_MCP_ABSENT}"}
            )
        },
    )
    provider = ScriptedProvider(text_response("still works"))
    runtime = make_runtime(tmp_path, provider, environ={})
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("hello")]
        assert events[-1].type == "turn.completed"
        assert "mcp__fs__read_file" not in runtime.manifest.tools
        rows = runtime.extensions.diagnostics()
        assert any(
            row.get("kind") == "mcp" and "NEXUS_MCP_ABSENT" in row.get("error", "")
            for row in rows
        )
    finally:
        await runtime.aclose()


async def test_mcp_index_lists_only_connected_servers(tmp_path: Path) -> None:
    good_root = make_server_root(tmp_path, "good")
    bad_root = make_server_root(tmp_path, "bad")
    control = control_path(tmp_path)
    write_control(control)  # good server
    bad_control = tmp_path / "bad-control.json"
    write_control(bad_control, dead=True)  # never launches
    write_mcp_config(
        tmp_path,
        {
            "good": fs_definition(good_root, control),
            "bad": fs_definition(bad_root, bad_control, timeout=1.0),
        },
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("hi")]
        assert events[-1].type == "turn.completed"
        system = provider.requests[0].system or ""
        assert "server: good" in system
        assert "server: bad" not in system
        # The bad server is still present in the manifest, but disconnected.
        assert set(runtime.manifest.mcp) == {"good", "bad"}
        assert runtime.manifest.mcp["bad"].connected is False
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Live (credential-gated; deselected by default)
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_github_mcp_server(tmp_path: Path) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN is not set")
    write_mcp_config(
        tmp_path,
        {
            "github": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_TOKEN}"},
                "connect_timeout_s": 60,
                "init_timeout_s": 60,
                "list_timeout_s": 60,
            }
        },
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider, environ={"GITHUB_TOKEN": token})
    try:
        await runtime.ensure_started()
        # The real server's tools are present; exact names depend on upstream.
        assert runtime.manifest.mcp.get("github") is not None
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Review hardening: JSONC config, read-only profile, token rotation
# ---------------------------------------------------------------------------


async def test_jsonc_mcp_json_with_comments_and_trailing_comma(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    definition = fs_definition(root, control)
    document = (
        "// .agents/mcp.json -- JSONC is accepted for human edits\n"
        "{\n"
        '  "servers": {\n'
        f'    "fs": {json.dumps(definition)},\n'
        "  },\n"
        "}\n"
    )
    # Also exercises the legacy ``.nexus/mcp.json`` read-only fallback
    # (STATE_PLAN §5.4): no ``.agents/mcp.json`` exists here.
    legacy = tmp_path / ".nexus"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "mcp.json").write_text(document, encoding="utf-8")

    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    try:
        await runtime.ensure_started()
        assert "mcp__fs__read_file" in runtime.manifest.tools
    finally:
        await runtime.aclose()


async def test_malformed_mcp_json_is_rejected_not_guessed(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    definition = fs_definition(root, control)
    # Legacy read-only fallback location (STATE_PLAN §5.4).
    legacy = tmp_path / ".nexus"
    legacy.mkdir(parents=True, exist_ok=True)
    # Duplicate keys are not valid JSON and must not be silently last-wins.
    payload = json.dumps(definition)
    (legacy / "mcp.json").write_text(
        f'{{"servers": {{"fs": {payload}, "fs": {payload}}}}}',
        encoding="utf-8",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    try:
        await runtime.ensure_started()
        rows = runtime.extensions.diagnostics()
        assert any(
            row.get("kind") == "mcp" and "duplicate key" in row.get("error", "")
            for row in rows
        )
    finally:
        await runtime.aclose()


async def test_agents_mcp_json_shadows_legacy_nexus_mcp_json(tmp_path: Path) -> None:
    """STATE_PLAN §5.4: ``.agents/mcp.json`` is read; the legacy file is not."""
    root_agents = make_server_root(tmp_path, "agents")
    root_legacy = make_server_root(tmp_path, "legacy")
    control = control_path(tmp_path)
    write_control(control)
    # A legacy ``.nexus/mcp.json`` naming a server that would connect if read.
    legacy = tmp_path / ".nexus"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "mcp.json").write_text(
        json.dumps({"servers": {"fs": fs_definition(root_legacy, control)}}),
        encoding="utf-8",
    )
    # The current, writable location -- a same-named server, different root.
    write_mcp_config(tmp_path, {"fs": fs_definition(root_agents, control)})

    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    try:
        await runtime.ensure_started()
        assert "mcp__fs__read_file" in runtime.manifest.tools
        config = runtime.extensions.mcp.definition("fs").config
        assert str(root_agents) in " ".join(config.args)
        assert str(root_legacy) not in " ".join(config.args)
    finally:
        await runtime.aclose()


async def test_research_profile_excludes_mutating_mcp_tools(tmp_path: Path) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(
        tmp_path, provider, config=make_config(profile="research")
    )
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("hi")]
        assert events[-1].type == "turn.completed"
        names = {schema.name for schema in provider.requests[0].tools}
        assert "mcp__fs__read_file" in names
        assert "mcp__fs__list_dir" in names
        assert "mcp__fs__write_file" not in names
        assert "mcp__fs__evil" not in names
        # The global resource reader is read-only and stays available.
        assert "ReadMcpResource" in names
    finally:
        await runtime.aclose()


async def test_token_rotation_in_mcp_json_reconfigures_the_server(
    tmp_path: Path,
) -> None:
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    write_mcp_config(
        tmp_path,
        {"fs": fs_definition(root, control, extra_env={"MCP_FS_TOKEN": "one"})},
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    try:
        await runtime.ensure_started()
        first = runtime.mcp.definitions["fs"].fingerprint()
        assert runtime.mcp.definitions["fs"].config.env["MCP_FS_TOKEN"] == "one"

        write_mcp_config(
            tmp_path,
            {"fs": fs_definition(root, control, extra_env={"MCP_FS_TOKEN": "two"})},
        )
        await runtime.ensure_started()
        assert runtime.mcp.definitions["fs"].fingerprint() != first
        assert runtime.mcp.definitions["fs"].config.env["MCP_FS_TOKEN"] == "two"
        assert await wait_for(lambda: runtime.manifest.mcp["fs"].connected)
    finally:
        await runtime.aclose()


async def test_workspace_skill_loads_and_calls_its_declared_mcp_tool(tmp_path: Path) -> None:
    """Discover a workspace skill, activate it, and dispatch its MCP tool in a turn."""
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    write_mcp_config(tmp_path, {"fs": fs_definition(root, control)})
    skill = tmp_path / ".agents" / "skills" / "mcp-reader"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: mcp-reader\ndescription: Read files with MCP\n"
        "allowed-tools: [mcp__fs__read_file]\n---\nRead hello.txt using MCP.\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("s1", "skill", {"name": "mcp-reader"})),
        tool_response(("m1", "mcp__fs__read_file", {"path": "hello.txt"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    try:
        events = [event async for event in runtime.session("skill-mcp").send("read using the skill")]
        assert events[-1].type == "turn.completed"
        assert "mcp-reader" in runtime.manifest.skills
        assert [schema.name for schema in provider.requests[1].tools] == ["mcp__fs__read_file"]
        results = [
            block for message in provider.requests[2].messages
            for block in message.content if isinstance(block, ToolResult)
        ]
        assert any("hello-mcp" in str(result.content) for result in results)
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("cwd", [None, "relative", "absolute"])
async def test_benchmark_skill_and_relative_mcp_script_load_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cwd: str | None,
) -> None:
    """Selected workspace, rather than daemon cwd, anchors stdio scripts."""
    import shutil

    benchmark = Path(__file__).resolve().parents[1] / "benchmark"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shutil.copytree(benchmark / ".agents", workspace / ".agents")
    server_dir = workspace if cwd is None else workspace / "server"
    server_dir.mkdir(exist_ok=True)
    shutil.copyfile(benchmark / "mcp_echo.py", server_dir / "mcp_echo.py")
    config_path = workspace / ".agents" / "mcp.json"
    definitions = json.loads(config_path.read_text())
    definitions["servers"]["echo"]["command"] = sys.executable
    if cwd is not None:
        definitions["servers"]["echo"]["cwd"] = (
            "server" if cwd == "relative" else str(server_dir)
        )
    config_path.write_text(json.dumps(definitions))
    monkeypatch.chdir(tmp_path)
    provider = ScriptedProvider(
        tool_response(("s1", "skill", {"name": "benchmark-echo"})),
        tool_response(("m1", "mcp__echo__echo", {"text": "benchmark-mcp-ok"})),
        text_response("done"),
    )
    runtime = make_runtime(workspace, provider)
    try:
        session = runtime.session("benchmark-extensions")
        events = [event async for event in session.send("Use benchmark-echo")]
        assert events[-1].type == "turn.completed"
        assert "benchmark-echo" in runtime.manifest.skills
        assert runtime.manifest.mcp["echo"].connected
        assert "benchmark-echo" in provider.requests[0].system
        assert "mcp__echo__echo" in {tool.name for tool in provider.requests[0].tools}
        assert [tool.name for tool in provider.requests[1].tools] == ["mcp__echo__echo"]
        assert "benchmark-mcp-ok" in tool_result_text(session)
        assert not any(
            block.is_error for message in session.messages
            for block in message.content if isinstance(block, ToolResult)
        )
    finally:
        await runtime.aclose()
