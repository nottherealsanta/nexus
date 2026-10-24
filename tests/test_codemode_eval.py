"""Offline code-mode evaluation harness (plan 03, E1): direct tools vs the kernel tools.

These are regression tests of the harness and of the mechanical properties it
measures (fewer model-visible bytes and requests for the scripted programs). They
do not assert anything about live model quality.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "benchmark" / "codemode_eval.py"
_SPEC = importlib.util.spec_from_file_location("nexus_benchmark_codemode_eval", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
ev = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ev)

needs_monty = pytest.mark.skipif(not ev.code_mode_available(), reason="pydantic-monty not installed")


async def test_direct_arm_runs_without_monty(tmp_path):
    """The direct arm is the control and must not need the optional dependency."""
    out = await ev.run_scenario("fanout_read", tmp_path, monty=False)
    assert out["direct"]["passed"], out["direct"]
    assert out["direct"]["top_level_tool_calls"] == ev.FILES
    assert out["direct"]["nested_tool_calls"] == 0
    assert out["code"] == {"skipped": ev.SKIP_NO_MONTY}
    assert "ratio" not in out


async def test_eval_state_stays_in_its_own_home_not_the_ambient_one(tmp_path, monkeypatch):
    ambient = tmp_path / "ambient"
    monkeypatch.setenv("NEXUS_HOME", str(ambient))
    base = tmp_path / "base"
    await ev.run_scenario("fanout_read", base, monty=False)
    # ``home=`` is a fake HOME: the state root is <home>/.nexus (config.paths.nexus_home).
    assert (base / "nexus-home" / ".nexus" / "nexus.db").exists()
    assert not ambient.exists()


async def test_long_child_skips_cleanly_without_monty(tmp_path):
    out = await ev.run_scenario("long_child_context", tmp_path, monty=False)
    assert out == {"scenario": "long_child_context", "code": {"skipped": ev.SKIP_NO_MONTY}}
    assert "skipped" in ev.render([out])


@needs_monty
async def test_fanout_read_code_mode_cuts_model_visible_bytes(tmp_path):
    out = await ev.run_scenario("fanout_read", tmp_path, monty=True)
    direct, code = out["direct"], out["code"]
    assert direct["passed"] and code["passed"], out
    # code_start + one cell; the handshake costs one model request and the manual's bytes once.
    assert code["top_level_tool_calls"] == 2 and code["nested_tool_calls"] == ev.FILES
    assert (direct["model_requests"], code["model_requests"]) == (2, 3)
    assert code["manual_bytes"] > 0 and direct["manual_bytes"] == 0
    assert direct["tool_result_bytes"] > 5 * code["tool_result_bytes"]
    assert direct["tool_result_bytes"] > 10 * (code["tool_result_bytes"] - code["manual_bytes"])
    assert code["tool_errors"] == 0 and direct["tool_errors"] == 0


@needs_monty
async def test_fanout_grep_code_mode_summarises(tmp_path):
    out = await ev.run_scenario("fanout_grep", tmp_path, monty=True)
    direct, code = out["direct"], out["code"]
    assert direct["passed"] and code["passed"], out
    assert code["nested_tool_calls"] == len(ev.GREP_PATTERNS)
    assert direct["top_level_tool_calls"] == len(ev.GREP_PATTERNS)
    # The one-time manual outweighs this small scenario's savings; the cell result itself is smaller.
    assert (code["tool_result_bytes"] - code["manual_bytes"]) * 2 < direct["tool_result_bytes"]


@needs_monty
async def test_mcp_list_filter_uses_real_stdio_server(tmp_path):
    out = await ev.run_scenario("mcp_list_filter", tmp_path, monty=True)
    direct, code = out["direct"], out["code"]
    assert direct["passed"] and code["passed"], out
    assert direct["model_requests"] == 3 and code["model_requests"] == 3  # start, one cell, answer
    # 1 list + one get per open critical record, all inside one program
    assert code["nested_tool_calls"] == 1 + len(ev.RECORD_IDS)
    assert direct["tool_result_bytes"] > 5 * code["tool_result_bytes"]
    # the program's filter leaves out critical-but-closed issues; the direct list shows them
    assert code["forbidden_present"] == []

@needs_monty
async def test_long_child_transcript_is_queried_not_dumped(tmp_path):
    out = await ev.run_scenario("long_child_context", tmp_path, monty=True)
    code = out["code"]
    raw = sum(path.stat().st_size for path in tmp_path.rglob("d*.txt"))
    assert raw > 20_000
    assert code["passed"]
    assert code["expected_found"] == ["NEEDLE-7731", "CHILD-REPORT"]
    assert code["surfaced_to_root_bytes"] * 10 < raw


async def test_runs_are_deterministic(tmp_path):
    first = await ev.run_scenario("fanout_read", tmp_path / "a", monty=False)
    second = await ev.run_scenario("fanout_read", tmp_path / "b", monty=False)
    assert first == second


def test_grader_fails_when_expected_text_is_missing():
    """A strategy that drops required facts must not pass on size alone."""
    class Msg:
        content = []

    class Session:
        messages = [Msg()]

    class Provider:
        calls = 1
        requests = [type("R", (), {"tools": []})()]

    out = ev.measure(Session(), Provider(), [], expected=["TODO(o4)"])
    assert out["passed"] is False and out["expected_missing"] == ["TODO(o4)"]


def test_render_and_cli_reject_unknown_scenario(capsys):
    rendered = ev.render([{"scenario": "x", "direct": {"skipped": "n/a"}, "code": {"skipped": "n/a"}}])
    assert "not evidence of live model quality" in rendered
    with pytest.raises(SystemExit):
        ev.main(["--scenario", "nope"])


@needs_monty
def test_cli_json_output_and_exit_code(capsys):
    assert ev.main(["--scenario", "fanout_read", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["scenario"] == "fanout_read" and data[0]["code"]["passed"]
