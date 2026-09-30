"""Tool call details render every parameter and output as labelled rows."""

from nexus.ui_support.tool_details import sections_to_text, tool_detail_sections
from nexus.view import ToolCallView


def _tool(**kw) -> ToolCallView:
    return ToolCallView(call_id="c1", name="Edit", status="completed", **kw)


def test_parameters_flatten_nested_values_and_miss_nothing():
    tool = _tool(input={
        "path": "a.py", "edits": [{"old": "x\ny", "new": "z"}], "flags": {}, "n": 3, "on": False, "none": None,
    })
    text = sections_to_text(tool_detail_sections(tool))
    for needle in ("path: a.py", "edits[0]\n    old:\n      x\n      y\n    new: z", "flags: {}", "n: 3", "on: false", "none: null"):
        assert needle in text, needle
    assert '"' not in text.split("PARAMETERS")[1]


def test_result_blocks_error_metrics_progress_and_overview_are_all_present():
    tool = _tool(
        input={"q": 1}, result=[{"type": "text", "text": "one\ntwo"}, {"type": "image", "mime": "image/png"}],
        error="boom", context_note="clipped", display="Read 2 lines", progress=["a", "b"],
        metrics={"lines": 2}, duration_ms=12, bundle="fs", code="print(1)",
    )
    titles = [s.title for s in tool_detail_sections(tool)]
    assert titles == ["Overview", "Parameters", "Code", "Progress", "Summary", "Result", "Error", "Context", "Metrics"]
    text = sections_to_text(tool_detail_sections(tool))
    for needle in ("Duration: 12 ms", "Bundle: fs", "one\n", "#2 image.mime: image/png", "boom", "lines: 2", "#2: b"):
        assert needle in text, needle


def test_secrets_are_redacted_and_long_values_report_the_clip():
    tool = _tool(input={"token": "password=hunter2", "big": "x" * 30_000})
    text = sections_to_text(tool_detail_sections(tool))
    assert "hunter2" not in text
    assert "[clipped 10000 more characters]" in text


def test_list_of_objects_render_as_headed_groups_and_todos_get_a_checklist():
    todos = [{"id": "a", "content": "Do a", "status": "in_progress", "priority": "high"},
             {"id": "b", "content": "Do b", "status": "pending"}]
    tool = ToolCallView(call_id="t", name="TodoWrite", status="completed",
                        input={"todos": todos}, metrics={"counts": {"pending": 1}, "todos": todos})
    text = sections_to_text(tool_detail_sections(tool))
    assert "  todos[0]\n    id: a\n    content: Do a" in text
    assert "[~] a: Do a (high)" in text and "[ ] b: Do b" in text
    assert "counts.pending: 1" in text and "<dict>" not in text


def test_short_scalar_lists_are_one_row_and_non_text_blocks_skip_their_type():
    tool = _tool(input={"glob": ["*.py", "*.js"]}, result=[{"type": "image", "mime": "image/png"}])
    text = sections_to_text(tool_detail_sections(tool))
    assert "glob: *.py, *.js" in text and "image.type" not in text and "image.mime: image/png" in text
