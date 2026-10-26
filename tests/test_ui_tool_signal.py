"""The relevant-only tool view: what the tool did and produced, never metadata (GROK_BUILD_TUI_PLAN §5.2)."""
import pytest

from nexus.ui_support.tool_details import SECTION_ROWS, VALUE_LIMIT, tool_detail_sections
from nexus.ui_support.tool_signal import (
    MAX_SIGNAL_CHARS, SignalRow, clip_cells, closed_summary, strip_job_header, strip_workspace, tool_signal,
)
from nexus.view.model import ToolCallView

METADATA = ("Call ID", "Requested", "Started", "Finished", "Status", "Bundle", "Model iteration", "Executed",
            "Provenance", "SECRET-CALL-ID", "SECRET-BUNDLE")


def _tool(name, args=None, text=None, **kwargs):
    kwargs.setdefault("status", "completed")
    result = [{"type": "text", "text": text}] if text is not None else []
    return ToolCallView(call_id="SECRET-CALL-ID", name=name, input=args or {}, result=result, **kwargs)


def test_read_shows_numbered_excerpt_and_line_span():
    view = tool_signal(_tool("read", {"path": "a.py"}, "one\ntwo\nthree"))
    assert view.summary == "Read a.py (lines 1–3)"
    assert [r.kind for r in view.rows] == ["out"] * 3
    assert [r.text for r in view.rows] == ["1  one", "2  two", "3  three"]


def test_shell_preview_bounds_input_and_uses_running_samples():
    from nexus.ui_support.tool_signal import shell_preview
    preview = shell_preview(_tool("shell", {"command": "one\ntwo\nthree"}, status="running",
                                  progress=["first", "latest"]))
    assert preview["input"] == ["$ one", "  two"]
    assert preview["input_lines"] == 3 and preview["input_clipped"]
    assert preview["output_tail"] == ["first", "latest"]
    preview = shell_preview(_tool("bash", {"command": "echo ok"},
                                  "job_id: x\nstatus: completed\nexit_code: 0\nstdout:\nok"))
    assert preview["output_tail"] == ["ok"]
    assert shell_preview(_tool("read")) is None


def test_read_honours_offset_for_numbering():
    view = tool_signal(_tool("read", {"path": "a.py", "offset": 10}, "x\ny"))
    assert view.summary == "Read a.py (lines 10–11)"
    assert view.rows[0].text.endswith("x") and view.rows[0].text.lstrip().startswith("10")


def test_long_read_keeps_first_five_and_last_three_and_announces_the_middle():
    text = "\n".join(f"line {i}" for i in range(20))
    view = tool_signal(_tool("read", {"path": "a.py"}, text))
    assert len(view.rows) == 5 + 1 + 3
    assert view.rows[5] == SignalRow("clip", "… 12 lines")
    assert view.rows[0].text.endswith("line 0") and view.rows[4].text.endswith("line 4")
    assert view.rows[6].text.endswith("line 17") and view.rows[-1].text.endswith("line 19")
    assert view.clipped == 12


def test_write_shows_the_content_as_added_lines():
    view = tool_signal(_tool("write", {"path": "p.txt", "content": "a\nb"}))
    assert view.summary == "Wrote p.txt (2 lines)"
    assert [(r.kind, r.text) for r in view.rows] == [("add", "1  a"), ("add", "2  b")]


def test_bash_shows_command_then_output_and_no_failure_line_on_success():
    view = tool_signal(_tool("bash", {"command": "make test"}, "ok\n3 passed", duration_ms=800))
    assert view.summary == "$ make test · 0.8s"
    assert [(r.kind, r.text) for r in view.rows] == [("code", "$ make test"), ("out", "ok"), ("out", "3 passed")]
    assert not any(r.kind == "error" for r in view.rows)


def test_cd_prefix_is_stripped_only_for_an_exact_workspace_match():
    assert strip_workspace("cd /w && make", "/w") == "make"
    assert strip_workspace("cd /w/ && make", "/w/") == "make"
    assert strip_workspace("cd '/w' && make", "/w") == "make"
    assert strip_workspace("cd /w/sub && make", "/w") == "cd /w/sub && make"
    assert strip_workspace("cd /wx && make", "/w") == "cd /wx && make"
    assert strip_workspace("cd /w && make", None) == "cd /w && make"
    assert strip_workspace("make && cd /w && ls", "/w") == "make && cd /w && ls"
    view = tool_signal(_tool("bash", {"command": "cd /w && ls"}, "f"), workspace="/w")
    assert view.rows[0] == SignalRow("code", "$ ls")
    elsewhere = tool_signal(_tool("bash", {"command": "cd /other && ls"}, "f"), workspace="/w")
    assert elsewhere.rows[0] == SignalRow("code", "$ cd /other && ls")


