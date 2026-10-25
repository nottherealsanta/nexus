from types import SimpleNamespace

from nexus.host_support.agent_context import project_agent_context


def test_child_context_preserves_reported_root_fields():
    snapshot = {
        "system": "child system",
        "prompt": "child task",
        "tools": [{"name": "read"}],
        "included_parts": [{"name": "agents", "text": "child AGENTS.md"}, {"name": "skills", "text": "skill instructions"}],
        "skills_index": [{"name": "child-skill"}],
        "mcp_index": "child-mcp",
        "request_context": {"custom": "child value"},
        "params": {"temperature": 0.3},
    }
    result = project_agent_context(
        SimpleNamespace(session="child", type="worker", model="model", tier=None),
        SimpleNamespace(agent_request=lambda session: snapshot),
    )
    assert result["system_text"] == "child system"
    for key in ("included_parts", "request_context", "params", "tools"):
        assert result[key] == snapshot[key]
    assert result["skills_index"][0]["name"] == "child-skill"
    assert result["mcp_index"] == "child-mcp"
    assert result["messages"][0]["blocks"][0]["text"] == "child task"


def test_capture_records_only_assembled_parts_and_safe_request():
    from nexus.core.loop import _request_snapshot
    from nexus.model.message import Image, Message, Text
    from nexus.model.request import ModelRequest, SamplingParams

    request = ModelRequest(
        system="child AGENTS instructions\nchild-skill: useful\nchild MCP",
        messages=[Message(role="user", content=[Text("task"), Image(media_type="image/png", data=b"private bytes")])],
        params=SamplingParams(temperature=0.3),
        metadata={"included_parts": {"agents_md": "child AGENTS instructions", "skills_index": "child-skill: useful", "mcp_index": "child MCP"},
                  "context": {"used_tokens": 42}, "unrelated_secret": "not captured"},
    )
    snapshot = _request_snapshot(request)
    assert snapshot["included_parts"][0] == {"name": "agents_md", "text": "child AGENTS instructions"}
    assert snapshot["skills_index"][0]["name"] == "child-skill"
    assert snapshot["mcp_index"] == "child MCP"
    assert snapshot["request_context"] == {"used_tokens": 42}
    assert snapshot["params"]["temperature"] == 0.3
    assert snapshot["messages"][0]["blocks"][0] == {"type": "text", "text": "task"}
    assert "private bytes" not in str(snapshot)
    assert "unrelated_secret" not in snapshot


def test_capture_bounds_and_redacts_text():
    from nexus.core.loop import _request_snapshot
    from nexus.model.message import Message, Text
    from nexus.model.request import ModelRequest

    snapshot = _request_snapshot(ModelRequest(
        system="x" * 300_000,
        messages=[Message(role="user", content=[Text("api_key=sk-live-secret-value")])],
    ))
    assert len(snapshot["system"]) <= 200_001
    assert "sk-live-secret-value" not in str(snapshot)


def test_capture_excludes_opaque_and_harness_fields_including_nested_attachments():
    from nexus.core.loop import _request_snapshot
    from nexus.model.message import (
        DUPLICATE_TOOL_CALL_KEY, Document, Image, Message, MessageMeta,
        Text, Thinking, ToolResult, ToolUse,
    )
    from nexus.model.request import ModelRequest

    image = Image(media_type="image/png", data=b"private-image", url="https://private-signed-url")
    snapshot = _request_snapshot(ModelRequest(messages=[Message(
        role="assistant",
        meta=MessageMeta(provider="private-meta"),
        content=[
            Thinking("visible reasoning", signature="private-signature"),
            ToolUse("call", "Read", {"path": "file", DUPLICATE_TOOL_CALL_KEY: "private-marker"}),
            ToolResult("call", [Text("visible result"), image], context_note="private-note",
                       display="private-display", metrics={"private-metrics": 1}, diff={"private-diff": 1}),
            image,
            Document(media_type="application/pdf", data=b"private-document", title="private-title"),
        ],
    )]))
    assert "private-" not in str(snapshot)
    assert DUPLICATE_TOOL_CALL_KEY not in str(snapshot)
    blocks = snapshot["messages"][0]["blocks"]
    assert blocks[0] == {"type": "thinking", "text": "visible reasoning"}
    assert blocks[1]["input"] == {"path": "file"}
    assert blocks[2] == {
        "type": "tool_result", "tool_use_id": "call", "is_error": False,
        "content": [{"type": "text", "text": "visible result"},
                    {"type": "attachment", "text": "[attachment omitted]"}],
    }
