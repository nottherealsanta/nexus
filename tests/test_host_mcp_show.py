"""MCP detail inspection reads retained state without remote operations."""
import json
from types import SimpleNamespace

import msgspec

from nexus.host import HostFacade
from nexus.host import protocol as p
from test_mcp_manager import make_manager, raw_definition, SECRET


def test_protocol_roundtrip():
    command = p.McpServerShow(session="s", name="demo", max_bytes=40)
    assert p.decode_command(msgspec.json.encode(command)) == command
    result = p.McpServerShowResult(name="demo", detail={"tools": []}, clipped=True)
    assert p.decode_result(msgspec.json.encode(result)) == result


def test_disconnected_snapshot_never_connects():
    manager, factory = make_manager({"s": raw_definition(command="/private/bin/demo", args=[SECRET], env={"TOKEN": SECRET})})
    detail = manager.server_detail("s")
    assert detail["status"] == "disconnected"
    assert detail["command_label"] == "demo (1 args)"
    assert detail["instructions"] is None
    assert manager.server_detail("missing") is None
    assert not factory.clients


async def test_host_snapshot_redacts_and_clips():
    manager, factory = make_manager({"s": raw_definition(env={"TOKEN": SECRET})})
    await manager.ensure_connected("s")
    before = len(factory.clients)
    client_state = [(c.connect_calls, dict(c.list_calls), list(c.call_calls)) for c in factory.clients]
    detail = manager.server_detail("s")
    assert detail["tools"][0]["input_schema"]
    original = manager.server_detail
    def with_secret(name):
        data = original(name)
        if data is None:
            return None
        data["error"] = SECRET
        data["instructions"] = SECRET + "é" * 1000
        return data
    manager.server_detail = with_secret
    runtime = SimpleNamespace(_mcp=manager, _extensions=None, sessions=SimpleNamespace(_handles={}))
    facade = HostFacade(runtime)
    try:
        result = await facade.handle(p.McpServerShow(session="s", name="s"))
        assert isinstance(result, p.McpServerShowResult), result
        assert SECRET not in msgspec.json.encode(result).decode()
        assert result.tools[0]["input_schema"] == detail["tools"][0]["input_schema"]
        assert not result.clipped
        full_bytes = len(json.dumps(result.detail, ensure_ascii=False).encode("utf-8"))
        for limit in (0, 1, 32, full_bytes - 1, full_bytes):
            bounded = await facade.handle(p.McpServerShow(session="s", name="s", max_bytes=limit))
            assert "snapshot_json" not in bounded.detail
            if bounded.detail:
                assert len(json.dumps(bounded.detail, ensure_ascii=False).encode("utf-8")) <= limit
            assert bounded.clipped == (limit < full_bytes)
            if bounded.clipped:
                assert bounded.detail == {}
                assert bounded.status == result.status
                assert bounded.command_label == result.command_label
                assert bounded.tool_loading == result.tool_loading
                assert bounded.tools == bounded.resources == bounded.prompts == []
                assert bounded.server_info == {}
                assert bounded.instructions == ""
                assert bounded.error.startswith("MCP snapshot exceeds requested byte limit; increase max_bytes")
                assert "all omitted" in bounded.error
        assert len(factory.clients) == before
        assert [(c.connect_calls, c.list_calls, c.call_calls) for c in factory.clients] == client_state
        missing = await facade.handle(p.McpServerShow(session="s", name="missing"))
        assert missing.error
        invalid = await facade.handle(p.McpServerShow(session="s", name="s", max_bytes=-1))
        assert invalid.error
    finally:
        await manager.aclose()


async def test_client_sends_snapshot_command_only():
    from nexus.client.protocol import Client

    class Transport:
        async def request(self, command):
            self.command = command
            return p.McpServerShowResult(name=command.name)

    transport = Transport()
    result = await Client(transport).mcp_server_show("session", "server", max_bytes=123)
    assert transport.command == p.McpServerShow(session="session", name="server", max_bytes=123)
    assert result.name == "server"


async def test_http_transport_omits_credentials_and_endpoint():
    manager, _ = make_manager({"s": raw_definition(
        transport="http", command="", url="https://user:password@example.invalid/private?token=secret",
        headers={"Authorization": SECRET},
    )})
    detail = manager.server_detail("s")
    assert detail["transport"] == "http"
    assert detail["command_label"] == ""
    assert "example.invalid" not in str(detail)
    assert SECRET not in str(detail)


def test_restart_protocol_roundtrip():
    command = p.McpServerRestart(session="s", name="demo")
    assert p.decode_command(msgspec.json.encode(command)) == command
    result = p.McpServerRestartResult(name="demo", status="connected")
    assert p.decode_result(msgspec.json.encode(result)) == result


async def test_restart_reconnects_one_server():
    manager, factory = make_manager({"s": raw_definition(env={"TOKEN": SECRET})})
    await manager.ensure_connected("s")
    first = factory.clients[-1]
    facade = HostFacade(SimpleNamespace(_mcp=manager))
    try:
        result = await facade.handle(p.McpServerRestart(session="x", name="s"))
        assert isinstance(result, p.McpServerRestartResult), result
        assert result.status == "connected" and not result.error
        assert len(factory.clients) == 2 and factory.clients[-1] is not first
        missing = await facade.handle(p.McpServerRestart(session="x", name="missing"))
        assert missing.error == "Unknown MCP server"
    finally:
        await manager.aclose()