@pytest.mark.parametrize("kwargs, line", [
    ({"metrics": {"exit_code": 2}}, "exit 2"),
    ({"error": "[timed out after 120s; process group terminated]"}, "timed out after 120s"),
    ({"error": "Bash 'x' exited 3; output evicted"}, "exit 3"),
])
def test_failure_line_appears_only_on_failure_after_the_output(kwargs, line):
    view = tool_signal(_tool("bash", {"command": "x"}, "partial", status="failed", **kwargs))
    assert [r.kind for r in view.rows] == ["code", "out", "error"]
    assert view.rows[-1].text == line


def test_other_failures_put_the_error_first():
    view = tool_signal(_tool("grep", {"pattern": "x"}, "a.py:1: x", status="failed", error="bad regex"))
    assert view.rows[0] == SignalRow("error", "bad regex")
    assert view.rows[1] == SignalRow("path", "a.py:1: x")


def test_grep_lists_hits_with_a_match_count():
    view = tool_signal(_tool("grep", {"pattern": "foo", "path": "rust/"}, "a.rs:3: foo\nb.rs:9: foo()"))
    assert view.summary == 'Searched "foo" in rust/ · 2 matches'
    assert [r.kind for r in view.rows] == ["path", "path"]


def test_glob_and_ls_list_paths():
    assert tool_signal(_tool("glob", {"pattern": "*.py"}, "a.py\nb.py")).summary == 'Searched "*.py"'
    ls = tool_signal(_tool("ls", {"path": "src"}, "a\nb"))
    assert ls.summary == "Listed src" and [r.text for r in ls.rows] == ["a", "b"]


def test_web_tools_show_results_and_fetched_markdown():
    search = tool_signal(_tool("websearch", {"query": "ratatui"}, "Title — https://x"))
    assert search.summary == 'Searched web for "ratatui"' and search.rows == (SignalRow("text", "Title — https://x"),)
    fetch = tool_signal(_tool("webfetch", {"url": "https://x"}, "# Page"))
    assert fetch.rows == (SignalRow("md", "# Page"),)


def test_mcp_call_shows_only_non_empty_parameters_then_the_result():
    tool = _tool("McpCall", {"tool": "srv/t", "key": "v", "empty": "", "none": None, "list": []}, "# Result\n- item")
    view = tool_signal(tool)
    assert view.summary == "srv · t"
    assert [(r.kind, r.text) for r in view.rows] == [("kv", "key: v"), ("md", "# Result"), ("md", "- item")]


def test_unknown_tool_shows_parameters_and_plain_result():
    view = tool_signal(_tool("frob", {"n": 3, "flag": True}, "done"))
    assert [(r.kind, r.text) for r in view.rows] == [("kv", "n: 3"), ("kv", "flag: True"), ("text", "done")]


@pytest.mark.parametrize("tool", [
    _tool("read", {"path": "a"}, "x", bundle="SECRET-BUNDLE", iteration=3, executed=True, requested_ts=1.0, started_ts=2.0, finished_ts=3.0),
    _tool("bash", {"command": "ls"}, "x", bundle="SECRET-BUNDLE", iteration=3, executed=True, provenance="Claude Code", started_ts=2.0),
    _tool("grep", {"pattern": "x"}, "a:1: x", bundle="SECRET-BUNDLE", iteration=3),
    _tool("McpCall", {"tool": "s/t", "a": "b"}, "x", bundle="SECRET-BUNDLE", iteration=3),
    _tool("bash", {"command": "ls"}, "x", status="failed", error="boom", bundle="SECRET-BUNDLE", iteration=3),
])
def test_no_metadata_labels_or_ids_appear(tool):
    view = tool_signal(tool)
    text = view.summary + "\n" + view.text()
    assert not any(label in text for label in METADATA), text
    # The full labelled details are untouched and still carry them.
    full = "\n".join(row.label for section in tool_detail_sections(tool) for row in section.rows)
    assert "Call ID" in full and "Status" in full


def test_output_is_control_safe():
    view = tool_signal(_tool("bash", {"command": "echo \x1b[31m"}, "\x1b[2Jhello\x07"))
    assert "\x1b" not in view.text() and "\x07" not in view.text()


def test_rows_are_bounded_and_the_clip_is_announced():
    view = tool_signal(_tool("bash", {"command": "yes"}, "\n".join(f"l{i}" for i in range(SECTION_ROWS + 50))))
    assert len(view.rows) == SECTION_ROWS + 1
    assert view.rows[-1].kind == "clip" and view.rows[-1].text.startswith("… ")
    assert view.clipped > 0


def test_one_huge_value_is_clipped_with_a_count():
    view = tool_signal(_tool("bash", {"command": "cat"}, "x" * (VALUE_LIMIT + 7)))
    assert "… 7 more characters" in view.text()
    assert view.rows[-1] == SignalRow("clip", "… 7 more characters") and view.clipped > 0


