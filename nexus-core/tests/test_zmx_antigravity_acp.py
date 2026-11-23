"""Offline tests for the Antigravity ACP worker (no agy, credentials or provider call)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from nexus.workers.antigravity import (
    ACP_TRANSPORT,
    AUTH_INSTRUCTION,
    LEGACY_TRANSPORT,
    AcpPolicy,
    AntigravityAcpWorker,
    AntigravityWorker,
    LegacyAntigravitySession,
    acp_resumable,
)
from nexus.workers.base import WorkerError, WorkerPolicy

pytestmark = pytest.mark.skipif(os.name != "posix", reason="ACP stdio transport uses POSIX process groups")
FIXTURE = Path(__file__).parent / "fixtures" / "workers" / "fake_agy_acp.py"


def make_worker(tmp_path: Path, scenario: str) -> AntigravityAcpWorker:
    (tmp_path / "acp-scenario.txt").write_text(scenario, encoding="utf-8")
    launcher = tmp_path / "fake-agy"
    launcher.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FIXTURE)!r}, run_name='__main__')\n")
    launcher.chmod(0o700)
    return AntigravityAcpWorker(str(launcher), environ={"HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")})


def read_log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "acp-log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def request_map(tmp_path: Path) -> dict[str, dict]:
    return {entry["method"]: entry["params"] for entry in read_log(tmp_path) if "method" in entry}


async def wait_text(path: Path, expected: str) -> None:
    async with asyncio.timeout(5):
        while not path.exists() or path.read_text(encoding="utf-8") != expected:
            await asyncio.sleep(.01)


async def collect(worker: AntigravityAcpWorker, tmp_path: Path, *, prompt: str = "assignment", model: str = "gemini-test",
                  policy: AcpPolicy | None = None, **kwargs):
    events = []

    async def emit(event):
        events.append(event)

    result = await asyncio.wait_for(worker.run(prompt=prompt, model=model, workspace=tmp_path,
                                               policy=policy, emit=emit, **kwargs), 10)
    return result, events


def test_acp_policy_refuses_bypass_and_marks_transports():
    with pytest.raises(ValueError, match="bypass"):
        AcpPolicy(permission_mode="always-proceed")
    assert AcpPolicy.from_worker_policy(WorkerPolicy("auto")) == AcpPolicy("default")
    assert AcpPolicy.from_worker_policy(WorkerPolicy("accept-edits")) == AcpPolicy("default")
    assert AcpPolicy.from_worker_policy(WorkerPolicy("plan")) == AcpPolicy("plan")
    assert AntigravityAcpWorker.transport == ACP_TRANSPORT
    assert AntigravityWorker.transport == LEGACY_TRANSPORT
    assert acp_resumable({"transport": ACP_TRANSPORT, "session_id": "acp-1", "policy_fingerprint": "f"})
    assert not acp_resumable({"transport": ACP_TRANSPORT, "session_id": "acp-1"})
    assert not acp_resumable({"session_id": "legacy"})
    assert not acp_resumable({"transport": LEGACY_TRANSPORT, "session_id": "legacy"})
    authority = AcpPolicy("default").authority(Path("/tmp/ws"))
    assert "ACP mode default" in authority and "approval" in authority and "context Nexus cannot see" in authority


async def test_new_session_sends_the_exact_contract_and_text(tmp_path):
    worker = make_worker(tmp_path, "text")
    prompt = "[Nexus assignment instructions]\nBe careful.\n\n[Assignment]\nDo the thing"
    result, events = await collect(worker, tmp_path, prompt=prompt, policy=AcpPolicy("default"))
    assert result.status == "completed" and result.text == "answer" and result.session_id == "acp-1"
    assert result.policy_fingerprint == AcpPolicy("default").fingerprint("gemini-test", tmp_path.resolve())
    assert acp_resumable({"transport": ACP_TRANSPORT, "session_id": result.session_id,
                          "policy_fingerprint": result.policy_fingerprint})
    assert result.notes and "context Nexus cannot see" in result.notes[0]
    requests = request_map(tmp_path)
    assert requests["initialize"]["protocolVersion"] == 1
    assert requests["initialize"]["clientCapabilities"] == {} and "clientInfo" not in requests["initialize"]
    assert requests["session/new"] == {"cwd": str(tmp_path.resolve()), "mcpServers": []}
    assert requests["session/set_model"] == {"sessionId": "acp-1", "modelId": "gemini-test"}
    assert requests["session/set_mode"] == {"sessionId": "acp-1", "modeId": "default"}
    assert requests["session/prompt"] == {"sessionId": "acp-1", "prompt": [{"type": "text", "text": prompt}]}
    assert "systemPrompt" not in json.dumps(requests)
    launch = next(event for event in events if event.kind == "launch")
    assert launch.data["transport"] == ACP_TRANSPORT and launch.data["pid"] > 0
    assert next(event for event in events if event.kind == "init").data["conversation_id"] == "acp-1"
    assert any(event.kind == "process" and event.data.get("session_id") == "acp-1" for event in events)
    assert [event.data["text"] for event in events if event.kind == "text"] == ["answer"]
    wire = [event.raw for event in events if event.kind == "wire"]
    assert wire and all(isinstance(line, bytes) for line in wire)
    assert json.loads(wire[0])["id"] == 1


async def test_tool_updates_become_standard_tool_events(tmp_path):
    result, events = await collect(make_worker(tmp_path, "tool"), tmp_path)
    assert result.status == "completed" and result.text == "done"
    starts = [event for event in events if event.kind == "tool.start"]
    ends = [event for event in events if event.kind == "tool.end"]
    assert len(starts) == len(ends) == 1
    assert starts[0].data["name"] == "read_file" and starts[0].data["input"] == {"path": "a.txt"}
    assert starts[0].data["id"] == ends[0].data["id"] == "agy:t1"
    assert ends[0].data["state"] == "DONE" and ends[0].data["output"] == "contents"


@pytest.mark.parametrize("allow,option,status", [(True, "allow-once", "completed"),
                                                  (False, "reject-once", "needs_attention")])
async def test_permission_uses_nexus_approval_and_selects_a_per_call_option(tmp_path, allow, option, status):
    calls = []

    async def approve(call, args):
        calls.append((call, args))
        return allow

    result, events = await collect(make_worker(tmp_path, "permission"), tmp_path, approve=approve)
    assert calls and calls[0][0].id == "agy:t1" and calls[0][0].name == "read_file"
    assert calls[0][1] == {"path": "a.txt"}
    starts = [event for event in events if event.kind == "tool.start" and event.data.get("tool_call_id") == "t1"]
    assert starts and starts[0].data["id"] == calls[0][0].id
    assert result.text == f"permission:{option}"
    assert result.status == status
    assert {"permission": {"outcome": "selected", "optionId": option}} in read_log(tmp_path)
    if not allow:
        assert result.denied_actions and "declined" in result.error


async def test_permission_without_a_callback_fails_closed(tmp_path):
    result, _ = await collect(make_worker(tmp_path, "permission"), tmp_path)
    assert result.status == "needs_attention"
    assert "could not obtain an approval" in result.error
    assert {"permission": {"outcome": "cancelled"}} in read_log(tmp_path)


async def test_persistent_only_permission_options_are_refused_without_prompting(tmp_path):
    calls = []

    async def approve(call, args):
        calls.append((call, args))
        return True

    result, _ = await collect(make_worker(tmp_path, "permission-persistent"), tmp_path, approve=approve)
    assert not calls
    assert result.status == "needs_attention" and result.text == "permission:cancelled"
    assert any("persistent permission" in note for note in result.notes)


async def test_permission_callback_error_rejects_instead_of_allowing(tmp_path):
    async def approve(_call, _args):
        raise RuntimeError("approval backend failed")

    result, _ = await collect(make_worker(tmp_path, "permission"), tmp_path, approve=approve)
    assert result.status == "needs_attention" and result.text == "permission:reject-once"
    assert any("approval failed" in note for note in result.notes)
    assert {"permission": {"outcome": "selected", "optionId": "reject-once"}} in read_log(tmp_path)


async def test_prompt_ending_with_an_unanswered_permission_fails_closed(tmp_path):
    async def approve(_call, _args):
        await asyncio.sleep(60)
        return True

    result, _ = await collect(make_worker(tmp_path, "permission-abandoned"), tmp_path, approve=approve)
    assert result.status == "needs_attention" and result.text == "ended"
    assert any("unanswered permission" in note for note in result.notes)


async def test_async_permission_keeps_notifications_running(tmp_path):
    working = asyncio.Event()
    events = []

    async def emit(event):
        events.append(event)
        if event.kind == "tool.start" and event.data.get("tool_call_id") == "t0":
            working.set()

    async def approve(_call, _args):
        await asyncio.wait_for(working.wait(), 5)
        return True

    result = await asyncio.wait_for(make_worker(tmp_path, "permission-async").run(
        prompt="assignment", model="gemini-test", workspace=tmp_path, policy=AcpPolicy("default"),
        emit=emit, approve=approve), 10)
    assert working.is_set() and result.status == "completed" and result.text == "done"


async def test_cancellation_sends_session_cancel_and_stops_the_process(tmp_path):
    worker = make_worker(tmp_path, "cancel")
    events = []

    async def emit(event):
        events.append(event)

    task = asyncio.create_task(worker.run(prompt="assignment", model="gemini-test", workspace=tmp_path,
                                          policy=AcpPolicy("default"), emit=emit))
    with pytest.raises(asyncio.CancelledError):
        await wait_text(tmp_path / "acp-marker.txt", "started")
        async with asyncio.timeout(5):
            while not any(event.kind == "launch" for event in events):
                await asyncio.sleep(.01)
        pid = next(event for event in events if event.kind == "launch").data["pid"]
        task.cancel()
        await asyncio.wait_for(task, 5)
    await wait_text(tmp_path / "acp-marker.txt", "cancelled")
    assert "session/cancel" in [entry.get("method") for entry in read_log(tmp_path)]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_resume_needs_the_acp_marker_and_matching_fingerprint(tmp_path):
    worker = make_worker(tmp_path, "resume")
    policy = AcpPolicy("default")
    with pytest.raises(LegacyAntigravitySession, match="legacy stream-json"):
        await worker.run(prompt="assignment", model="gemini-test", workspace=tmp_path, policy=policy,
                         resume="old-conversation", previous_transport=None, previous_fingerprint="x")
    with pytest.raises(WorkerError, match="different model, mode, effort or directory"):
        await worker.run(prompt="assignment", model="gemini-test", workspace=tmp_path, policy=policy,
                         resume="acp-1", previous_transport=ACP_TRANSPORT, previous_fingerprint="x")
    first, _ = await collect(worker, tmp_path, policy=policy)
    second, _ = await collect(worker, tmp_path, policy=policy, resume="acp-1",
                              previous_transport=ACP_TRANSPORT, previous_fingerprint=first.policy_fingerprint)
    assert first.status == second.status == "completed"
    methods = [entry["method"] for entry in read_log(tmp_path) if "method" in entry]
    assert methods.count("session/new") == 1 and methods.count("session/resume") == 1
    resume = next(entry["params"] for entry in read_log(tmp_path) if entry.get("method") == "session/resume")
    assert resume == {"sessionId": "acp-1", "cwd": str(tmp_path.resolve()), "mcpServers": []}


async def test_resume_error_fails_closed_without_sending_a_prompt(tmp_path):
    worker = make_worker(tmp_path, "resume-error")
    first, _ = await collect(worker, tmp_path)
    result, _ = await collect(worker, tmp_path, resume="acp-1", previous_transport=ACP_TRANSPORT,
                              previous_fingerprint=first.policy_fingerprint)
    assert result.status == "failed" and "session/resume" in result.error
    methods = [entry["method"] for entry in read_log(tmp_path) if "method" in entry]
    assert methods.count("session/new") == 1 and methods.count("session/prompt") == 1


async def test_resume_without_the_advertised_capability_fails_closed(tmp_path):
    worker = make_worker(tmp_path, "no-resume")
    first, _ = await collect(worker, tmp_path)
    result, _ = await collect(worker, tmp_path, resume="acp-1", previous_transport=ACP_TRANSPORT,
                              previous_fingerprint=first.policy_fingerprint)
    assert result.status == "failed" and "session/resume" in result.error
    assert "session/resume" not in [entry.get("method") for entry in read_log(tmp_path)]


@pytest.mark.parametrize("scenario", ["new-error", "init-auth", "stderr-auth"])
async def test_authentication_error_points_to_agy_acp_login(tmp_path, scenario):
    result, _ = await collect(make_worker(tmp_path, scenario), tmp_path)
    assert result.status == "needs_attention"
    assert result.error == AUTH_INSTRUCTION and "agy acp --login" in result.error
    assert "session/prompt" not in request_map(tmp_path)


@pytest.mark.parametrize("scenario,policy,match", [
    ("model-error", AcpPolicy("default"), "ACP request failed"),
    ("no-modes", AcpPolicy("default"), "did not advertise session modes"),
    ("mode-missing", AcpPolicy("default"), "did not advertise mode"),
    ("mode-wrong", AcpPolicy("default"), "did not confirm the requested mode"),
    ("effort-missing", AcpPolicy("default", "high"), "thought_level"),
])
async def test_setup_failures_fail_closed_before_prompt(tmp_path, scenario, policy, match):
    result, _ = await collect(make_worker(tmp_path, scenario), tmp_path, policy=policy)
    assert result.status == "failed" and match in result.error
    assert "session/prompt" not in request_map(tmp_path)


async def test_advertised_model_config_option_is_preferred_over_the_extension(tmp_path):
    result, _ = await collect(make_worker(tmp_path, "model-config"), tmp_path)
    assert result.status == "completed" and result.text == "answer"
    requests = request_map(tmp_path)
    assert requests["session/set_config_option"] == {"sessionId": "acp-1", "configId": "model", "value": "gemini-test"}
    assert "session/set_model" not in requests


async def test_effort_uses_the_advertised_thought_level_option(tmp_path):
    result, _ = await collect(make_worker(tmp_path, "effort"), tmp_path, policy=AcpPolicy("default", "high"))
    assert result.status == "completed" and result.text == "answer"
    assert request_map(tmp_path)["session/set_config_option"] == {
        "sessionId": "acp-1", "configId": "thought_level", "value": "high"}


async def test_unknown_updates_are_wire_only_and_noted(tmp_path):
    result, events = await collect(make_worker(tmp_path, "unknown"), tmp_path)
    assert result.status == "completed" and result.text == "ok"
    assert any("future_update" in note for note in result.notes)
    assert any(event.kind == "wire" and event.raw and b"future_update" in event.raw for event in events)


async def test_prompt_usage_is_per_turn_and_normalized(tmp_path):
    result, _ = await collect(make_worker(tmp_path, "usage"), tmp_path)
    assert result.status == "completed" and result.text == "usage"
    assert result.usage == {"input_tokens": 11, "output_tokens": 3,
                            "cache_read_input_tokens": 5, "reasoning_tokens": 2}
    assert not any("cumulative" in note for note in result.notes)


@pytest.mark.parametrize("allow,option", [(True, "allow"), (False, "deny")])
async def test_live_acp_model_config_permission_and_usage(tmp_path, allow, option):
    calls = []

    async def approve(call, args):
        calls.append((call, args))
        return allow

    result, _ = await collect(make_worker(tmp_path, "live"), tmp_path, model="gemini-3.8-flash-low",
                              policy=AcpPolicy("default"), approve=approve)
    assert result.session_id == "live-session"
    assert result.usage == {"input_tokens": 10404, "output_tokens": 74,
                            "cache_read_input_tokens": 0, "reasoning_tokens": 0}
    requests = request_map(tmp_path)
    assert requests["session/set_model"] == {"sessionId": "live-session", "modelId": "gemini-3.8-flash-low"}
    assert "session/set_config_option" not in requests  # the exact per-effort id is not a config value
    assert requests["session/set_mode"] == {"sessionId": "live-session", "modeId": "default"}
    methods = [entry["method"] for entry in read_log(tmp_path) if "method" in entry]
    assert methods.index("session/set_model") < methods.index("session/set_mode")
    assert methods.index("session/set_mode") < methods.index("session/prompt")
    assert calls and calls[0][0].name == "write_file" and calls[0][0].id == "agy:call-write"
    assert calls[0][1] == {"Content": "ok", "Path": "/workspace/probe-write.txt"}
    assert {"permission": {"outcome": "selected", "optionId": option}} in read_log(tmp_path)
    assert result.status == "needs_attention"  # the live first turn ends with no assistant text
    assert bool(result.denied_actions) is (not allow)


async def test_live_wrong_config_confirmation_fails_before_the_prompt(tmp_path):
    result, _ = await collect(make_worker(tmp_path, "live-wrong"), tmp_path, model="gemini-3.8-flash-low",
                              policy=AcpPolicy("default"))
    assert result.status == "failed" and "reported model" in result.error
    assert "session/prompt" not in request_map(tmp_path)


async def test_live_resume_uses_session_resume_without_load_replay_and_reports_usage(tmp_path):
    worker = make_worker(tmp_path, "live")

    async def deny(_call, _args):
        return False

    first, _ = await collect(worker, tmp_path, model="gemini-3.8-flash-low", policy=AcpPolicy("default"),
                             approve=deny)
    second, _ = await collect(worker, tmp_path, model="gemini-3.8-flash-low", policy=AcpPolicy("default"),
                              resume="live-session", previous_transport=ACP_TRANSPORT,
                              previous_fingerprint=first.policy_fingerprint)
    assert second.status == "completed" and second.text == "resumed"
    assert second.usage == {"input_tokens": 2547, "output_tokens": 2,
                            "cache_read_input_tokens": 8350, "reasoning_tokens": 0}
    methods = [entry["method"] for entry in read_log(tmp_path) if "method" in entry]
    assert methods.count("session/new") == 1 and methods.count("session/resume") == 1
    assert "session/load" not in methods
