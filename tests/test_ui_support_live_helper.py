"""``ui_support/live_helper.py``: the nexus-live process wrapper, with a fake helper (M4)."""
from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.ui_support import live_helper as lh
from nexus.ui_support.live_helper import LiveCall, LiveHelper, LiveHelperError

FAKE = Path(__file__).parent / "fixtures" / "live" / "fake_nexus_live.py"


@pytest.fixture
def fake_binary(tmp_path, monkeypatch):
    """An executable wrapper around the fake helper (the real binary is a single file)."""
    wrapper = tmp_path / "nexus-live"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE}\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("NEXUS_LIVE_BINARY", str(wrapper))
    monkeypatch.setenv("FAKE_LIVE_LOG", str(tmp_path / "commands.jsonl"))
    return wrapper


def commands(tmp_path):
    path = tmp_path / "commands.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class FakeClient:
    def __init__(self, fail_start=None):
        self.calls: list[tuple] = []
        self.fail_start = fail_start
        self.muted = False

    async def live_start(self, session, offer, *, voice="", new_conversation=False):
        self.calls.append(("start", session, offer, voice, new_conversation))
        if self.fail_start:
            raise self.fail_start
        return SimpleNamespace(call_id="c1", answer_sdp="v=0\r\nanswer", voice="sol")

    async def live_stop(self, session, reason="user"):
        self.calls.append(("stop", session, reason))
        return SimpleNamespace(state="idle", muted=False)

    async def live_mute(self, session, muted=None):
        self.muted = (not self.muted) if muted is None else muted
        self.calls.append(("mute", session, muted))
        return SimpleNamespace(state="muted" if self.muted else "active", muted=self.muted)

    async def live_pause(self, session, paused=None):
        self.paused = (not getattr(self, "paused", False)) if paused is None else paused
        self.calls.append(("pause", session, paused))
        return SimpleNamespace(state="paused" if self.paused else "active", paused=self.paused)

    async def live_helper_event(self, session, kind, detail=""):
        self.calls.append(("event", kind, detail))
        return SimpleNamespace(ok=True)


