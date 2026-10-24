"""The debug-only manager MCP server (nexus/devtools/manager_debug_mcp.py).

All offline: a fake daemon client stands in for the host protocol, so no running session
or daemon is touched.
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest

from nexus.client.protocol import FacadeError
from nexus.devtools import manager_debug_mcp as dbg
from nexus.devtools.debug_mcp_port import DEBUG_ID_PREFIX, DEBUG_SEND_PREFIX, DaemonManagerPort
from nexus.host.transports import DaemonUnavailable
from nexus.session.manager import SessionSummary
from nexus.tools.bundles import MANAGER_TOOL_NAMES
from nexus.host import protocol as p

NOW = 1_000_000.0


def summary(sid, *, state="idle", title="", last_seq=1, kind="root", user_at=NOW - 60, **kw):
    return SessionSummary(
        id=sid, title=title or sid, state=state, last_activity=user_at, last_seq=last_seq,
        last_user_at=user_at, created_at=user_at, kind=kind, **kw,
    )


def finished_view(text="all done", phase="completed"):
    return {"turns": [{"phase": phase, "messages": [
        {"role": "user", "blocks": [{"kind": "text", "text": "do it"}]},
        {"role": "assistant", "blocks": [{"kind": "text", "text": text}]},
    ]}]}


class FakeClient:
    def __init__(self, sessions, views=None):
        self.sessions = list(sessions)
        self.views = views or {}
        self.calls: list[tuple] = []
        self.closed = False

    async def list_sessions(self):
        return list(self.sessions)

    async def state(self, session, from_seq=0):
        self.calls.append(("state", session))
        return self.views.get(session, {"turns": []}), 1

    async def manager_state(self):
        return p.ManagerStateResult(max_live_managed=2)

    async def select_agent(self, session, name):
        self.calls.append(("select_agent", session, name))
        if name != "build":
            raise FacadeError("ValueError", "unknown agent")

    async def enqueue(self, session, content, *, mode="steer", **kw):
        self.calls.append(("enqueue", session, content, mode))
        return "q1", "t1"

    async def promote_session(self, session, *, reason="", continuation="", name=""):
        self.calls.append(("promote_session", session, name))
        if name == "taken":
            raise FacadeError("ValueError", "Branch nexus/wt/taken already exists")

    async def rename_session(self, session_id, title):
        self.calls.append(("rename_session", session_id, title))

    async def cancel(self, session, *, reason="", drop_queue=True):
        self.calls.append(("cancel", session, reason, drop_queue))
        return True, 0

    async def aclose(self):
        self.closed = True


def make(sessions, views=None, clock=lambda: NOW):
    client = FakeClient(sessions, views)

    async def connect():
        return client

    port = DaemonManagerPort(connect, clock=clock, poll_interval=0.01)
    return dbg.DebugMcpServer(port), client


def text_of(reply):
    return reply["content"][0]["text"]


def mutations(client):
    return [call for call in client.calls if call[0] in {"enqueue", "cancel", "select_agent", "rename_session", "promote_session"}]


def test_catalog_is_the_managers_without_user_message_provenance():
    tools = dbg.tool_descriptors()
    assert tuple(tool["name"] for tool in tools) == MANAGER_TOOL_NAMES
    for tool in tools:
        assert tool["description"].startswith("[DEBUG ONLY]")
        assert "origin_message_id" not in tool["inputSchema"].get("properties", {})
        assert "origin_message_id" not in tool["inputSchema"].get("required", [])
        assert tool["inputSchema"]["additionalProperties"] is False
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["sessions_find"]["annotations"]["readOnlyHint"] is True
    assert by_name["session_launch"]["annotations"]["readOnlyHint"] is False


async def test_session_show_is_explicitly_unsupported_and_touches_nothing():
    server, client = make([summary("t")])
    reply = await server.call_tool("session_show", {"id": "t"})
    assert reply["isError"] and "not supported" in text_of(reply)
    assert client.calls == [] and mutations(client) == []
    assert (await server.call_tool("session_show", {"id": "t", "origin_message_id": "m1"}))["isError"]


def test_has_no_env_gate():
    assert not hasattr(dbg, "DEBUG_ENV") and not hasattr(dbg, "debug_enabled")


def test_list_tools_flag_prints_names_without_running(capsys):
    assert dbg.main(["--list-tools"]) == 0
    assert capsys.readouterr().out.split() == list(MANAGER_TOOL_NAMES)


async def test_find_status_result_read_the_daemon_only():
    server, client = make(
        [summary("a", title="flaky sync", state="running"), summary("b", title="docs"),
         summary("old", user_at=NOW - 10 * 3600), summary("nexus-manager", kind="manager"),
         summary("child", kind="subagent")],
        {"b": finished_view("report api_key=sk-secret1234567 end")},
    )
    found = json.loads(text_of(await server.call_tool("sessions_find", {"scope": "recent"})))
    assert [row["id"] for row in found["sessions"]] == ["a", "b"]
    live = json.loads(text_of(await server.call_tool("sessions_find", {"scope": "live", "query": "FLAKY"})))
    assert [row["id"] for row in live["sessions"]] == ["a"]
    status = json.loads(text_of(await server.call_tool("session_status", {"ids": ["a", "b"]})))
    assert [(s["id"], s["outcome"]) for s in status["sessions"]] == [("a", None), ("b", "completed")]
    result = text_of(await server.call_tool("session_result", {"id": "b"}))
    assert result.startswith('<session_result id="b" outcome="completed"')
    assert "sk-secret1234567" not in result
    hidden = await server.call_tool("session_status", {"ids": ["nexus-manager"]})
    assert hidden["isError"] and "not visible" in text_of(hidden)
    assert not mutations(client)


async def test_result_needs_a_finished_turn():
    server, _ = make([summary("run", state="running"), summary("fresh")])
    for sid in ("run", "fresh"):
        reply = await server.call_tool("session_result", {"id": sid})
        assert reply["isError"] and "no finished turn" in text_of(reply)


async def test_launch_starts_a_dbg_root_through_enqueue():
    server, client = make([])
    reply = await server.call_tool("session_launch", {"instruction": "fix the test", "agent": "build"})
    assert not reply["isError"], text_of(reply)
    sid = json.loads(text_of(reply))["id"]
    assert sid.startswith(DEBUG_ID_PREFIX)
    assert mutations(client) == [("select_agent", sid, "build"), ("enqueue", sid, "fix the test", "queue")]
    bad = await server.call_tool("session_launch", {"instruction": "x", "agent": "nope"})
    assert bad["isError"] and "not a usable root role" in text_of(bad)


async def test_launch_worktree_selects_the_named_worktree_before_the_enqueue():
    server, client = make([])
    schema = next(t for t in dbg.tool_descriptors() if t["name"] == "session_launch")
    assert schema["inputSchema"]["properties"]["worktree"]["type"] == "boolean"
    assert schema["description"].startswith("[DEBUG ONLY]") and "worktree" in schema["description"]
    no_title = await server.call_tool("session_launch", {"instruction": "go", "worktree": True})
    assert no_title["isError"] and mutations(client) == []
    bad = await server.call_tool("session_launch", {"instruction": "go", "worktree": True, "title": "a b"})
    assert bad["isError"] and mutations(client) == []
    reply = json.loads(text_of(await server.call_tool(
        "session_launch", {"instruction": "go", "worktree": True, "title": "fix-sync"})))
    assert reply["id"].startswith(DEBUG_ID_PREFIX) and reply["title"] == "fix-sync"
    assert reply["worktree_branch"] == "nexus/wt/fix-sync"
    assert mutations(client) == [
        ("promote_session", reply["id"], "fix-sync"), ("enqueue", reply["id"], "go", "queue"),
    ]


async def test_launch_worktree_refusal_starts_nothing():
    server, client = make([])
    refused = await server.call_tool("session_launch", {"instruction": "go", "worktree": True, "title": "taken"})
    assert refused["isError"] and "already exists" in text_of(refused)
    assert [c[0] for c in mutations(client)] == ["promote_session"]  # no enqueue


async def test_launch_title_is_applied_as_a_rename():
    server, client = make([])
    reply = json.loads(text_of(await server.call_tool(
        "session_launch", {"instruction": "go", "title": "Flaky sync"})))
    assert reply["title"] == "Flaky sync"
    assert mutations(client) == [
        ("enqueue", reply["id"], "go", "queue"), ("rename_session", reply["id"], "Flaky sync"),
    ]

    async def broken(session_id, title):
        raise FacadeError("ValueError", "nope")

    client.rename_session = broken
    again = await server.call_tool("session_launch", {"instruction": "go", "title": "x"})
    assert not again["isError"] and json.loads(text_of(again))["title"] == ""


async def test_launch_is_capped_and_rate_limited():
    server, client = make([])
    sessions = client.sessions
    first = json.loads(text_of(await server.call_tool("session_launch", {"instruction": "one"})))["id"]
    second = json.loads(text_of(await server.call_tool("session_launch", {"instruction": "two"})))["id"]
    sessions += [summary(first, state="running"), summary(second, state="running")]
    capped = await server.call_tool("session_launch", {"instruction": "three"})
    assert capped["isError"] and "limit is 2" in text_of(capped)
    sessions.clear()
    await server.call_tool("session_launch", {"instruction": "three"})
    limited = await server.call_tool("session_launch", {"instruction": "four"})
    assert limited["isError"] and "per 60 seconds" in text_of(limited)
    assert sum(1 for call in client.calls if call[0] == "enqueue") == 3


async def test_origin_argument_is_not_accepted():
    server, client = make([])
    reply = await server.call_tool("session_launch", {"instruction": "x", "origin_message_id": "m1"})
    assert reply["isError"] and "origin_message_id" in text_of(reply)
    assert not mutations(client)


async def test_cancel_only_reaches_sessions_this_server_launched():
    server, client = make([summary("other", state="running")])
    refused = await server.call_tool("session_cancel", {"id": "other"})
    assert refused["isError"] and "not launched" in text_of(refused)
    sid = json.loads(text_of(await server.call_tool("session_launch", {"instruction": "go"})))["id"]
    client.sessions.append(summary(sid, state="running"))
    done = json.loads(text_of(await server.call_tool("session_cancel", {"id": sid, "reason": "key sk-secret1234567"})))
    assert done == {"id": sid, "cancelled": True}
    cancel = [call for call in client.calls if call[0] == "cancel"]
    assert cancel == [("cancel", sid, cancel[0][2], True)] and "sk-secret1234567" not in cancel[0][2]


async def test_send_is_labelled_bounded_and_rate_limited():
    server, client = make([summary("t", state="running"), summary("stale", user_at=NOW - 3 * 3600)])
    sent = json.loads(text_of(await server.call_tool("session_send", {"id": "t", "message": "try again"})))
    assert sent["state"] == "queued"
    assert mutations(client) == [("enqueue", "t", DEBUG_SEND_PREFIX + "try again", "queue")]
    stale = await server.call_tool("session_send", {"id": "stale", "message": "hi"})
    assert stale["isError"] and "idle for over 2 hours" in text_of(stale)
    big = await server.call_tool("session_send", {"id": "t", "message": "x" * (16 * 1024 + 1)})
    assert big["isError"]
    for _ in range(5):
        await server.call_tool("session_send", {"id": "t", "message": "again"})
    limited = await server.call_tool("session_send", {"id": "t", "message": "again"})
    assert limited["isError"] and "per 60 seconds" in text_of(limited)


async def test_wait_returns_finished_and_running():
    server, _ = make(
        [summary("done"), summary("busy", state="running")], {"done": finished_view()},
    )
    reply = json.loads(text_of(await server.call_tool(
        "session_wait", {"ids": ["done", "busy"], "until": "any", "max_wait_seconds": 1})))
    assert reply["completed"] == ["done"] and reply["running"] == ["busy"] and reply["interrupted"] is False
    timed = json.loads(text_of(await server.call_tool(
        "session_wait", {"ids": ["busy"], "max_wait_seconds": 1})))
    assert timed["completed"] == [] and timed["waited_seconds"] >= 1


async def test_unreachable_daemon_is_an_error_reply_not_a_crash():
    async def connect():
        raise DaemonUnavailable("no socket")

    server = dbg.DebugMcpServer(DaemonManagerPort(connect))
    reply = await server.call_tool("sessions_find", {})
    assert reply["isError"] and "no Nexus daemon is reachable" in text_of(reply)
    assert (await server.call_tool("nope", {}))["isError"]


async def test_stdio_protocol(monkeypatch):
    server, _ = make([summary("a", state="running")])
    out = io.StringIO()
    monkeypatch.setattr(server, "_out", out)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sessions_find", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    ]

    async def feed():
        yield "not json\n"
        for item in lines:
            yield json.dumps(item) + "\n"
        for _ in range(20):  # let the in-flight requests finish before the stream ends
            await asyncio.sleep(0.01)

    await server.serve(feed())
    replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert "DEBUG ONLY" in replies[1]["result"]["instructions"]
    assert [t["name"] for t in replies[2]["result"]["tools"]] == list(MANAGER_TOOL_NAMES)
    assert json.loads(replies[3]["result"]["content"][0]["text"])["sessions"][0]["id"] == "a"
    assert replies[4]["result"] == {}
    assert replies[5]["error"]["code"] == -32601


async def test_bad_request_ids_are_rejected_not_fatal(monkeypatch):
    server, _ = make([])
    out = io.StringIO()
    monkeypatch.setattr(server, "_out", out)

    async def feed():
        yield json.dumps({"jsonrpc": "2.0", "id": [1], "method": "ping"}) + "\n"
        yield json.dumps({"jsonrpc": "2.0", "id": True, "method": "ping"}) + "\n"
        yield json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"
        for _ in range(10):
            await asyncio.sleep(0.01)

    await server.serve(feed())
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("error", {}).get("code") for r in replies[:2]] == [-32600, -32600]
    assert {"jsonrpc": "2.0", "id": 7, "result": {}} in replies


def test_stdio_subprocess_handshake_and_clean_exit(tmp_path):
    """The real entry point over pipes: MCP handshake, catalog, an error reply with no daemon."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k != "NEXUS_DEBUG_MCP"}  # starts with no env flag
    proc = subprocess.Popen(
        [sys.executable, "-m", "nexus.devtools.manager_debug_mcp", "--workspace", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=root, env=env,
    )

    def rpc(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["capabilities"] == {"tools": {}}
        newer = rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                     "params": {"protocolVersion": "2099-01-01"}})
        assert newer["result"]["protocolVersion"] == dbg.SUPPORTED_PROTOCOLS[0]
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        listed = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert [t["name"] for t in listed["result"]["tools"]] == list(MANAGER_TOOL_NAMES)
        called = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "sessions_find", "arguments": {}}})
        assert called["result"]["isError"] is True  # no daemon in tmp_path
        assert "no Nexus daemon is reachable" in called["result"]["content"][0]["text"]
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.stderr.close()
    assert proc.returncode == 0


def test_registered_in_the_project_config_enabled_without_env_gate():
    from pathlib import Path

    config = json.loads((Path(__file__).resolve().parent.parent / ".agents" / "mcp.json").read_text())
    entry = config["mcpServers"]["nexus-manager-debug"]
    assert entry.get("enabled", True) is True
    assert "env" not in entry
    assert "nexus.devtools.manager_debug_mcp" in entry["args"]
    assert "browser-use" in config["mcpServers"]  # existing settings preserved


@pytest.mark.parametrize("name", ["sessions_find", "session_status"])
def test_read_tools_are_marked_read_only(name):
    tool = next(t for t in dbg.tool_descriptors() if t["name"] == name)
    assert tool["annotations"]["readOnlyHint"] is True