def test_clipped_parameter_value_is_announced_with_its_count():
    view = tool_signal(_tool("frob", {"blob": "q" * 2_005}))
    assert view.rows[-1] == SignalRow("clip", "… 5 more characters") and view.clipped > 0


def test_many_parameters_are_bounded_and_announced():
    args = {f"k{i}": "v" for i in range(5_000)}
    view = tool_signal(_tool("frob", args, "done"))
    assert len(view.rows) <= 52
    assert any(r.kind == "clip" and "more parameters" in r.text for r in view.rows)


def test_huge_structured_parameter_is_clipped():
    view = tool_signal(_tool("frob", {"items": list(range(500_000))}))
    assert sum(len(r.text) for r in view.rows) < 10_000


def test_many_result_blocks_and_total_chars_are_bounded():
    result = [{"type": "text", "text": "y" * 1_000} for _ in range(50_000)]
    tool = ToolCallView(call_id="c", name="bash", input={"command": "x"}, result=result, status="completed")
    view = tool_signal(tool)
    assert sum(len(r.text) for r in view.rows) <= MAX_SIGNAL_CHARS + 100
    assert view.rows[-1].kind == "clip" and "more characters" in view.rows[-1].text and view.clipped > 0


def test_total_characters_are_bounded_with_marker():
    text = "\n".join("z" * 1_000 for _ in range(300))
    view = tool_signal(_tool("bash", {"command": "x"}, text))
    assert sum(len(r.text) for r in view.rows if r.kind != "clip") <= MAX_SIGNAL_CHARS
    assert view.rows[-1].kind == "clip" and view.clipped > 0


def test_controls_escaped_in_every_field_and_full_inspector_is_separate():
    tool = _tool("frob", {"k\x1b[2J": "v\x1b[31m"}, "out\x1b]0;t\x07", target="t\x1b[0m")
    view = tool_signal(tool)
    assert "\x1b" not in view.summary + view.text() and "\x07" not in view.text()
    big = "w" * (VALUE_LIMIT + 5_000)
    huge = _tool("bash", {"command": "cat"}, big)
    assert "more characters" in tool_signal(huge).text()
    full = "\n".join(row.value for section in tool_detail_sections(huge) for row in section.rows)
    assert big[:VALUE_LIMIT] in full  # the inspector's own bounds are unchanged by the signal


def test_shell_job_header_is_stripped_but_program_output_is_kept():
    raw = "job_id: j1\nstatus: completed\nexit_code: 0\nstdout:\nhello\nstatus: fine\nstderr:\nwarn"
    view = tool_signal(_tool("bash", {"command": "ls"}, raw))
    assert [r.text for r in view.rows] == ["$ ls", "hello", "status: fine", "stderr:", "warn"]
    assert not any(label in view.text() for label in ("job_id", "exit_code: 0", "stdout:"))
    # Output that merely looks like a header is untouched unless the job header leads.
    plain = tool_signal(_tool("bash", {"command": "ls"}, "status: fine\nexit_code: 0"))
    assert [r.text for r in plain.rows] == ["$ ls", "status: fine", "exit_code: 0"]
    lone = tool_signal(_tool("bash", {"command": "ls"}, "job_id: x\nplain"))
    assert [r.text for r in lone.rows] == ["$ ls", "job_id: x", "plain"]
    assert strip_job_header("job_id: j\nstatus: running") == ""


def test_failed_shell_still_reports_its_exit_after_the_header_is_stripped():
    raw = "job_id: j\nstatus: failed\nexit_code: 2\nstderr:\nboom"
    view = tool_signal(_tool("bash", {"command": "x"}, raw, status="failed", metrics={"exit_code": 2}))
    assert [r.text for r in view.rows] == ["$ x", "stderr:", "boom", "exit 2"]


def test_closed_summary_is_a_target_not_output():
    assert closed_summary(_tool("read", {"path": "a.py"}, "first\nsecond")) == "a.py (lines 1–2)"
    assert closed_summary(_tool("read", {"path": "a.py", "offset": 5}, "x")) == "a.py (lines 5–5)"
    assert closed_summary(_tool("read", {"path": "a.py"}, status="running")) == "a.py"
    assert closed_summary(_tool("read", {"path": "a.py"}, "boom", status="failed", error="no")) == "a.py"
    assert closed_summary(_tool("bash", {"command": "make\nmore"}, "out")) == "make"
    assert closed_summary(_tool("grep", {"pattern": "x"}, "a:1: x")) is None


def test_command_is_clipped_to_forty_display_cells():
    long = closed_summary(_tool("bash", {"command": "echo " + "a" * 100}))
    assert len(long) == 40 and long.endswith("…")
    wide = clip_cells("界" * 30)
    assert wide.endswith("…") and 2 * (len(wide) - 1) + 1 <= 40
    assert clip_cells("short") == "short"


def test_wire_is_json_ready_rows():
    assert tool_signal(_tool("bash", {"command": "ls"}, "a")).wire() == [["code", "$ ls"], ["out", "a"]]