def test_missing_binary_is_a_clear_unsupported_error(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_LIVE_BINARY", str(tmp_path / "nope"))
    assert lh.available() is False
    with pytest.raises(LiveHelperError) as info:
        lh.binary_path()
    assert info.value.code == "unsupported" and "nexus-live" in str(info.value)
    assert "uv tool install --upgrade --force nexus-harness" in info.value.hint
    assert "restart the daemon" in info.value.hint and "cargo" not in info.value.hint


@pytest.mark.parametrize("kind", ["live_unavailable", "unsupported"])
def test_unavailable_hints_have_no_live_extra(kind):
    fix = lh.fix_for(kind)
    assert fix == lh.INSTALL_HINT and "[live]" not in fix and "cargo" not in fix


def test_symlinked_venv_python_finds_neighbour_binary(monkeypatch, tmp_path):
    """uv-style venv: bin/python is a symlink elsewhere, nexus-live sits beside the symlink."""
    real = tmp_path / "real" / "python3"
    real.parent.mkdir()
    real.write_text("")
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").symlink_to(real)
    helper = bindir / "nexus-live"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    monkeypatch.delenv("NEXUS_LIVE_BINARY", raising=False)
    monkeypatch.setattr(lh.sys, "executable", str(bindir / "python"))
    monkeypatch.setattr(lh.shutil, "which", lambda name: None)
    assert lh.binary_path() == helper.resolve()


def test_binary_override_and_missing_exec_bit(monkeypatch, tmp_path):
    plain = tmp_path / "nexus-live"
    plain.write_text("x")
    monkeypatch.setenv("NEXUS_LIVE_BINARY", str(plain))
    assert not lh.available()  # not executable
    plain.chmod(0o755)
    assert lh.binary_path() == plain.resolve()


async def test_spawn_failure_is_unsupported(tmp_path):
    async def boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file")

    helper = LiveHelper(binary=tmp_path / "x", spawn=boom)
    with pytest.raises(LiveHelperError) as info:
        await helper.start()
    assert info.value.code == "unsupported"


async def test_offer_answer_mute_stop(fake_binary, tmp_path):
    seen, forwarded = [], []

    async def forward(kind, detail):
        forwarded.append((kind, detail))

    helper = LiveHelper(on_event=seen.append, forward=forward)
    sdp = await helper.offer()
    assert sdp == "v=0\r\nfake-offer"
    await helper.answer("v=0\r\nanswer")
    assert helper.ready and helper.running
    await asyncio.sleep(0.2)
    assert (helper.level_in, helper.level_out, helper.phase) == (0.25, 0.5, "speaking")
    await helper.mute(True)
    assert helper.muted
    await helper.pause(True)
    assert helper.paused
    await helper.stop()
    await helper.stop()  # idempotent
    assert not helper.running and helper.process.returncode == 0
    assert [c["type"] for c in commands(tmp_path)] == ["offer", "answer", "mute", "pause", "stop"]
    assert commands(tmp_path)[2] == {"type": "mute", "on": True}
    assert commands(tmp_path)[3] == {"type": "pause", "on": True}
    # phase, dropped and dc_error reach the daemon; levels do not; our own stop is not forwarded
    assert ("phase", "speaking") in forwarded and ("dropped", "3") in forwarded and ("dc_error", "boom") in forwarded
    assert not [f for f in forwarded if f[0] in {"levels", "closed"}]
    assert any(e["type"] == "levels" for e in seen)


async def test_stdin_eof_closes_the_helper_and_the_mic(fake_binary):
    helper = LiveHelper()
    await helper.offer()
    helper.process.stdin.close()
    await asyncio.wait_for(helper.process.wait(), 5)
    await asyncio.sleep(0.1)
    assert helper.closed_reason == "stdin_eof" and not helper.running
    with pytest.raises(LiveHelperError):
        await helper.mute(True)
    await helper.stop()


async def test_helper_crash_is_forwarded_as_closed(fake_binary, monkeypatch):
    monkeypatch.setenv("FAKE_LIVE_MODE", "crash_after_ready")
    forwarded = []

    async def forward(kind, detail):
        forwarded.append((kind, detail))

    helper = LiveHelper(forward=forward)
    await helper.offer()
    await helper.answer("a")
    await asyncio.sleep(0.3)
    assert not helper.running and helper.closed_reason == "helper_exited"
    await helper.stop()


async def test_close_during_offer_reports_the_reason_and_fix(fake_binary, monkeypatch):
    monkeypatch.setenv("FAKE_LIVE_MODE", "close_on_offer")
    helper = LiveHelper()
    with pytest.raises(LiveHelperError) as info:
        await helper.offer()
    assert info.value.code == "audio_error" and "permission denied" in str(info.value)
    assert "Microphone" in info.value.hint
    await helper.stop()


async def test_silent_helper_times_out(fake_binary, monkeypatch):
    monkeypatch.setenv("FAKE_LIVE_MODE", "silent")
    helper = LiveHelper()
    with pytest.raises(LiveHelperError) as info:
        await helper.offer(timeout=0.3)
    assert info.value.code == "timeout"
    await helper.stop()


async def test_overlong_lines_are_dropped_and_counted(fake_binary, monkeypatch):
    monkeypatch.setenv("FAKE_LIVE_MODE", "flood")
    helper = LiveHelper()
    assert await helper.offer() == "v=0\r\nfake-offer"  # the 400 KB line did not poison the stream
    assert helper.overlong_lines == 1
    await helper.stop()


async def test_oversized_offer_is_refused_locally(fake_binary, monkeypatch):
    # Prevent the fake helper's valid offer racing the injected oversized one.
    monkeypatch.setenv("FAKE_LIVE_MODE", "silent")
    helper = LiveHelper()

    # simulate a helper whose offer exceeds the host limit
    async def offer_event():
        await asyncio.sleep(0.05)
        helper._handle({"type": "offer", "sdp": "v" * (lh.MAX_OFFER + 1)})

    await helper.start()
    task = asyncio.ensure_future(offer_event())
    with pytest.raises(LiveHelperError) as info:
        await helper.offer()
    assert info.value.code == "bad_offer"
    await task
    await helper.stop()


async def test_call_handshake_mute_and_stop(fake_binary, tmp_path):
    client = FakeClient()
    call = LiveCall(client, "s1")
    result = await call.start(voice="cove", new_conversation=True)
    assert result.call_id == "c1" and call.helper.ready
    assert client.calls[0] == ("start", "s1", "v=0\r\nfake-offer", "cove", True)
    state = await call.mute(None)
    assert state.muted and call.helper.muted
    state = await call.pause(None)
    assert state.paused and call.helper.paused
    state = await call.pause(False)
    assert not state.paused and not call.helper.paused
    await asyncio.sleep(0.2)
    assert ("event", "dropped", "3") in client.calls
    await call.stop("user")
    assert ("stop", "s1", "user") in client.calls and not call.helper.running


async def test_failed_start_releases_the_helper_and_the_daemon_call(fake_binary):
    client = FakeClient(fail_start=RuntimeError("busy"))
    call = LiveCall(client, "s1")
    with pytest.raises(RuntimeError):
        await call.start()
    assert not call.helper.running
    assert not [c for c in client.calls if c[0] == "stop"]  # the daemon never accepted

    client = FakeClient()
    call = LiveCall(client, "s1", helper=LiveHelper())
    call.helper.answer = _boom  # the peer fails after the daemon accepted the call
    with pytest.raises(LiveHelperError):
        await call.start()
    assert ("stop", "s1", "helper_failed") in client.calls and not call.helper.running


async def _boom(*args, **kwargs):
    raise LiveHelperError("timeout", "no ready")


async def test_cancelled_start_releases_helper_and_accepted_call(fake_binary):
    client = FakeClient()
    call = LiveCall(client, "s1")
    answering = asyncio.Event()

    async def wait_for_answer(*args, **kwargs):
        answering.set()
        await asyncio.Event().wait()

    call.helper.answer = wait_for_answer
    task = asyncio.create_task(call.start())
    await asyncio.wait_for(answering.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("stop", "s1", "helper_failed") in client.calls
    assert not call.helper.running
