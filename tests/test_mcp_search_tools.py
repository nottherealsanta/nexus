"""Real stdio search/call integration and fixed request declarations."""

import msgspec

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from test_mcp_integration import (make_server_root, control_path, write_control,
    write_mcp_config, fs_definition, make_runtime, tool_result_text)


def setup(tmp_path, *scripts, mode=None):
    root = make_server_root(tmp_path)
    control = control_path(tmp_path)
    write_control(control)
    definition = fs_definition(root, control)
    definition.pop("tool_loading", None)
    if mode:
        definition["tool_loading"] = mode
    write_mcp_config(tmp_path, {"fs": definition})
    provider = ScriptedProvider(*scripts)
    return make_runtime(tmp_path, provider), provider


async def test_search_call_and_fixed_tools(tmp_path):
    runtime, provider = setup(tmp_path,
        tool_response(("s", "McpSearch", {"queries": [{"server": "fs", "query": "read file"}, {"server": "missing", "query": "x"}]})),
        tool_response(("c", "McpCall", {"tool": "fs/read_file", "arguments": {"path": "hello.txt"}})), text_response("done"))
    try:
        session = runtime.session("s")
        events = [event async for event in session.send("Read hello.txt")]
        assert events[-1].type == "turn.completed"
        texts = tool_result_text(session)
        assert "fs/read_file" in texts and "Unknown MCP server" in texts
        assert "hello-mcp" in texts and "<untrusted-mcp-data>" in texts
        schemas = [msgspec.json.encode(request.tools) for request in provider.requests]
        assert len(set(schemas)) == 1
        assert {t.name for t in provider.requests[0].tools} >= {"McpSearch", "McpCall"}
        assert not any(t.name.startswith("mcp__") for t in provider.requests[0].tools)
        started = next(e for e in events if e.type == "tool.started" and e.data["call_id"] == "c")
        assert started.data["tool"] == "McpCall" and started.data["target"] == "mcp__fs__read_file"
        assert session.mcp_loading_frozen == {"fs": "search"}
    finally:
        await runtime.aclose()


async def test_selection_overrides_config_and_locks(tmp_path):
    runtime, provider = setup(tmp_path, text_response("done"), mode="all")
    try:
        facade = HostFacade(runtime)
        facade.open_session("s")
        preview = await facade.handle(p.ContextInspect("s"))
        assert preview.mcp_servers[0]["tool_loading"] == "all"
        selected = await facade.handle(p.ContextMcpLoadingSelect("s", "fs", "search"))
        assert selected.mcp_servers[0]["tool_loading_source"] == "session"
        session = runtime.session("s")
        [e async for e in session.send("hi")]
        rejected = await facade.handle(p.ContextMcpLoadingSelect("s", "fs", "all"))
        assert isinstance(rejected, p.ErrorResult) and "prompt cache" in rejected.message
        # Config editing cannot move an existing session's tool prefix.
        await runtime.mcp.apply({"fs": {"command": "unused", "tool_loading": "all"}})
        assert runtime._selected_manifest(runtime.manifest, session).mcp["fs"].tool_loading == "search"
        assert session.mcp_loading_choices == {"fs": "search"}
        reopened = runtime.sessions.open("s", create=False)
        assert reopened.mcp_loading_frozen == {"fs": "search"}
    finally:
        await runtime.aclose()


async def test_all_mode_direct_and_schema_correction(tmp_path):
    runtime, provider = setup(tmp_path,
        tool_response(("c", "McpCall", {"tool": "fs/read_file", "arguments": {}})), text_response("done"))
    try:
        session = runtime.session("s")
        [e async for e in session.send("hi")]
        assert "Input schema" in tool_result_text(session)
    finally:
        await runtime.aclose()


