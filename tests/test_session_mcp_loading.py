"""First-turn MCP mode choices survive reconnect and preserve request prefixes."""
import msgspec

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import text_response
from test_mcp_search_tools import setup
from test_mcp_integration import write_mcp_config, fs_definition, make_server_root, control_path, write_control


async def test_reconnect_freeze_and_preview_follow_session(tmp_path):
    runtime, provider = setup(tmp_path, text_response("done"), mode="all")
    try:
        facade = HostFacade(runtime)
        facade.open_session("s")
        preview = await facade.handle(p.ContextMcpLoadingSelect("s", "fs", "search"))
        assert "McpCall" in {tool["name"] for tool in preview.tools}
        assert not any(tool["name"].startswith("mcp__") and tool.get("enabled") for tool in preview.tools)
        session = runtime.session("s")
        live = [event async for event in session.send("hi")]
        assert live == session.events[-len(live):]
        assert session.events[0].type == "context.mcp_loading_selected"
        assert live[0].type == "turn.started" and live[1].type == "context.mcp_loading_frozen"
        schemas = msgspec.json.encode(provider.requests[0].tools)
    finally:
        await runtime.aclose()
    runtime2, provider2 = setup(tmp_path, text_response("again"), mode="all")
    try:
        session2 = runtime2.session("s")
        assert session2.mcp_loading_choices == {"fs": "search"}
        assert session2.mcp_loading_frozen == {"fs": "search"}
        facade2 = HostFacade(runtime2)
        preview2 = await facade2.handle(p.ContextInspect("s"))
        assert preview2.mcp_servers[0]["tool_loading"] == "search"
        assert "McpCall" in {tool["name"] for tool in preview2.tools}
        [event async for event in session2.send("continue")]
        assert msgspec.json.encode(provider2.requests[0].tools) == schemas
        assert len([e for e in session2.events if e.type == "context.mcp_loading_frozen"]) == 1
    finally:
        await runtime2.aclose()


async def test_follow_config_and_new_server_after_first_turn(tmp_path):
    runtime, _ = setup(tmp_path, text_response("done"), mode="all")
    try:
        session = runtime.session("s")
        session.select_mcp_loading("fs", "search")
        session.select_mcp_loading("fs", None)
        assert session.mcp_loading_choices == {}
        [event async for event in session.send("hi")]
        assert session.mcp_loading_frozen == {"fs": "all"}
        root = make_server_root(tmp_path)
        control = control_path(tmp_path)
        write_control(control)
        definition = fs_definition(root, control)
        write_mcp_config(tmp_path, {"fs": definition, "added": definition})
        await runtime.extensions.reload()
        selected = runtime._selected_manifest(runtime.manifest, session)
        assert selected.mcp["fs"].tool_loading == "all"
        assert selected.mcp["added"].tool_loading == "search"
    finally:
        await runtime.aclose()


async def test_no_enabled_or_all_servers_have_no_proxies(tmp_path):
    runtime, _ = setup(tmp_path, text_response("done"), mode="all")
    try:
        facade = HostFacade(runtime)
        facade.open_session("s")
        preview = await facade.handle(p.ContextInspect("s"))
        assert "McpCall" not in {tool["name"] for tool in preview.tools}
        await runtime.mcp.apply({"off": {"command": "unused", "enabled": False}})
        await runtime.extensions.reload()
        session = runtime.session("s")
        session.select_extension("mcp", "fs", False)
        selected = runtime._selected_manifest(runtime.manifest, session)
        assert "McpCall" not in selected.tools
    finally:
        await runtime.aclose()


async def test_detached_turn_freezes_once_before_first_model_request(tmp_path):
    runtime, provider = setup(tmp_path, text_response("done"), mode="search")
    try:
        session = runtime.session("s")
        turn = await session.start_turn("hi")
        await session.wait_idle()
        frozen = [e for e in session.events if e.type == "context.mcp_loading_frozen"]
        assert len(frozen) == 1 and turn
        assert session.mcp_loading_frozen == {"fs": "search"}
        types = [e.type for e in session.events]
        assert types.index("turn.started") < types.index("context.mcp_loading_frozen")
        replayed = [e async for e in session.subscribe(0, follow=False)]
        assert [e.seq for e in replayed if e.type == "context.mcp_loading_frozen"] == [frozen[0].seq]
        assert provider.requests
    finally:
        await runtime.aclose()
