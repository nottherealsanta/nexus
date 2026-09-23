"""Phase 8b2 end-to-end: the CLI surface over a real daemon subprocess.

These tests exercise the *canonical* wire. The UI client is no longer a second,
incompatible framing: :mod:`nexus.ui.cli.uds` adapts
:class:`nexus.host.transports.uds.UDSClient`, and auto-start is
:func:`nexus.host.ensure_daemon`, which launches ``python -m nexus.host.daemon``.
Every test here starts that real subprocess around an offline
:class:`~nexus.model.providers.scripted.ScriptedProvider`, then drives the
surface API exactly as ``nexus run`` / ``nexus chat`` would.

Covered: one-shot (human and JSONL), interactive chat, reconnect from the last
rendered ``seq``, first-responder approval across two clients, a loud version
mismatch that is never retried, Ctrl-C cancellation of the daemon-side turn, a
zero-view turn that survives its client, and two clients sharing one session.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.host import PROTOCOL_VERSION
from nexus.ui.cli import (
    Approver,
    Client,
    ProtocolVersionError,
    open_client,
    run_chat,
    run_once,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The daemon the UI tests auto-start. Its provider is selected by
#: ``NEXUS_UI_SCRIPT`` and its per-call delay by ``NEXUS_UI_DELAY``, so one
#: driver serves the text, tool-approval, slow, and zero-view cases.
DAEMON_DRIVER = r'''
import argparse
import asyncio
import itertools
import os
from pathlib import Path

from nexus.config import Config
from nexus.config.schema import (
    AgentSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection,
)
from nexus.host.daemon import Daemon
from nexus.model.providers.scripted import (
    ScriptedProvider, text_response, tool_response,
)
from nexus.runtime import Runtime

DELAY = float(os.environ.get("NEXUS_UI_DELAY", "0"))
SCRIPT = os.environ.get("NEXUS_UI_SCRIPT", "text")
FAIL_START = os.environ.get("NEXUS_UI_FAIL_START") == "1"

#: Each scripted text response is numbered so a test can prove a later run saw
#: its *own* turn rather than a replay of an earlier one.
COUNTER = itertools.count(1)


def _step(text):
    async def run(request):
        if DELAY:
            await asyncio.sleep(DELAY)
        return text_response(f"{text}{next(COUNTER)}")
    return run


def make_runtime(workspace, **kwargs):
    if SCRIPT == "tool":
        call = tool_response(("c1", "Write", {"path": "b.txt", "content": "B"}))
        scripts = []
        for _ in range(32):
            scripts.append(call)
            scripts.append(text_response("done"))
        provider = ScriptedProvider(*scripts)
    else:
        provider = ScriptedProvider(*([[_step("pong")]] * 64))
    config = Config(model="scripted/m", version=2, v2=ConfigV2(
        model=ModelSection(default="scripted/m"),
        agent=AgentSection(profile="coding"),
        permissions=PermissionsSection(mode="ask", on_unattended="deny"),
        tools=ToolsSection(),
    ))
    runtime = Runtime(workspace, config=config, providers={"scripted": provider})
    if FAIL_START:
        original = runtime.session

        def session(session_id, **kw):
            handle = original(session_id, **kw)

            async def boom(*args, **kwargs):
                raise RuntimeError("provider unavailable")

            handle.start_turn = boom
            return handle

        runtime.session = session
    return runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--socket", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--idle-timeout", type=float, default=300.0)
    parser.add_argument("--max-concurrent-turns", type=int, default=4)
    args = parser.parse_args()
    daemon = Daemon(
        Path(args.workspace), home=args.home, socket_path=args.socket,
        idle_timeout=args.idle_timeout,
        max_concurrent_turns=args.max_concurrent_turns,
        runtime_factory=make_runtime,
    )
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: A separate process that runs one surface turn, so a real ``SIGINT`` can be
#: delivered to it exactly as an interactive user would.
UI_DRIVER = r'''
import asyncio
import sys
from pathlib import Path

from nexus.ui.cli import open_client, run_once


async def main():
    workspace = Path(sys.argv[1])
    socket = sys.argv[2]
    client = await open_client(workspace, socket_path=socket, timeout=15.0)
    try:
        return await run_once(client, session="s", content="slow")
    finally:
        await client.aclose()


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 130
    print(f"EXIT={code}", flush=True)
    sys.exit(0)
'''


@pytest.fixture
def short_dir():
    """A short socket directory; macOS caps a Unix socket path near 104 bytes."""
    path = Path(tempfile.mkdtemp(prefix="nexus-ui-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def ui_env(short_dir):
    workspace = short_dir / "ws"
    workspace.mkdir()
    driver = short_dir / "daemon_driver.py"
    driver.write_text(DAEMON_DRIVER, encoding="utf-8")
    ui_driver = short_dir / "ui_driver.py"
    ui_driver.write_text(UI_DRIVER, encoding="utf-8")
    processes: list[subprocess.Popen] = []

    def spawn(
        workspace_,
        path,
        *,
        home=None,
        idle_timeout=None,
        max_concurrent_turns=None,
        environ=None,
    ):
        argv = [
            sys.executable,
            str(driver),
            "--workspace",
            str(workspace_),
            "--socket",
            str(path),
        ]
        if idle_timeout is not None:
            argv += ["--idle-timeout", str(idle_timeout)]
        if max_concurrent_turns is not None:
            argv += ["--max-concurrent-turns", str(max_concurrent_turns)]
        process = subprocess.Popen(
            argv,
            cwd=str(workspace_),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **dict(environ or {})},
        )
        processes.append(process)
        return process

    yield SimpleNamespace(
        workspace=workspace,
        socket=short_dir / "daemon.sock",
        driver=driver,
        ui_driver=ui_driver,
        spawn=spawn,
        processes=processes,
    )

    for process in processes:
        if process.poll() is None:
            process.terminate()
            with contextlib.suppress(Exception):
                process.wait(timeout=5)


async def wait_for(predicate, timeout=10.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout)


async def wait_for_async(predicate, timeout=10.0):
    async def _wait():
        while not await predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout)


async def open_ui(ui_env, *, script="text", delay=0.0, fail_start=False, **kwargs) -> Client:
    environ = {
        "NEXUS_UI_SCRIPT": script,
        "NEXUS_UI_DELAY": str(delay),
        "NEXUS_UI_FAIL_START": "1" if fail_start else "0",
    }
    return await open_client(
        ui_env.workspace,
        socket_path=ui_env.socket,
        spawn=ui_env.spawn,
        timeout=15.0,
        environ=environ,
        **kwargs,
    )


def scripted_reader(*lines):
    iterator = iter(lines)

    async def read(prompt: str) -> str:
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    return read


async def collect_until(stream, terminal: str, timeout: float = 15.0):
    """Drain ``stream`` until ``terminal``; return every event seen."""
    events = []

    async def _collect():
        async for event in stream:
            events.append(event)
            if event.type == terminal:
                return

    try:
        await asyncio.wait_for(_collect(), timeout)
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()
    return events


# ---------------------------------------------------------------------------
# One-shot and chat
# ---------------------------------------------------------------------------


async def test_one_shot_human(ui_env):
    client = await open_ui(ui_env)
    out, err = _buffers()
    try:
        code = await run_once(
            client,
            session="s",
            content="hi",
            stdout=out,
            stderr=err,
            approver=Approver(scripted_reader("y"), stderr=err),
        )
    finally:
        await client.aclose()
    assert code == 0
    assert "pong" in out.getvalue()


async def test_one_shot_approval_round_trip(ui_env):
    client = await open_ui(ui_env, script="tool")
    out, err = _buffers()
    try:
        code = await run_once(
            client,
            session="s",
            content="go",
            stdout=out,
            stderr=err,
            approver=Approver(scripted_reader("y"), stderr=err),
        )
    finally:
        await client.aclose()
    assert code == 0
    assert "done" in out.getvalue()
    assert (ui_env.workspace / "b.txt").read_text(encoding="utf-8") == "B"


async def test_one_shot_jsonl_emits_envelopes(ui_env):
    import json

    client = await open_ui(ui_env)
    out, err = _buffers()
    try:
        code = await run_once(
            client, session="s", content="hi", stdout=out, stderr=err, json_output=True
        )
    finally:
        await client.aclose()
    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [item["type"] for item in lines][-1] == "turn.completed"
    assert any(item["type"] == "text.delta" for item in lines)


async def test_chat_round_trip(ui_env):
    client = await open_ui(ui_env)
    out, err = _buffers()
    reader = scripted_reader("hello", "/exit")
    try:
        code = await run_chat(
            client, session="s", reader=reader, stdout=out, stderr=err
        )
    finally:
        await client.aclose()
    assert code == 0
    assert "pong" in out.getvalue()


async def test_sequential_run_once_on_one_session(ui_env):
    client = await open_ui(ui_env)
    first, second = _buffers(), _buffers()
    try:
        code_one = await run_once(
            client, session="s", content="one", stdout=first[0], stderr=first[1]
        )
        code_two = await run_once(
            client, session="s", content="two", stdout=second[0], stderr=second[1]
        )
    finally:
        await client.aclose()
    assert code_one == 0 and code_two == 0
    # Each numbered response proves the run observed its own turn; the second
    # run must not have exited on a replayed first-turn terminal event.
    assert "pong1" in first[0].getvalue()
    assert "pong2" in second[0].getvalue()
    assert "pong1" not in second[0].getvalue()


async def test_sequential_chat_turns_on_one_session(ui_env):
    client = await open_ui(ui_env)
    out, err = _buffers()
    reader = scripted_reader("one", "two", "/exit")
    try:
        code = await run_chat(
            client, session="s", reader=reader, stdout=out, stderr=err
        )
    finally:
        await client.aclose()
    assert code == 0
    text = out.getvalue()
    assert "pong1" in text and "pong2" in text


async def test_resumed_session_does_not_replay_across_reconnect(ui_env):
    setup = await open_ui(ui_env)
    first = _buffers()
    try:
        code = await run_once(
            setup, session="s", content="first", stdout=first[0], stderr=first[1]
        )
        assert code == 0 and "pong1" in first[0].getvalue()
    finally:
        await setup.aclose()

    resumed = await open_ui(ui_env)
    second = _buffers()
    try:
        code = await run_once(
            resumed, session="s", content="second", stdout=second[0], stderr=second[1]
        )
    finally:
        await resumed.aclose()
    assert code == 0
    assert "pong2" in second[0].getvalue()
    assert "pong1" not in second[0].getvalue()


async def test_start_failure_terminates_run_and_jsonl(ui_env):
    import json

    client = await open_ui(ui_env, fail_start=True)
    out, err = _buffers()
    try:
        code = await run_once(
            client, session="s", content="hi", stdout=out, stderr=err, json_output=True
        )
    finally:
        await client.aclose()
    assert code == 1
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    failed = [item for item in lines if item["type"] == "turn.failed"]
    assert failed
    assert "provider unavailable" in failed[-1]["data"]["error"]


async def test_start_failure_terminates_chat(ui_env):
    client = await open_ui(ui_env, fail_start=True)
    out, err = _buffers()
    reader = scripted_reader("hi", "/exit")
    try:
        code = await run_chat(
            client, session="s", reader=reader, stdout=out, stderr=err
        )
    finally:
        await client.aclose()
    assert code == 0
    assert "provider unavailable" in err.getvalue()


# ---------------------------------------------------------------------------
# Reconnect and views
# ---------------------------------------------------------------------------


async def test_reconnect_from_the_last_seq(ui_env):
    client = await open_ui(ui_env)
    try:
        await client.open_session("s")
        first = client.stream("s", 0, follow=True)
        task = asyncio.create_task(collect_until(first, "turn.completed"))
        await wait_for_async(lambda: _viewers(client, "s", 1))
        await client.start_turn("s", "go")
        events = await task
        cursor = max(event.seq for event in events)
        assert cursor > 0
        await client.aclose()

        second = await open_ui(ui_env)
        try:
            await second.open_session("s")
            tail = second.stream("s", cursor, follow=True)
            task = asyncio.create_task(collect_until(tail, "turn.completed"))
            await wait_for_async(lambda: _viewers(second, "s", 1))
            await second.start_turn("s", "again")
            seen = await task
            # No replayed turn-1 event: every sequenced event is past the cursor.
            assert all(event.seq > cursor for event in seen if event.seq)
            assert sum(event.type == "turn.started" for event in seen) == 1
        finally:
            await second.aclose()
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


async def test_zero_view_turn_survives_client_disconnect(ui_env):
    client = await open_ui(ui_env, delay=0.4)
    await client.open_session("s")
    await client.start_turn("s", "long")
    # No subscription was ever opened; the view now vanishes mid-turn.
    await client.aclose()

    await asyncio.sleep(1.2)
    late = await open_ui(ui_env)
    try:
        events = await collect_until(
            late.stream("s", 0, follow=False), "turn.completed"
        )
        assert any(event.type == "turn.completed" for event in events)
    finally:
        await late.aclose()


async def test_two_clients_share_one_session(ui_env):
    first = await open_ui(ui_env)
    second = await open_ui(ui_env)
    try:
        await first.open_session("s")
        stream_a = first.stream("s", 0, follow=True)
        stream_b = second.stream("s", 0, follow=True)

        async def collect(stream):
            return await collect_until(stream, "turn.completed")

        task_a = asyncio.create_task(collect(stream_a))
        task_b = asyncio.create_task(collect(stream_b))
        await wait_for_async(lambda: _viewers(first, "s", 2))
        await first.start_turn("s", "go")
        seen_a, seen_b = await asyncio.gather(task_a, task_b)
        for seen in (seen_a, seen_b):
            kinds = [event.type for event in seen]
            assert "text.delta" in kinds and "turn.completed" in kinds
    finally:
        await first.aclose()
        await second.aclose()


# ---------------------------------------------------------------------------
# Approval race
# ---------------------------------------------------------------------------


async def test_first_responder_approval_wins(ui_env):
    first = await open_ui(ui_env, script="tool")
    second = await open_ui(ui_env, script="tool")
    try:
        await first.open_session("s")
        requests: list[str] = []

        async def watch(client):
            # Stay attached until the turn ends: leaving on the request would
            # drop the last viewer and let the unattended policy answer first.
            async for event in client.stream("s", 0, follow=True):
                if event.type == "permission.requested":
                    requests.append(event.data["id"])
                if event.type == "turn.completed":
                    return

        task_a = asyncio.create_task(watch(first))
        task_b = asyncio.create_task(watch(second))
        await wait_for_async(lambda: _viewers(first, "s", 2))
        await first.start_turn("s", "go")
        await wait_for(lambda: len(requests) >= 1)
        request_id = requests[0]

        results = await asyncio.gather(
            first.resolve_permission("s", request_id, "allow_once"),
            second.resolve_permission("s", request_id, "deny_once"),
        )
        assert sorted(results) == [False, True]
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=15.0)
    finally:
        await first.aclose()
        await second.aclose()


# ---------------------------------------------------------------------------
# Version mismatch and Ctrl-C
# ---------------------------------------------------------------------------


async def test_version_mismatch_fails_loudly_and_is_not_retried(ui_env):
    control = await open_ui(ui_env)
    await control.aclose()
    spawns_before = len(ui_env.processes)

    with pytest.raises(ProtocolVersionError) as info:
        await open_client(
            ui_env.workspace,
            socket_path=ui_env.socket,
            spawn=ui_env.spawn,
            timeout=5.0,
            version=PROTOCOL_VERSION + 1,
        )
    assert info.value.expected == PROTOCOL_VERSION + 1
    assert info.value.actual == PROTOCOL_VERSION
    # A mismatch is a build problem, never a spawn-and-retry loop.
    assert len(ui_env.processes) == spawns_before


async def test_ctrl_c_cancels_the_daemon_turn(ui_env):
    control = await open_ui(ui_env, script="slow", delay=5.0)
    await control.aclose()  # leave the daemon running

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ui_env.ui_driver),
        str(ui_env.workspace),
        str(ui_env.socket),
        cwd=str(ui_env.workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await wait_for_async(lambda: _running(ui_env.workspace, ui_env.socket))
        process.send_signal(signal.SIGINT)
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        assert b"EXIT=130" in stdout

        probe = await open_ui(ui_env)
        try:
            events = await collect_until(
                probe.stream("s", 0, follow=False), "turn.cancelled"
            )
            assert any(event.type == "turn.cancelled" for event in events)
        finally:
            await probe.aclose()
    finally:
        if process.returncode is None:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _buffers():
    import io

    return io.StringIO(), io.StringIO()


async def _viewers(client: Client, session: str, expected: int) -> bool:
    """Whether ``session`` reports at least ``expected`` attached viewers."""
    summaries = await client.list_sessions()
    return any(
        getattr(summary, "id", "") == session
        and getattr(summary, "viewers", 0) >= expected
        for summary in summaries
    )


async def _running(workspace: Path, socket: Path) -> bool:
    """Whether the daemon has any turn in flight (a fresh control connection)."""
    client = await open_client(workspace, socket_path=socket, timeout=5.0)
    try:
        summaries = await client.list_sessions()
        return any(
            getattr(summary, "state", "idle") == "running" for summary in summaries
        )
    finally:
        await client.aclose()
