from __future__ import annotations

from nexus.host import protocol as p
from nexus.host_support.context_preview import project_context_preview
from nexus.ui.cli.client import Client
from nexus.ui_support.context import render_context_details, render_context_summary


class _Transport:
    def __init__(self) -> None:
        self.command = None

    async def request(self, command):
        self.command = command
        return p.ContextInspectResult(
            session=command.session,
            system_text="preview",
            redacted_for_display=True,
        )

    def events(self, *_args, **_kwargs):
        raise AssertionError("context inspection does not subscribe")

    async def aclose(self) -> None:
        return None


async def test_client_context_inspection_uses_read_only_host_command():
    transport = _Transport()
    client = Client(transport)

    result = await client.inspect_context("new-session")

    assert transport.command == p.ContextInspect(session="new-session")
    assert isinstance(result, p.ContextInspectResult)
    assert result.mode == "next_turn_preview"
    assert result.actually_sent is False
    assert result.redacted_for_display is True


def test_context_details_distinguishes_prompt_tools_and_current_messages():
    result = p.ContextInspectResult(
        session="session",
        system_text="system instruction",
        tools=[{
            "name": "Read",
            "description": "Read a file",
            "input_schema": {"type": "object"},
        }],
        messages=[{
            "role": "user",
            "blocks": [
                {"type": "tool_use", "id": "call-7", "name": "Read", "input": {"path": "a.py"}},
                {"type": "tool_result", "tool_use_id": "call-7", "content": [{"type": "text", "text": "Earlier request"}]},
            ],
        }],
        history_included=True,
        request_context={"used_tokens": 80, "input_budget": 1000},
    )

    rendered = render_context_details(result, "80 / 1,000 tokens", session="session")

    assert "SYSTEM PROMPT · request.system" in rendered and "system instruction" in rendered
    assert "TOOLS · structured request.tools" in rendered and "Read a file" in rendered
    assert "MESSAGES · ordered request.messages · 1" in rendered
    assert "Earlier request" in rendered
    assert "id call-7" in rendered and "for call call-7" in rendered
    assert "Persisted conversation history is included." in rendered
    assert rendered.index("SYSTEM PROMPT · request.system") < rendered.index("TOOLS · structured request.tools") < rendered.index("MESSAGES · ordered request.messages")
    summary = render_context_summary(result)
    assert "request.tools" in summary and "request.messages" in summary


def test_context_summary_caps_each_part_at_ten_terminal_visual_lines():
    result = p.ContextInspectResult(
        session="session",
        system_text="x" * 120,
    )

    summary = render_context_summary(result, width=10)
    system = summary.plain.split("SYSTEM PROMPT · request.system\n", 1)[1].split("\nTOOLS ·", 1)[0]
    lines = system.splitlines()

    assert len(lines) == 10
    assert lines[-1] == "..."
    assert all(len(line) <= 10 for line in lines[:-1])


def test_context_parts_render_markdown_for_terminal_preview_and_modal():
    result = p.ContextInspectResult(
        session="session",
        system_text="# Instructions\n\nUse **careful** handling and `code`.\n- first\n- second",
        tools=[{"name": "Read", "description": "Read **files**", "input_schema": {"type": "object"}}],
        messages=[{"role": "assistant", "blocks": [{"type": "text", "text": "## Answer\n\nA *formatted* response."}]}],
    )

    summary = render_context_summary(result, width=50)
    details = render_context_details(result, "not reported", session="session")

    assert "Instructions" in summary.plain and "careful" in summary.plain
    assert "Instructions" in details and "careful" in details
    assert "```json" in details and "Input schema" in details


def test_context_inspection_redacts_and_bounds_request_content():
    secret = "sk-proj-0123456789abcdefghijklmnopqrstuvwxyz"
    result = project_context_preview({
        "messages": [{
            "role": "user",
            "blocks": [{"type": "text", "text": f"request {secret}"}],
        }],
        "tools": [{
            "name": "Read",
            "description": "d" * 20_000,
            "input_schema": {"type": "object"},
        }],
        "request_context": {"used_tokens": 12},
    })

    assert secret not in str(result)
    assert len(result["tools"][0]["description"]) <= 16_385
    assert result["messages"][0]["blocks"][0]["text"].startswith("request ")
