"""Native shared detail rows expose code telemetry without protocol dumps."""

from nexus.ui_support.tool_details import sections_to_text, tool_detail_sections
from nexus.view import ToolCallView


def _fixture_tool(index=0):
    import json
    from pathlib import Path
    from nexus.events import Event
    from nexus.view import fold
    path = Path(__file__).parent / "fixtures" / "view" / "code_mode.events.json"
    view = fold([Event(**e) for e in json.loads(path.read_text())])
    return view.turns[index].tools[0]


def test_code_details_are_labelled_sections_with_every_nested_call():
    sections = tool_detail_sections(_fixture_tool())
    titles = [s.title for s in sections]
    assert titles[0] == "code · audit TODOs"
    for title in ("Program status", "Targets", "Code", "Nested call 1", "Nested call 5", "Result", "Store writes"):
        assert title in titles
    text = sections_to_text(sections)
    assert "1  files = grep(" in text  # line-numbered program
    assert "timeout_s: 30" in text
    assert "✓ 1. grep" in text and "✗ 2. read" in text and "⊘ 3. write" in text
    assert "Matched rule: Write(/etc/**)" in text
    assert "Arguments.pattern: TODO" in text and "Bytes: 22" in text
    assert "Model: claude-x" in text and "Input tokens: 120" in text and "Output tokens: 9" in text
    assert "Prompt excerpt" in text and "n: 3" in text


def test_cancelled_program_shows_cancelled_row_and_diagnostic():
    text = sections_to_text(tool_detail_sections(_fixture_tool(1)))
    assert "– 1. read" in text and "Kind: cancelled" in text and "Line: 2" in text


def test_nested_clipping_is_announced():
    tool = ToolCallView(name="code", call_id="c", input={"program": "x"}, code_mode={"purpose": "p"},
                        nested_calls=[{"nested_id": "c.1", "index": 1, "tool": "read", "status": "ok",
                                       "args": {"path": "a"}, "result": "abc", "bytes": 9000, "clipped": True}])
    assert "Result clipped: shown 3 characters of 9000 bytes" in sections_to_text(tool_detail_sections(tool))


def test_legacy_code_without_nested_events_still_renders():
    tool = ToolCallView(name="code", input={"code": "print(1)"}, result=[])
    text = sections_to_text(tool_detail_sections(tool))
    assert "print(1)" in text
    assert "Nested call" not in text


def test_native_signal_has_numbered_program_glyph_rows_and_outcome():
    from nexus.ui_support.tool_signal import tool_signal
    signal = tool_signal(_fixture_tool())
    text = "\n".join(row.text for row in signal.rows)
    for expected in ("1  files = grep(", "✓ 1. grep", "✗ 2. read", "⊘ 3. write", "rule Write(/etc/**)",
                     "claude-x 120→9 tok", "value: 3", "store: n, note"):
        assert expected in text
    assert any(row.kind == "error" and "⊘ 3." in row.text for row in signal.rows)
