"""Phase 5 P5-C: the MCP server manager (plan section 5.5).

The manager is the lifecycle half of MCP: it owns configured servers, connects
lazily, tracks health, restarts with exponential backoff behind a circuit
breaker, isolates a dead server so it can never fail a turn, caches listings
under an injected directory, reacts to ``list_changed``, and hot-applies a new
definition set.

Most tests inject a deterministic fake client through ``client_factory`` so
death, hang, restart, list_changed, cache, concurrency, and configuration are
exercised without a subprocess. A handful of integration tests use the real
:class:`~nexus.mcp.client.MCPClient` against the scriptable stdio fixture in
``tests/fixtures/mcp_server.py``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.core.bus import Bus
from nexus.mcp.client import (
    MCPCallResult,
    MCPClient,
    MCPContent,
    MCPPrompt,
    MCPPromptArgument,
    MCPResource,
    MCPResourceTemplate,
    MCPServerConfig,
    MCPServerInfo,
    MCPTool,
    parse_server_config,
)
from nexus.mcp.errors import (
    MCPCallError,
    MCPClosed,
    MCPConfigError,
    MCPTransportError,
)
from nexus.mcp.manager import (
    ApplyReport,
    MCPHealth,
    MCPManager,
    MCPServerSnapshot,
    MCPSnapshot,
    ServerDefinition,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = REPO_ROOT / "nexus" / "mcp" / "manager.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_server.py"
SECRET = "sk-manager-secret-value-123456"


@pytest.fixture(autouse=True)
def _isolate_mcp_bundle(monkeypatch):
    """Keep the bridge's ``mcp`` bundle registration from leaking to other tests."""
    from nexus.tools import bundles

    monkeypatch.setattr(bundles, "BUNDLES", bundles.BUNDLES)
    monkeypatch.setattr(bundles, "BUNDLE_NAMES", bundles.BUNDLE_NAMES)


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeClient:
    """A deterministic structural MCP client double."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        version: str = "1",
        tools: tuple[str, ...] = ("echo",),
        resources: tuple[tuple[str, str], ...] = (("file:///a", "hello"),),
        templates: tuple[str, ...] = (),
        prompts: tuple[str, ...] = ("greet",),
        connect_failures: int = 0,
        connect_error: BaseException | None = None,
        list_error: BaseException | None = None,
        call_error: BaseException | None = None,
    ) -> None:
        self.config = config
        self.version = version
        self.tools = tools
        self.resources = resources
        self.templates = templates
        self.prompts = prompts
        self.connect_failures = connect_failures
        self.connect_error = connect_error
        self.list_error = list_error
        self.call_error = call_error
        self.connect_calls = 0
        self.closed = 0
        self.connected = False
        self.list_calls = {"tools": 0, "resources": 0, "prompts": 0}
        self.call_calls: list[tuple[str, dict]] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self.server_info = MCPServerInfo(
            name="fake", version=version, capabilities={"tools": {}}
        )

    async def connect(self, *, timeout=None):
        self.connect_calls += 1
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise self.connect_error or MCPTransportError("fake connect refused")
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        return self.server_info

    async def aclose(self):
        self.closed += 1
        self.connected = False

    async def list_tools(self, *, timeout=None):
        self.list_calls["tools"] += 1
        if self.list_error is not None:
            raise self.list_error
        return tuple(
            MCPTool(name=name, description=f"tool {name}") for name in self.tools
        )

    async def list_resources(self, *, timeout=None):
        self.list_calls["resources"] += 1
        return tuple(
            MCPResource(uri=uri, name=uri, description="r")
            for uri, _ in self.resources
        )

    async def list_resource_templates(self, *, timeout=None):
        return tuple(
            MCPResourceTemplate(uri_template=template, name=template)
            for template in self.templates
        )

    async def list_prompts(self, *, timeout=None):
        self.list_calls["prompts"] += 1
        return tuple(
            MCPPrompt(
                name=name,
                description="p",
                arguments=(MCPPromptArgument(name="who", required=True),),
            )
            for name in self.prompts
        )

    async def call_tool(self, name, arguments, *, timeout=None):
        self.call_calls.append((name, dict(arguments or {})))
        if self.call_error is not None:
            raise self.call_error
        return MCPCallResult(
            content=(MCPContent(type="text", text=f"{name}:{arguments}"),)
        )

    async def read_resource(self, uri, *, timeout=None):
        for known, body in self.resources:
            if known == uri:
                return (MCPContent(type="text", text=body),)
        raise MCPCallError(-32002, "no such resource")

    def notifications(self):
        async def generator():
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item

        return generator()

    def emit(self, method: str) -> None:
        self._queue.put_nowait(SimpleNamespace(method=method, params={}))

    def stop_notifications(self) -> None:
        self._queue.put_nowait(None)


class ClientFactory:
    """A ``client_factory`` that builds (and records) one FakeClient per call."""

    def __init__(self, builder=None) -> None:
        self.builder = builder or (lambda config, index: FakeClient(config))
        self.clients: list[FakeClient] = []

    def __call__(self, config: MCPServerConfig) -> FakeClient:
        client = self.builder(config, len(self.clients))
        self.clients.append(client)
        return client

    @property
    def calls(self) -> int:
        return len(self.clients)

    def __getitem__(self, index: int) -> FakeClient:
        return self.clients[index]


def server_config(name: str = "s", **overrides) -> MCPServerConfig:
    raw = {
        "transport": "stdio",
        "command": "true",
        "connect_timeout_s": 2.0,
        "init_timeout_s": 2.0,
        "list_timeout_s": 2.0,
        "call_timeout_s": 2.0,
    }
    raw.update(overrides)
    return parse_server_config(name, raw, environ={})


def raw_definition(name: str = "s", **overrides) -> dict:
    raw = {
        "transport": "stdio",
        "command": "true",
        "connect_timeout_s": 2.0,
        "init_timeout_s": 2.0,
        "list_timeout_s": 2.0,
        "call_timeout_s": 2.0,
    }
    raw.update(overrides)
    return raw


def make_manager(
    definitions=None,
    *,
    factory: ClientFactory | None = None,
    sink=None,
    cache_dir=None,
    **kwargs,
) -> tuple[MCPManager, ClientFactory]:
    factory = factory or ClientFactory()
    manager = MCPManager(
        definitions if definitions is not None else {"s": raw_definition()},
        client_factory=factory,
        sink=sink,
        cache_dir=cache_dir,
        **kwargs,
    )
    return manager, factory


async def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def event_names(events: list) -> list[str]:
    return [getattr(event, "type", event) for event in events]


# ---------------------------------------------------------------------------
# Parsing and definitions
# ---------------------------------------------------------------------------


def test_parses_raw_config_and_definition_forms():
    factory = ClientFactory()
    manager = MCPManager(
        {
            "raw": raw_definition("raw"),
            "config": server_config("config"),
            "definition": ServerDefinition(name="definition", config=server_config()),
        },
        client_factory=factory,
    )
    assert manager.server_names == ("config", "definition", "raw")
    assert isinstance(manager.definition("raw").config, MCPServerConfig)
    assert manager.definition("config").config.name == "config"


def test_raw_enabled_flag_is_honored_and_not_passed_to_parser():
    manager, _ = make_manager(
        {"off": raw_definition("off", enabled=False), "on": raw_definition("on")}
    )
    assert manager.definition("off").enabled is False
    assert manager.definition("on").enabled is True


def test_bad_definition_is_recorded_not_fatal():
    manager, _ = make_manager(
        {"bad": {"transport": "carrier-pigeon"}, "good": raw_definition("good")}
    )
    assert manager.server_names == ("good",)
    diagnostics = manager.diagnostics()
    assert diagnostics
    assert any(row["name"] == "bad" for row in diagnostics)


def test_unknown_server_raises_config_error():
    manager, _ = make_manager()
    with pytest.raises(MCPConfigError):
        manager.status("missing")
    assert manager.server_snapshot("missing") is None


def test_apply_report_to_dict_is_json_safe():
    report = ApplyReport(
        generation=3,
        added=("a",),
        removed=("b",),
        reconfigured=("c",),
        unchanged=("d",),
    )
    assert report.changed is True
    json.dumps(report.to_dict())


# ---------------------------------------------------------------------------
# Lazy connect and concurrency
# ---------------------------------------------------------------------------


async def test_lazy_connect_only_on_first_use():
    manager, factory = make_manager()
    assert manager.status("s").health is MCPHealth.UNKNOWN
    assert factory.calls == 0
    client = await manager.ensure_connected("s")
    assert client is factory[0]
    assert manager.status("s").health is MCPHealth.READY
    assert factory.calls == 1
    # A second use reuses the same live client.
    assert await manager.ensure_connected("s") is client
    assert factory.calls == 1


async def test_snapshot_exposes_tools_resources_and_prompts():
    manager, _ = make_manager()
    await manager.ensure_connected("s")
    snapshot = manager.snapshot()
    assert isinstance(snapshot, MCPSnapshot)
    assert snapshot.tool_names() == ("mcp__s__echo",)
    assert snapshot.tools[0].origin == "mcp"
    assert snapshot.tools[0].bundle == "mcp"
    assert snapshot.resources[0].uri == "file:///a"
    assert snapshot.prompts[0].slash == "/mcp__s__greet"
    assert snapshot.server("s").health is MCPHealth.READY


async def test_concurrent_connect_is_single_flight():
    manager, factory = make_manager()
    clients = await asyncio.gather(*(manager.ensure_connected("s") for _ in range(12)))
    assert factory.calls == 1
    assert factory[0].connect_calls == 1
    assert all(client is factory[0] for client in clients)


async def test_tool_call_lazily_connects_and_wraps_result():
    manager, factory = make_manager()
    # No tools exist until something connects; the snapshot drives the tool.
    assert manager.tools() == ()
    await manager.ensure_connected("s")
    tool = manager.snapshot().tool("mcp__s__echo")
    assert tool is not None
    result = await tool.run({"text": "hi"}, None)
    assert result.is_error is False
    assert "untrusted-mcp-data" in result.content[0].text
    assert factory[0].call_calls == [("echo", {"text": "hi"})]


async def test_configured_name_is_used_for_qualification():
    manager, _ = make_manager({"github": raw_definition("github")})
    await manager.ensure_connected("github")
    assert manager.snapshot().tool_names() == ("mcp__github__echo",)


# ---------------------------------------------------------------------------
# Events and secret scrubbing
# ---------------------------------------------------------------------------


async def test_sync_sink_receives_connected_event():
    events: list = []
    manager, _ = make_manager(sink=lambda event: events.append(event))
    await manager.ensure_connected("s")
    assert "mcp.connected" in event_names(events)


async def test_async_sink_is_awaited():
    events: list = []

    async def sink(event):
        events.append(event)

    manager, _ = make_manager(sink=sink)
    await manager.ensure_connected("s")
    assert "mcp.connected" in event_names(events)


async def test_bus_sink_receives_events():
    bus = Bus()
    subscription = bus.subscribe()
    manager, _ = make_manager(sink=bus)
    await manager.ensure_connected("s")
    event = await asyncio.wait_for(subscription.get(), timeout=2.0)
    assert event.type == "mcp.connected"
    await bus.aclose()


async def test_secrets_are_scrubbed_from_failure_event_and_status():
    raw = raw_definition("s", env={"TOKEN": SECRET})
    factory = ClientFactory(
        lambda config, index: FakeClient(
            config, connect_error=MCPTransportError(f"dial failed token={SECRET}")
        )
    )
    events: list = []
    manager, _ = make_manager(
        {"s": raw}, factory=factory, sink=lambda event: events.append(event)
    )
    await manager.ensure_connected("s")
    failed = [e for e in events if e.type == "mcp.failed"]
    assert failed
    assert SECRET not in json.dumps(failed[0].to_dict())
    assert "***" in failed[0].data["error"]
    assert SECRET not in manager.status("s").last_error


async def test_broken_sink_never_breaks_a_connect():
    def broken(event):
        raise RuntimeError("sink exploded")

    manager, _ = make_manager(sink=broken)
    client = await manager.ensure_connected("s")
    assert client is not None
    assert manager.status("s").health is MCPHealth.READY


# ---------------------------------------------------------------------------
# Failure, backoff, and the circuit breaker
# ---------------------------------------------------------------------------


async def test_connect_failure_backs_off_and_withdraws_tools():
    factory = ClientFactory(
        lambda config, index: FakeClient(
            config, connect_error=MCPTransportError("nope")
        )
    )
    manager, _ = make_manager(
        factory=factory, backoff_base_s=0.05, backoff_max_s=1.0, restart_max=5
    )
    assert await manager.ensure_connected("s") is None
    status = manager.status("s")
    assert status.health is MCPHealth.BACKOFF
    assert status.attempts == 1
    assert status.retry_in_s > 0
    assert manager.snapshot().tool_names() == ()
    assert factory.calls == 1
    # A second call inside the backoff window does not dial again.
    assert await manager.ensure_connected("s") is None
    assert factory.calls == 1


def test_backoff_is_exponential_and_capped():
    manager, _ = make_manager(backoff_base_s=0.5, backoff_max_s=2.0)
    assert manager.backoff_delay(1) == 0.5
    assert manager.backoff_delay(2) == 1.0
    assert manager.backoff_delay(3) == 2.0
    assert manager.backoff_delay(9) == 2.0
    with pytest.raises(MCPConfigError):
        manager.backoff_delay(True)


async def test_circuit_opens_then_allows_one_half_open_attempt():
    factory = ClientFactory(
        lambda config, index: FakeClient(
            config, connect_error=MCPTransportError("down")
        )
    )
    manager, _ = make_manager(
        factory=factory,
        backoff_base_s=0.02,
        backoff_max_s=0.04,
        circuit_cooldown_s=0.08,
        restart_max=2,
    )
    assert await manager.ensure_connected("s") is None  # attempt 1 -> BACKOFF
    assert manager.status("s").health is MCPHealth.BACKOFF
    await asyncio.sleep(0.05)
    assert await manager.ensure_connected("s") is None  # attempt 2 -> circuit open
    assert manager.status("s").health is MCPHealth.FAILED
    assert factory.calls == 2
    # Circuit is open: a call inside cooldown must not dial.
    assert await manager.ensure_connected("s") is None
    assert factory.calls == 2
    # Cooldown elapses: exactly one half-open attempt is made.
    await asyncio.sleep(0.1)
    assert await manager.ensure_connected("s") is None
    assert factory.calls == 3


async def test_successful_restart_resets_attempts():
    factory = ClientFactory(
        lambda config, index: FakeClient(
            config, connect_failures=1 if index == 0 else 0, version="7"
        )
    )
    manager, _ = make_manager(
        factory=factory, backoff_base_s=0.02, restart_max=5
    )
    assert await manager.ensure_connected("s") is None
    assert manager.status("s").attempts == 1
    await asyncio.sleep(0.05)
    client = await manager.ensure_connected("s")
    assert client is not None
    status = manager.status("s")
    assert status.health is MCPHealth.READY
    assert status.attempts == 0
    assert status.version == "7"
    assert factory.calls == 2


async def test_selective_retry_honors_remaining_backoff():
    factory = ClientFactory(
        lambda config, index: FakeClient(config, connect_failures=3)
    )
    manager, _ = make_manager(
        factory=factory,
        backoff_base_s=0.5,
        backoff_max_s=1.0,
        restart_max=5,
    )
    assert await manager.ensure_connected("s") is None
    assert factory.calls == 1
    # Backoff has not elapsed: still no dial.
    assert await manager.ensure_connected("s") is None
    assert factory.calls == 1


async def test_remote_tool_error_does_not_break_the_connection():
    manager, factory = make_manager()
    await manager.ensure_connected("s")
    factory[0].call_error = MCPCallError(-32000, "tool said no")
    tool = manager.snapshot().tool("mcp__s__echo")
    result = await tool.run({}, None)
    assert result.is_error is True
    # A remote (JSON-RPC) error is tool-level: the server stays healthy.
    assert manager.status("s").health is MCPHealth.READY
    assert manager.snapshot().tool_names() == ("mcp__s__echo",)


async def test_fatal_call_removes_only_that_servers_tools():
    factory = ClientFactory()

    def builder(config, index):
        if config.name == "bad":
            client = FakeClient(config, tools=("bad",))
        else:
            client = FakeClient(config, tools=("good",))
        return client

    factory = ClientFactory(builder)
    manager, _ = make_manager(
        {"good": raw_definition("good"), "bad": raw_definition("bad")},
        factory=factory,
        backoff_base_s=0.02,
    )
    await manager.ensure_connected("good")
    await manager.ensure_connected("bad")
    assert set(manager.snapshot().tool_names()) == {
        "mcp__good__good",
        "mcp__bad__bad",
    }
    bad_client = next(c for c in factory.clients if c.config.name == "bad")
    bad_client.call_error = MCPTransportError("connection closed")
    tool = manager.snapshot().tool("mcp__bad__bad")
    result = await tool.run({}, None)
    assert result.is_error is True
    names = manager.snapshot().tool_names()
    assert names == ("mcp__good__good",)
    assert manager.status("bad").health is MCPHealth.BACKOFF
    assert manager.status("good").health is MCPHealth.READY
    await manager.aclose()


async def test_previously_connected_server_is_restarted_in_background():
    factory = ClientFactory()
    manager, _ = make_manager(
        factory=factory, backoff_base_s=0.02, backoff_max_s=0.05, restart_max=5
    )
    await manager.ensure_connected("s")
    factory[0].call_error = MCPTransportError("connection closed")
    tool = manager.snapshot().tool("mcp__s__echo")
    await tool.run({}, None)
    assert manager.status("s").health is MCPHealth.BACKOFF
    # No caller asks again: the manager restores the server on its own.
    assert await wait_for(
        lambda: manager.status("s").health is MCPHealth.READY, timeout=2.0
    )
    assert factory.calls == 2
    assert manager.snapshot().tool_names() == ("mcp__s__echo",)
    await manager.aclose()


# ---------------------------------------------------------------------------
# Snapshot and the ReadMcpResource contract
# ---------------------------------------------------------------------------


async def test_snapshot_is_immutable():
    manager, _ = make_manager()
    await manager.ensure_connected("s")
    snapshot = manager.snapshot()
    assert isinstance(snapshot, MCPSnapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.generation = 99  # type: ignore[misc]
    assert isinstance(snapshot.server("s"), MCPServerSnapshot)


async def test_read_resource_tool_reads_through_the_live_client():
    manager, _ = make_manager()
    await manager.ensure_connected("s")
    tool = manager.read_resource_tool()
    result = await tool.run({"server": "s", "uri": "file:///a"}, None)
    assert result.is_error is False
    assert "hello" in result.content[0].text


async def test_read_resource_tool_reports_offline_server():
    manager, _ = make_manager()
    tool = manager.read_resource_tool()
    result = await tool.run({"server": "s", "uri": "file:///a"}, None)
    assert result.is_error is True
    assert "unknown or offline" in result.content[0].text


# ---------------------------------------------------------------------------
# Refresh coalescing and list_changed
# ---------------------------------------------------------------------------


async def test_refresh_coalesces_concurrent_callers():
    manager, factory = make_manager()
    await manager.ensure_connected("s")
    before = factory[0].list_calls["tools"]
    await asyncio.gather(*(manager.refresh("s") for _ in range(8)))
    assert factory[0].list_calls["tools"] == before + 1


async def test_list_changed_notification_refreshes_and_emits():
    events: list = []
    manager, factory = make_manager(sink=lambda event: events.append(event))
    await manager.ensure_connected("s")
    assert manager.snapshot().tool_names() == ("mcp__s__echo",)
    factory[0].tools = ("echo", "extra")
    factory[0].emit("notifications/tools/list_changed")
    assert await wait_for(
        lambda: "mcp__s__extra" in manager.snapshot().tool_names()
    )
    assert "mcp.tools_changed" in event_names(events)
    assert factory[0].list_calls["tools"] >= 2


async def test_unrelated_notification_is_ignored():
    manager, factory = make_manager()
    await manager.ensure_connected("s")
    before = factory[0].list_calls["tools"]
    factory[0].emit("notifications/other/thing")
    await asyncio.sleep(0.05)
    assert factory[0].list_calls["tools"] == before


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


async def test_cache_hit_avoids_a_second_listing(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    first, _ = make_manager(cache_dir=cache_dir)
    await first.ensure_connected("s")
    assert first.cache_path("s") is not None
    assert first.cache_path("s").exists()
    await first.aclose()

    def builder(config, index):
        return FakeClient(
            config, list_error=MCPTransportError("must not list on a cache hit")
        )

    second, second_factory = make_manager(
        factory=ClientFactory(builder), cache_dir=cache_dir
    )
    client = await second.ensure_connected("s")
    assert client is not None
    status = second.status("s")
    assert status.health is MCPHealth.READY
    assert status.cached is True
    assert second.snapshot().tool_names() == ("mcp__s__echo",)
    assert second_factory[0].list_calls["tools"] == 0
    await second.aclose()


async def test_cache_key_changes_with_version(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    first, _ = make_manager(cache_dir=cache_dir)
    await first.ensure_connected("s")
    first_key = first.cache_key("s")
    await first.aclose()

    second, second_factory = make_manager(
        factory=ClientFactory(
            lambda config, index: FakeClient(config, version="2")
        ),
        cache_dir=cache_dir,
    )
    await second.ensure_connected("s")
    assert second.cache_key("s") != first_key
    assert second_factory[0].list_calls["tools"] == 1
    await second.aclose()


async def test_corrupt_cache_is_a_miss_and_is_repaired(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    first, _ = make_manager(cache_dir=cache_dir)
    await first.ensure_connected("s")
    path = first.cache_path("s")
    assert path is not None
    path.write_text("{not valid json", encoding="utf-8")
    await first.aclose()

    manager, factory = make_manager(cache_dir=cache_dir)
    await manager.ensure_connected("s")
    assert factory[0].list_calls["tools"] == 1
    assert manager.status("s").cached is False
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["tools"]
    await manager.aclose()


async def test_cache_write_is_atomic_and_leaves_no_temp_files(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    manager, _ = make_manager(cache_dir=cache_dir)
    await manager.ensure_connected("s")
    leftovers = list(cache_dir.glob(".tmp-*"))
    assert leftovers == []
    await manager.aclose()


async def test_invalidate_clears_the_cache_entry(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    manager, _ = make_manager(cache_dir=cache_dir)
    await manager.ensure_connected("s")
    path = manager.cache_path("s")
    assert path is not None and path.exists()
    await manager.invalidate("s")
    assert not path.exists()
    await manager.refresh("s")
    assert path.exists()
    await manager.aclose()


# ---------------------------------------------------------------------------
# Hot apply
# ---------------------------------------------------------------------------


async def test_apply_adds_removes_and_reconfigures():
    events: list = []
    manager, _ = make_manager(
        {
            "a": raw_definition("a"),
            "b": raw_definition("b"),
        },
        sink=lambda event: events.append(event),
    )
    await manager.ensure_connected("a")
    await manager.ensure_connected("b")
    before_generation = manager.generation

    report = await manager.apply(
        {
            "b": raw_definition("b", args=["changed"]),
            "c": raw_definition("c"),
        }
    )
    assert report.removed == ("a",)
    assert report.reconfigured == ("b",)
    assert report.added == ("c",)
    assert report.unchanged == ()
    assert manager.server_names == ("b", "c")
    assert manager.generation > before_generation
    names = event_names(events)
    assert "mcp.disconnected" in names
    assert "mcp.tools_changed" in names
    # The removed and reconfigured servers contributed tools that are now gone.
    assert manager.snapshot().tool_names() == ()


async def test_apply_reconfigure_withdraws_old_tools_and_reconnects_lazily():
    manager, _ = make_manager({"b": raw_definition("b")})
    await manager.ensure_connected("b")
    assert manager.snapshot().tool_names() == ("mcp__b__echo",)
    await manager.apply({"b": raw_definition("b", args=["changed"])})
    assert manager.snapshot().tool_names() == ()
    assert manager.status("b").health is MCPHealth.UNKNOWN
    client = await manager.ensure_connected("b")
    assert client is not None
    assert manager.snapshot().tool_names() == ("mcp__b__echo",)


async def test_apply_keeps_unchanged_connected_servers():
    manager, factory = make_manager({"a": raw_definition("a")})
    await manager.ensure_connected("a")
    original = factory[0]
    report = await manager.apply({"a": raw_definition("a")})
    assert report.unchanged == ("a",)
    assert report.changed is False
    assert manager.snapshot().tool_names() == ("mcp__a__echo",)
    assert factory.calls == 1
    assert original.closed == 0


async def test_apply_is_idempotent_and_generation_stable():
    manager, _ = make_manager({"a": raw_definition("a")})
    await manager.ensure_connected("a")
    generation = manager.generation
    await manager.apply({"a": raw_definition("a")})
    assert manager.generation == generation


async def test_apply_failures_do_not_drop_live_servers():
    manager, _ = make_manager({"a": raw_definition("a")})
    await manager.ensure_connected("a")
    report = await manager.apply(
        {"a": raw_definition("a"), "bad": {"transport": "nope"}}
    )
    assert report.failures
    assert manager.server_names == ("a",)
    assert manager.snapshot().tool_names() == ("mcp__a__echo",)


# ---------------------------------------------------------------------------
# Disable and close
# ---------------------------------------------------------------------------


async def test_disabled_manager_never_dials():
    manager, factory = make_manager(enabled=False)
    assert await manager.ensure_connected("s") is None
    assert factory.calls == 0
    assert manager.status("s").health is MCPHealth.DISABLED
    assert manager.status("s").enabled is False


async def test_disabled_server_never_dials():
    manager, factory = make_manager(
        {"off": raw_definition("off", enabled=False), "on": raw_definition("on")}
    )
    assert await manager.ensure_connected("off") is None
    assert manager.status("off").health is MCPHealth.DISABLED
    assert factory.calls == 0
    assert await manager.ensure_connected("on") is not None
    assert factory.calls == 1


async def test_disconnect_withdraws_tools_and_emits():
    events: list = []
    manager, factory = make_manager(sink=lambda event: events.append(event))
    await manager.ensure_connected("s")
    assert await manager.disconnect("s") is True
    assert manager.snapshot().tool_names() == ()
    assert factory[0].closed >= 1
    assert "mcp.disconnected" in event_names(events)
    assert await manager.disconnect("s") is False


async def test_close_is_deterministic_and_idempotent():
    events: list = []
    manager, factory = make_manager(sink=lambda event: events.append(event))
    await manager.ensure_connected("s")
    state = manager._states["s"]
    await manager.aclose()
    assert manager.closed is True
    assert factory[0].closed == 1
    assert manager.status("s").health is MCPHealth.CLOSED
    assert manager.snapshot().tool_names() == ()
    assert state.notify_task is None or state.notify_task.done()
    assert "mcp.disconnected" in event_names(events)
    await manager.aclose()  # idempotent
    assert factory[0].closed == 1
    with pytest.raises(MCPClosed):
        await manager.apply({})


async def test_async_context_manager_closes():
    factory = ClientFactory()
    async with MCPManager(
        {"s": raw_definition()}, client_factory=factory
    ) as manager:
        await manager.ensure_connected("s")
    assert factory[0].closed == 1


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------


def test_manager_does_not_import_upstream_mcp():
    tree = ast.parse(MANAGER_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.append(node.module or "")
    assert not [
        name for name in imported if name == "mcp" or name.startswith("mcp.")
    ], imported


def test_manager_does_not_import_runtime_or_extension_layers():
    tree = ast.parse(MANAGER_PATH.read_text(encoding="utf-8"))
    forbidden = ("nexus.runtime", "nexus.ext", "nexus.session", "nexus.core")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert not name.startswith(forbidden), name


# ---------------------------------------------------------------------------
# Integration: the real client against the scriptable stdio fixture
# ---------------------------------------------------------------------------


def native_factory(config: MCPServerConfig) -> MCPClient:
    return MCPClient(config, backend="native")


def control_path(tmp_path: Path) -> Path:
    return tmp_path / "control.json"


def write_control(path: Path, **values) -> None:
    path.write_text(json.dumps(values), encoding="utf-8")


def fixture_definition(path: Path, **overrides) -> dict:
    raw = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE)],
        "env": {"MCP_FIXTURE_CONTROL": str(path)},
        "connect_timeout_s": 2.0,
        "init_timeout_s": 2.0,
        "list_timeout_s": 2.0,
        "call_timeout_s": 2.0,
    }
    raw.update(overrides)
    return raw


async def test_integration_connect_list_call_and_close(tmp_path: Path):
    control = control_path(tmp_path)
    write_control(control, launches=0, tools=["extra"])
    events: list = []
    manager = MCPManager(
        {"fixture": fixture_definition(control)},
        client_factory=native_factory,
        sink=lambda event: events.append(event),
        backoff_base_s=0.05,
    )
    try:
        client = await manager.ensure_connected("fixture")
        assert client is not None
        names = manager.snapshot().tool_names()
        assert "mcp__fixture__echo" in names
        assert "mcp__fixture__extra" in names
        assert manager.snapshot().resources
        assert manager.snapshot().prompts
        tool = manager.snapshot().tool("mcp__fixture__echo")
        result = await tool.run({"text": "ping"}, None)
        assert result.is_error is False
        assert "ping" in result.content[0].text
        assert "mcp.connected" in event_names(events)
    finally:
        await manager.aclose()
    assert manager.status("fixture").health is MCPHealth.CLOSED


async def test_integration_restart_after_failed_launch(tmp_path: Path):
    control = control_path(tmp_path)
    write_control(control, launches=0, fail_launches=1)
    manager = MCPManager(
        {"fixture": fixture_definition(control)},
        client_factory=native_factory,
        backoff_base_s=0.05,
        backoff_max_s=0.1,
        restart_max=5,
    )
    try:
        assert await manager.ensure_connected("fixture") is None
        assert manager.status("fixture").health is MCPHealth.BACKOFF
        assert manager.snapshot().tool_names() == ()
        # An initial connect failure is lazily retried: after the backoff and a
        # now-healthy launch, the next use reconnects.
        control.write_text(json.dumps({"launches": 1, "fail_launches": 0}))
        await asyncio.sleep(0.1)
        assert await manager.ensure_connected("fixture") is not None
        assert manager.status("fixture").health is MCPHealth.READY
        assert "mcp__fixture__echo" in manager.snapshot().tool_names()
    finally:
        await manager.aclose()


async def test_integration_server_dies_mid_call_then_recovers(tmp_path: Path):
    control = control_path(tmp_path)
    write_control(control, launches=0, die_on_call=True)
    manager = MCPManager(
        {"fixture": fixture_definition(control)},
        client_factory=native_factory,
        backoff_base_s=0.05,
        backoff_max_s=0.1,
        restart_max=5,
    )
    try:
        await manager.ensure_connected("fixture")
        assert "mcp__fixture__echo" in manager.snapshot().tool_names()
        tool = manager.snapshot().tool("mcp__fixture__echo")
        result = await tool.run({"text": "boom"}, None)
        assert result.is_error is True
        assert manager.status("fixture").health is MCPHealth.BACKOFF
        assert manager.snapshot().tool_names() == ()
        control.write_text(json.dumps({"launches": 1, "die_on_call": False}))
        await asyncio.sleep(0.1)
        assert await wait_for(
            lambda: manager.status("fixture").health is MCPHealth.READY,
            timeout=3.0,
        )
    finally:
        await manager.aclose()


async def test_integration_list_changed_refresh(tmp_path: Path):
    control = control_path(tmp_path)
    write_control(control, launches=0, notify_list_changed=True)
    events: list = []
    manager = MCPManager(
        {"fixture": fixture_definition(control)},
        client_factory=native_factory,
        sink=lambda event: events.append(event),
        backoff_base_s=0.05,
    )
    try:
        await manager.ensure_connected("fixture")
        assert await wait_for(
            lambda: "mcp__fixture__extra" in manager.snapshot().tool_names(),
            timeout=3.0,
        )
        assert "mcp.tools_changed" in event_names(events)
    finally:
        await manager.aclose()


# ---------------------------------------------------------------------------
# Review hardening: provenance, cache issues, collisions, races, fingerprint
# ---------------------------------------------------------------------------


async def test_resource_templates_are_listed_and_bridged():
    factory = ClientFactory(
        lambda config, index: FakeClient(
            config, templates=("file:///{path}", "db://{id}")
        )
    )
    manager, _ = make_manager(factory=factory)
    await manager.ensure_connected("s")
    snapshot = manager.snapshot()
    assert [item.uri for item in snapshot.resource_templates] == [
        "file:///{path}",
        "db://{id}",
    ]
    assert snapshot.server("s").resource_templates


async def test_generation_provenance_matches_the_published_snapshot():
    manager, _ = make_manager()
    await manager.ensure_connected("s")
    snapshot = manager.snapshot()
    assert snapshot.generation == manager.generation
    tool = snapshot.tool("mcp__s__echo")
    assert tool is not None
    assert tool.generation == manager.generation
    assert snapshot.server("s").generation == manager.generation


def test_backoff_is_bounded_for_absurd_attempt_counts():
    manager, _ = make_manager(backoff_base_s=0.5, backoff_max_s=2.0)
    assert manager.backoff_delay(10 ** 9) == 2.0
    assert manager.backoff_delay(10 ** 100) == 2.0


def test_config_fingerprint_changes_when_a_token_rotates_without_leaking():
    first = ServerDefinition(
        name="s", config=server_config("s", env={"TOKEN": "token-one"})
    )
    second = ServerDefinition(
        name="s", config=server_config("s", env={"TOKEN": "token-two"})
    )
    same = ServerDefinition(
        name="s", config=server_config("s", env={"TOKEN": "token-one"})
    )
    assert first.fingerprint() != second.fingerprint()
    assert first.fingerprint() == same.fingerprint()
    assert "token-one" not in first.fingerprint()


async def test_token_rotation_triggers_reconfiguration():
    manager, _ = make_manager({"s": raw_definition("s", env={"TOKEN": "one"})})
    await manager.ensure_connected("s")
    report = await manager.apply({"s": raw_definition("s", env={"TOKEN": "two"})})
    assert report.reconfigured == ("s",)
    assert manager.status("s").health is MCPHealth.UNKNOWN


async def test_cache_preserves_list_issues_and_degraded_health(tmp_path: Path):
    cache_dir = tmp_path / "cache"

    def builder(config, index):
        return FakeClient(
            config, list_error=MCPCallError(-32000, "listing unavailable")
        )

    first, _ = make_manager(factory=ClientFactory(builder), cache_dir=cache_dir)
    await first.ensure_connected("s")
    assert first.status("s").health is MCPHealth.DEGRADED
    await first.aclose()

    second, second_factory = make_manager(
        factory=ClientFactory(builder), cache_dir=cache_dir
    )
    await second.ensure_connected("s")
    assert second.status("s").cached is True
    assert second.status("s").health is MCPHealth.DEGRADED
    assert second_factory[0].list_calls["tools"] == 0
    assert any(
        issue.code == "list_failed" for issue in second.snapshot().server("s").issues
    )
    await second.aclose()


async def test_cross_server_name_collision_is_diagnosed_and_deduped():
    def builder(config, index):
        return FakeClient(config, tools=("x",))

    manager, _ = make_manager(
        {"a-b": raw_definition("a-b"), "a.b": raw_definition("a.b")},
        factory=ClientFactory(builder),
    )
    await manager.ensure_connected("a-b")
    await manager.ensure_connected("a.b")
    names = manager.snapshot().tool_names()
    assert names.count("mcp__a_b__x") == 1
    loser = manager.snapshot().server("a.b")
    assert any(issue.code == "cross_server_collision" for issue in loser.issues)
    assert any(row.get("kind") == "collision" for row in manager.diagnostics())


async def test_read_resource_resolver_never_serves_a_stale_client():
    manager, _factory = make_manager()
    await manager.ensure_connected("s")
    tool = manager.read_resource_tool()
    assert (await tool.run({"server": "s", "uri": "file:///a"}, None)).is_error is False

    await manager.disconnect("s")
    stale = await tool.run({"server": "s", "uri": "file:///a"}, None)
    assert stale.is_error is True
    assert "unknown or offline" in stale.content[0].text

    await manager.ensure_connected("s")
    fresh = await tool.run({"server": "s", "uri": "file:///a"}, None)
    assert fresh.is_error is False


async def test_disconnect_during_connect_closes_the_new_client():
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowClient(FakeClient):
        async def connect(self, *, timeout=None):
            started.set()
            await release.wait()
            return await super().connect(timeout=timeout)

    holder: dict[str, SlowClient] = {}

    def builder(config, index):
        holder["client"] = SlowClient(config)
        return holder["client"]

    manager, _ = make_manager(factory=ClientFactory(builder))
    task = asyncio.create_task(manager.ensure_connected("s"))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await manager.disconnect("s")
    release.set()
    assert await asyncio.wait_for(task, timeout=2.0) is None
    assert holder["client"].closed >= 1
    assert manager._states["s"].client is None


def test_default_factory_wires_a_bounded_file_stderr_sink(tmp_path: Path):
    from nexus.config.paths import project_state_dir
    from nexus.mcp.client import FileStderrSink

    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    manager = MCPManager(
        {"s": raw_definition("s")}, workspace=workspace, home=home, client_factory=None
    )
    client = manager._default_client_factory(manager.definition("s").config)
    assert isinstance(client._stderr_sink, FileStderrSink)
    # STATE_PLAN §5.4: server stderr logs are per-project machine state under
    # ``project_state_dir()``, never inside the workspace.
    expected = project_state_dir(workspace, home) / "logs" / "mcp" / "s.log"
    assert client._stderr_sink.path == expected
    assert client._stderr_sink.max_bytes > 0
