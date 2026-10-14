"""Native presentation of an Antigravity (agy) external worker, from the durable reduced view."""
from nexus.events import Event
from nexus.ui_support.external_worker import external_blocks, task_note
from nexus.view import fold

PROV = "Antigravity (agy) own tools"
MANUAL = "agy is waiting for approval; run `cd /w && agy` once and accept it"
USAGE = {"input_tokens": 1200, "output_tokens": 34, "cache_read_tokens": 900, "thinking_tokens": 56,
         "total_tokens": 1290}


def _ev(seq, type_, data):
    return Event(type=type_, data=data, seq=seq, ts=float(seq), session="s", turn="t1", id=f"e{seq:03d}")


def _external(status="completed", usage="unknown", attention=None, **finished):
    events = [
        _ev(1, "turn.started", {"limits": {}}),
        _ev(2, "external.started", {
            "provider": "antigravity", "model": "gemini-3", "session": "n-1", "workspace": "/w",
            "permission_mode": "accept-edits", "tools": None, "allowed_tools": [], "disallowed_tools": [],
            "restricted": False, "setting_sources": "", "mcp": "agy-inherited", "inherits_agy_settings": True,
            "read_only": False, "timeout_seconds": 60.0, "provenance": PROV,
            "authority": "agy ran its own tools (mode accept-edits) in /w; Nexus permissions did not mediate them"}),
        _ev(3, "external.tool", {"phase": "requested", "call_id": "k1", "tool": "run_command",
                                 "input": {"command": "ls"}, "provenance": PROV}),
        _ev(4, "external.finished", {
            "status": status, "provider": "antigravity", "usage": usage, "cost": "unknown",
            "agy_session": "conv-9", "agy_permission_mode": "plan", "agy_tools": ["view_file", "run_command"],
            "tool_calls": 1, "attention": attention, "error": attention, "transcript_path": None,
            "elapsed_seconds": 1.5, "clip_notes": ["answer clipped at 8000 characters"], **finished}),
    ]
    return fold(events).external


def _lines(block):
    return block["text"].splitlines()


def test_header_states_inherited_agy_settings_not_none():
    (head,), _ = external_blocks("a", _external())
    lines = _lines(head)
    assert head["title"] == "Antigravity (agy) worker · gemini-3 · independent tools"
    assert "Provider: Antigravity (agy) (external worker)" in lines
    assert "Permission mode: accept-edits" in lines
    assert "Tools: agy default set (inherited)" in lines
    assert "Settings sources: inherited from agy (user/global settings and plugins)" in lines
    assert "MCP: inherited from agy (servers and plugins)" in lines
    assert f"Tool rows: marked {PROV}; Nexus did not execute them" in lines
    assert "Nexus permissions did not mediate them" in head["text"]
    assert "Claude" not in head["text"] + head["title"]
    assert "none" not in head["text"]


def test_footer_reports_real_tokens_without_guessing_cost():
    _, (foot,) = external_blocks("a", _external(usage=USAGE))
    lines = _lines(foot)
    assert foot["status"] == "done" and foot["title"] == "Antigravity (agy) completed"
    assert "Usage: input 1,200, output 34, cache read 900, thinking 56, total 1,290 tokens" in lines
    assert "Cost: unknown (not estimated)" in lines
    assert "Effective permission mode: plan" in lines
    assert "Tools: view_file, run_command" in lines
    assert "Antigravity session: conv-9" in lines
    assert "Antigravity tool calls: 1" in lines
    assert "Clipped: answer clipped at 8000 characters" in lines
    assert "Claude" not in foot["text"] + foot["title"]


def test_missing_usage_is_unknown_not_zero():
    for usage in ("unknown", {}):
        _, (foot,) = external_blocks("a", _external(usage=usage))
        assert "Usage: unknown (agy reported no tokens to Nexus; this is not zero)" in _lines(foot)
        assert "0 tokens" not in foot["text"]


def test_needs_attention_is_exact_and_distinct():
    external = _external("needs_attention", attention=MANUAL)
    (head,), (foot,) = external_blocks("a", external)
    assert foot["status"] == "attention" and head["status"] == "attention"
    assert "NEEDS ATTENTION" in foot["title"]
    assert f"Needs attention: {MANUAL}" in _lines(foot)
    assert "resume" not in foot["text"]
    assert task_note(external) == "Needs attention: " + MANUAL
    assert task_note(_external("needs_attention")) == "Needs attention: Antigravity (agy) is waiting for you"


def test_unsafe_text_is_escaped_single_line_and_clipped_with_announcement():
    external = _external("failed", attention="x\nInjected: row\x1b[31m" + "y" * 5000)
    _, (foot,) = external_blocks("a", external)
    needs = [line for line in _lines(foot) if line.startswith("Needs attention: ")]
    assert len(needs) == 1 and "\x1b" not in foot["text"]
    assert "[clipped, " in needs[0]
    assert all(line.split(": ")[0] in {"Status", "Needs attention", "Stop event", "Antigravity tool calls",
                                       "Antigravity session", "Effective permission mode", "Tools", "Usage",
                                       "Cost", "Elapsed", "Transcript", "Clipped"}
               for line in _lines(foot))


def test_legacy_payload_without_provider_stays_claude():
    (head,), (foot,) = external_blocks("a", {"finished": True, "status": "completed", "model": "m"})
    assert head["title"] == "Claude Code worker · m · independent tools"
    assert "Provider: Claude Code (external worker)" in _lines(head)
    assert foot["title"] == "Claude Code completed"
    assert "Usage: unknown (Claude Code reports none to Nexus; this is not zero)" in _lines(foot)
    assert task_note({"finished": True, "status": "needs_attention"}) == "Needs attention: Claude Code is waiting for you"


def test_reduced_view_projection_is_replay_stable():
    first = external_blocks("a", _external(usage=USAGE))
    assert first == external_blocks("a", _external(usage=USAGE))
