"""Focused offline tests for the minimal ACP v1 stdio JSON-RPC transport.

Every test runs a real fake agent subprocess; no agy, credentials or network.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from nexus.workers import acp, base
from nexus.workers.acp import AcpClient, AcpProcessError, AcpProtocolError, AcpResponseError, AcpTimeoutError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="ACP stdio cleanup uses POSIX process groups")
FIXTURE = Path(__file__).parent / "fixtures" / "workers" / "fake_acp_agent.py"


def make_client(tmp_path: Path, scenario: str = "echo", *extra: str, **kwargs) -> AcpClient:
    return AcpClient(sys.executable, [str(FIXTURE), scenario, *extra], cwd=tmp_path, **kwargs)


async def wait_text(path: Path, expected: str) -> None:
    async with asyncio.timeout(5):
        while not path.exists() or path.read_text(encoding="utf-8") != expected:
            await asyncio.sleep(.01)


async def test_initialize_negotiates_version_one_and_requests_return_fake_results(tmp_path):
    async with make_client(tmp_path) as client:
        info = await client.initialize(client_capabilities={"fs": {}},
                                       client_info={"name": "nexus-test", "version": "1"})
        assert info["protocolVersion"] == acp.PROTOCOL_VERSION
        assert await client.request("sample", {"n": 1}) == {"echo": {"n": 1}}
        pid = client.pid
        assert pid is not None and client.returncode is None
    assert client.returncode == 0
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_responses_are_correlated_when_they_arrive_out_of_order(tmp_path):
    async with make_client(tmp_path, "out-of-order") as client:
        await client.initialize()
        first, second = await asyncio.gather(client.request("first"), client.request("second"))
    assert first == {"which": "first"}
    assert second == {"which": "second"}


async def test_notifications_permission_and_nested_requests_do_not_deadlock(tmp_path):
    updates: list[str] = []
    permissions: list[dict] = []
    pongs: list[object] = []
    raw: list[bytes] = []
    clients: list[AcpClient] = []

    async def on_message(line: bytes) -> None:
        raw.append(line)

    async def on_notification(method: str, params) -> None:
        if method == "session/update":
            updates.append(params["update"]["content"]["text"])

    async def on_permission(params: dict) -> str:
        permissions.append(params)
        pongs.append(await clients[0].request("client/ping", {"n": 1}))
        return "allow-once"

    client = make_client(tmp_path, "interleaved", on_message=on_message,
                         on_notification=on_notification, on_permission=on_permission)
    clients.append(client)
    async with client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.request("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "go"}]}), 5)
        await client.flush_notifications()
    assert result == {"stopReason": "end_turn", "handled": ["client/ping"]}
    assert updates == ["first", "permission:allow-once", "second"]
    assert permissions and permissions[0]["toolCall"]["title"] == "Run the tests"
    assert pongs == [{"method": "client/ping"}]
    messages = [json.loads(line) for line in raw]
    assert any(message.get("method") == "session/request_permission" and "id" in message for message in messages)
    assert any(message.get("method") == "session/update" for message in messages)


async def test_missing_permission_callback_is_answered_cancelled_without_hanging(tmp_path):
    async with make_client(tmp_path, "interleaved") as client:
        await client.initialize()
        result = await asyncio.wait_for(client.request("session/prompt", {"sessionId": "s1", "prompt": []}), 5)
    assert result == {"stopReason": "end_turn", "handled": []}
    assert any("no permission callback" in note for note in client.notes)


async def test_permission_can_stay_pending_while_notifications_and_prompt_finish(tmp_path):
    updates: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()
    answered = asyncio.Event()

    async def on_notification(method: str, params) -> None:
        if method == "session/update":
            text = params["update"]["content"]["text"]
            updates.append(text)
            if text.startswith("permission:"):
                answered.set()

    async def on_permission(params: dict) -> str:
        started.set()
        await release.wait()
        return "allow-once"

    async with make_client(tmp_path, "permission-late", on_notification=on_notification,
                           on_permission=on_permission) as client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.request("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "go"}]}), 5)
        assert result == {"stopReason": "end_turn", "permissionPending": True}
        await asyncio.wait_for(started.wait(), 5)
        await client.flush_notifications()
        assert updates == ["before", "during"]
        assert not release.is_set()
        release.set()
        await asyncio.wait_for(answered.wait(), 5)
        await client.flush_notifications()
    assert updates == ["before", "during", "permission:allow-once"]


async def test_remote_error_is_typed_and_does_not_echo_credentials(tmp_path):
    async with make_client(tmp_path, "error") as client:
        await client.initialize()
        with pytest.raises(AcpResponseError) as caught:
            await client.request("boom", {"api_key": "SECRET"})
    error = caught.value
    assert error.code == acp.INVALID_PARAMS
    assert error.method == "boom"
    assert error.data == {"field": "x"}
    assert "SECRET" not in str(error)


async def test_malformed_input_reaches_the_raw_callback_before_failing_pending(tmp_path):
    raw: list[bytes] = []

    async def on_message(line: bytes) -> None:
        raw.append(line)

    async with make_client(tmp_path, "malformed", on_message=on_message) as client:
        await client.initialize()
        with pytest.raises(AcpProtocolError, match="malformed JSON"):
            await asyncio.wait_for(client.request("boom"), 5)
        assert isinstance(client.failure, AcpProtocolError)
    assert raw[-1] == b"not json"


async def test_oversized_input_fails_and_the_agent_is_stopped(tmp_path):
    client = make_client(tmp_path, "oversize", message_limit=512)
    async with client:
        await client.initialize()
        with pytest.raises(AcpProtocolError, match="line limit"):
            await asyncio.wait_for(client.request("boom"), 5)
        pid = client.pid
        assert pid is not None
    assert client.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_agent_death_fails_pending_requests_and_sanitizes_stderr(tmp_path):
    client = make_client(tmp_path, "death")
    async with client:
        await client.initialize()
        with pytest.raises(AcpProcessError) as caught:
            await asyncio.wait_for(client.request("die"), 5)
        assert "SECRET" not in str(caught.value)
    assert client.returncode == 7
    assert "api_key=[redacted]" in client.diagnostics
    assert "SECRET" not in client.diagnostics
    assert len(client.diagnostics) <= acp.MAX_DIAGNOSTIC_CHARS


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_cancel_and_timeout_send_a_cancel_request(tmp_path, mode):
    marker = tmp_path / "marker"
    async with make_client(tmp_path, "slow", str(marker)) as client:
        await client.initialize()
        if mode == "timeout":
            with pytest.raises(AcpTimeoutError):
                await client.request("slow", timeout=.2)
        else:
            task = asyncio.create_task(client.request("slow"))
            await wait_text(marker, "started")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await wait_text(marker, "cancelled")


async def test_unknown_server_request_is_answered_method_not_found(tmp_path):
    async with make_client(tmp_path, "unknown") as client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.request("session/prompt", {"sessionId": "s1", "prompt": []}), 5)
    assert result["stopReason"] == "end_turn"
    assert result["unknown"]["code"] == acp.METHOD_NOT_FOUND
    assert "vendor/unknown" in result["unknown"]["message"]


async def test_initialize_rejects_a_protocol_version_other_than_one(tmp_path):
    async with make_client(tmp_path, "wrong-version") as client:
        with pytest.raises(AcpProtocolError, match="protocol version 1"):
            await asyncio.wait_for(client.initialize(), 5)
        assert isinstance(client.failure, AcpProtocolError)


async def test_close_is_idempotent_and_stops_the_owned_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "EOF_GRACE_SECONDS", .05)
    monkeypatch.setattr(base, "TERM_GRACE_SECONDS", .05)
    marker = tmp_path / "pid"
    client = make_client(tmp_path, "linger", str(marker))
    await client.start()
    await client.initialize()
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(.01)
    pid = int(marker.read_text(encoding="utf-8"))
    assert client.pid == pid
    await asyncio.wait_for(client.close(), 5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    await client.close()
    with pytest.raises(AcpProcessError):
        await client.request("late")


async def test_oversized_outgoing_message_is_refused_before_writing(tmp_path):
    async with make_client(tmp_path, "hold", message_limit=512) as client:
        await client.initialize()
        with pytest.raises(ValueError, match="exceeds 512 bytes"):
            await client.request("huge", {"blob": "x" * 1000})
