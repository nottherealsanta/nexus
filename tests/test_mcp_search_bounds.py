"""McpSearch/McpCall failure isolation and bounds (MCP_SEARCH_PLAN phase 4)."""
from nexus.config import Config
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.mcp.bridge import build_search_tools
from nexus.mcp.client import MCPTool
from nexus.mcp.errors import MCPTransportError
from nexus.tools.manager import ToolManager
from nexus.tools.permissions import Decision
from nexus.tools.spec import ToolCall
from test_tools_manager import cfg
from test_mcp_manager import ClientFactory, FakeClient, make_manager, raw_definition


async def run_search(manager, tools, arguments, config=None):
    batch = manager.prepare([ToolCall("q", "McpSearch", arguments)])
    result = await manager.dispatch(batch.with_decisions({"q": Decision.ALLOW_ONCE}))
    return result[0].content[0].text


def big_client(size, names=("big",)):
    class Big(FakeClient):
        async def list_tools(self, *, timeout=None):
            return tuple(MCPTool(name=n, description="large schema tool",
                input_schema={"type": "object", "properties": {"blob": {"type": "string", "description": "x" * size}}})
                for n in names)
    return Big


async def test_one_server_failing_does_not_fail_the_other_servers_search(tmp_path):
    def builder(config, index):
        if config.name == "bad":
            return FakeClient(config, connect_error=MCPTransportError("refused"))
        return FakeClient(config, tools=("echo",))
    mcp, _ = make_manager({"bad": raw_definition("bad"), "good": raw_definition("good")}, factory=ClientFactory(builder))
    try:
        manager = ToolManager(Config(), workspace=tmp_path, tools=build_search_tools(mcp, {"bad": "search", "good": "search"}))
        text = await run_search(manager, None, {"queries": [{"query": "echo"}, {"server": "good", "query": "echo"}]})
        assert text.count("good/echo") >= 2
        assert "server bad failed" in text.split("Query 2")[0]
        assert "failed" not in text.split("Query 2")[1]
        await manager.aclose()
    finally:
        await mcp.aclose()


async def test_schema_clipping_announced_and_select_alone_gets_more(tmp_path):
    factory = ClientFactory(lambda config, index: big_client(12000)(config))
    mcp, _ = make_manager({"s": raw_definition("s")}, factory=factory)
    try:
        manager = ToolManager(Config(), workspace=tmp_path, tools=build_search_tools(mcp, {"s": "search"}))
        keyword = await run_search(manager, None, {"queries": [{"query": "large"}]})
        assert "Schema clipped; call McpSearch with select:s/big" in keyword
        alone = await run_search(manager, None, {"queries": [{"query": "select:big"}]})
        assert "Schema clipped" not in alone and len(alone) > len(keyword)
        await manager.aclose()
    finally:
        await mcp.aclose()


async def test_total_result_is_bounded_and_clipping_announced(tmp_path):
    names = tuple(f"tool_{i}" for i in range(10))
    factory = ClientFactory(lambda config, index: big_client(7000, names)(config))
    mcp, _ = make_manager({"s": raw_definition("s")}, factory=factory)
    try:
        config = cfg(max_result_tokens=2000)
        manager = ToolManager(config, workspace=tmp_path, tools=build_search_tools(mcp, {"s": "search"}))
        text = await run_search(manager, None, {"queries": [{"query": "large"}], "limit": 10})
        assert "Search result clipped" in text
        assert len(text) <= config.v2.tools.max_result_tokens * 4 + 400
        await manager.aclose()
    finally:
        await mcp.aclose()


async def test_call_uses_target_timeout_and_duplicate_calls_resolve_same_target(tmp_path):
    mcp, factory = make_manager({"s": raw_definition("s", call_timeout_s=7.0)})
    try:
        manager = ToolManager(Config(), workspace=tmp_path, tools=build_search_tools(mcp, {"s": "search"}))
        await run_search(manager, None, {"queries": [{"query": "echo"}]})
        batch = manager.prepare([ToolCall("a", "McpCall", {"tool": "s/echo", "arguments": {}}),
                                 ToolCall("b", "McpCall", {"tool": "mcp__s__echo", "arguments": {}})])
        assert [e.target.spec.name for e in batch.entries] == ["mcp__s__echo"] * 2
        assert all(e.target.spec.timeout_s == 7.0 for e in batch.entries)
        result = await manager.dispatch(batch.with_decisions({"a": Decision.ALLOW_ONCE, "b": Decision.ALLOW_ONCE}))
        assert len(result) == 2 and len(factory[0].call_calls) == 2
        await manager.aclose()
    finally:
        await mcp.aclose()


async def test_child_agent_inherits_frozen_modes_and_fixed_proxy_tools(tmp_path):
    import msgspec
    from nexus.model.providers.scripted import text_response, tool_response
    from test_mcp_search_tools import setup

    runtime, provider = setup(tmp_path,
        tool_response(("t", "subagent", {"prompt": "read it", "subagent_type": "general"})),
        tool_response(("s", "McpSearch", {"queries": [{"query": "select:read_file,write_file"}]})),
        tool_response(("c", "McpCall", {"tool": "fs/read_file", "arguments": {"path": "hello.txt"}})),
        text_response("child done"), text_response("parent done"))
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("delegate")]
        assert events[-1].type == "turn.completed"
        frozen = session.mcp_loading_frozen
        assert frozen == {"fs": "search"}
        child_requests = [r for r in provider.requests if r is not provider.requests[0]][:3]
        names = {tool["name"] if isinstance(tool, dict) else tool.name for tool in child_requests[0].tools}
        assert {"McpSearch", "McpCall"} <= names
        assert not any(name.startswith("mcp__fs__") for name in names)
        assert msgspec.json.encode(child_requests[0].tools) == msgspec.json.encode(child_requests[-1].tools)
        results = [str(request.messages[-1]) for request in child_requests[1:]]
        assert any("fs/read_file" in text for text in results)
        assert any("hello-mcp" in text for text in results)
    finally:
        await runtime.aclose()


async def test_proxy_catalogue_follows_enabled_modes(tmp_path):
    from test_mcp_search_tools import setup

    def proxies(runtime, session):
        tools = runtime._selected_manifest(runtime.manifest, session).tools
        return {name for name in tools if name in ("McpSearch", "McpCall")}, any(n.startswith("mcp__") for n in tools)

    runtime, _ = setup(tmp_path / "all", mode="all")
    try:
        session = runtime.session("s")
        await HostFacade(runtime).handle(p.ContextInspect("s"))
        assert proxies(runtime, session) == (set(), True)
    finally:
        await runtime.aclose()
    runtime, _ = setup(tmp_path / "search")
    try:
        session = runtime.session("s")
        await HostFacade(runtime).handle(p.ContextInspect("s"))
        assert proxies(runtime, session) == ({"McpSearch", "McpCall"}, False)
        session.select_extension("mcp", "fs", False)
        assert proxies(runtime, session) == (set(), False)
    finally:
        await runtime.aclose()
    runtime, _ = setup(tmp_path / "none")
    try:
        await runtime.mcp.apply({})
        session = runtime.session("s")
        assert proxies(runtime, session) == (set(), False)
    finally:
        await runtime.aclose()