async def test_proxy_target_restrictions_search_and_call(tmp_path):
    from nexus.tools.manager import ToolManager
    from nexus.tools.permissions import Decision
    from nexus.tools.spec import ToolCall

    runtime, _ = setup(tmp_path, text_response("done"))
    try:
        facade = HostFacade(runtime)
        facade.open_session("s")
        await facade.handle(p.ContextInspect("s"))
        selected = runtime._selected_manifest(runtime.manifest, runtime.session("s"))
        tools = [selected.tools[name] for name in ("McpSearch", "McpCall")]
        manager = ToolManager(runtime._load_config(), workspace=tmp_path, tools=tools,
            restrict=("McpSearch", "McpCall", "mcp__fs__read_file"))
        calls = manager.prepare([ToolCall("s", "McpSearch", {"queries": [{"query": "select:read_file,write_file"}]})])
        result = await manager.dispatch(calls.with_decisions({"s": Decision.ALLOW_ONCE}))
        text = result[0].content[0].text
        assert "fs/read_file" in text and "not found: write_file" in text
        entry = manager.prepare([ToolCall("c", "McpCall", {"tool": "fs/write_file", "arguments": {"path": "x", "text": "bad"}})]).entries[0]
        assert "restrictions" in entry.error.content[0].text
        await runtime.mcp.apply({})
        entry = manager.prepare([ToolCall("c", "McpCall", {"tool": "mcp__fs__read_file", "arguments": {"path": "hello.txt"}})]).entries[0]
        assert "not found; search again" in entry.error.content[0].text
        await manager.aclose()
    finally:
        await runtime.aclose()


async def test_agent_wildcard_keeps_proxies_and_intersection(tmp_path):
    from nexus.agents.model import parse_frontmatter

    runtime, _ = setup(tmp_path, text_response("done"))
    try:
        facade = HostFacade(runtime)
        facade.open_session("s")
        await facade.handle(p.ContextInspect("s"))
        role = parse_frontmatter('---\nname: limited\ndescription: Limited\ntools: [mcp__fs__*]\n---\nPrompt')
        # Parsing and matching the existing MCP permission-style wildcard is supported.
        assert "mcp__fs__*" in role.tools
        from dataclasses import replace
        role = replace(runtime.agents.require("build"), name=role.name, tools=role.tools, bundles=())
        available = ("McpSearch", "McpCall", "mcp__fs__read_file", "mcp__other__run")
        selection = runtime.agents.select_tools(role, available=available)
        assert selection.selected == {"McpSearch", "McpCall", "mcp__fs__read_file"}
    finally:
        await runtime.aclose()


async def test_lazy_search_disabled_server_and_all_refusal(tmp_path):
    from nexus.config import Config
    from nexus.mcp.bridge import build_search_tools
    from nexus.tools.manager import ToolManager
    from nexus.tools.permissions import Decision
    from nexus.tools.spec import ToolCall
    from test_mcp_manager import make_manager, raw_definition

    mcp, factory = make_manager({"s": raw_definition("s"), "off": {**raw_definition("off"), "enabled": False}})
    try:
        tools = build_search_tools(mcp, {"s": "search", "off": "search"})
        manager = ToolManager(Config(), workspace=tmp_path, tools=tools)
        assert factory.calls == 0
        batch = manager.prepare([ToolCall("s", "McpSearch", {"queries": [
            {"server": "off", "query": "x"}, {"server": "s", "query": "echo"}]})])
        result = await manager.dispatch(batch.with_decisions({"s": Decision.ALLOW_ONCE}))
        assert "switched off" in result[0].content[0].text
        assert "s/echo" in result[0].content[0].text
        assert factory.calls == 1
        batch = manager.prepare([ToolCall("c", "McpCall", {"tool": "s/echo", "arguments": {}})])
        assert batch.entries[0].target.spec.name == "mcp__s__echo"
        all_tools = build_search_tools(mcp, {"s": "all"})
        manager2 = ToolManager(Config(), workspace=tmp_path, tools=all_tools)
        refused = manager2.prepare([ToolCall("c", "McpCall", {"tool": "s/echo", "arguments": {}})]).entries[0]
        assert "loaded directly" in refused.error.content[0].text
        await manager.aclose()
        await manager2.aclose()
    finally:
        await mcp.aclose()
