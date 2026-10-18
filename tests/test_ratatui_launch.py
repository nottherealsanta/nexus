"""Native client launch order: the process starts before the host connection (splash first)."""
from __future__ import annotations

import asyncio

from nexus.ui.ratatui import run as launch


class _Stdin:
    def __init__(self, process):
        self.process = process

    def close(self):
        self.process.returncode = 0  # the client restores the terminal and exits on EOF


class _Process:
    def __init__(self, *, exits_on_eof=True):
        self.returncode = None
        self.stdin = _Stdin(self) if exits_on_eof else type("S", (), {"close": lambda self: None})()
        self.terminated = False

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


async def test_release_closes_stdin_so_the_client_restores_the_terminal():
    process = _Process()
    await launch.release(process)
    assert process.returncode == 0 and not process.terminated


async def test_release_ignores_a_client_that_already_left():
    process = _Process()
    process.returncode = 0
    await launch.release(process)
    assert not process.terminated


class _Pipe:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data

    async def drain(self):
        pass


async def test_progress_writes_a_startup_line_for_the_splash():
    process = _Process()
    process.stdin = _Pipe()
    await launch.progress(process, 2)
    assert process.stdin.written == b'{"startup":2}\n'


async def test_progress_ignores_a_client_that_left_or_a_broken_pipe():
    gone = _Process()
    gone.returncode = 0
    gone.stdin = _Pipe()
    await launch.progress(gone, 2)
    assert gone.stdin.written == b""

    class Broken:
        def write(self, data):
            raise BrokenPipeError

    broken = _Process()
    broken.stdin = Broken()
    await launch.progress(broken, 2)  # must not raise
    await launch.progress(None, 2)


async def test_chat_reports_host_connected_to_the_splash(monkeypatch, tmp_path):
    from nexus import cli
    process = _Process()
    process.stdin = _Pipe()

    async def spawn():
        return process

    async def open_client(workspace):
        assert process.stdin.written == b"", "not before the host answers"

        class Client:
            async def aclose(self):
                pass

        return Client()

    async def run(client, **kwargs):
        assert process.stdin.written == b'{"startup":2}\n'
        return 0

    async def release(process):
        pass

    import nexus.ui.cli as ui_cli
    monkeypatch.setattr(launch, "spawn", spawn)
    monkeypatch.setattr(launch, "run", run)
    monkeypatch.setattr(launch, "release", release)
    monkeypatch.setattr(ui_cli, "open_client", open_client)
    assert await cli._chat(tmp_path, session="s") == 0


async def test_chat_spawns_the_client_before_connecting(monkeypatch, tmp_path):
    from nexus import cli
    order: list[str] = []
    process = _Process()

    async def spawn():
        order.append("spawn")
        return process

    async def open_client(workspace):
        order.append("connect")

        class Client:
            async def aclose(self):
                order.append("close")

        return Client()

    async def run(client, **kwargs):
        order.append("run")
        assert kwargs["process"] is process
        return 0

    import nexus.ui.cli as ui_cli
    monkeypatch.setattr(launch, "spawn", spawn)
    monkeypatch.setattr(launch, "run", run)
    monkeypatch.setattr(ui_cli, "open_client", open_client)
    assert await cli._chat(tmp_path, session="s") == 0
    assert order == ["spawn", "connect", "run", "close"]
    assert process.returncode == 0


def test_stderr_to_moves_native_writes_off_the_terminal_and_restores(tmp_path):
    """Native libraries (PortAudio) write to fd 2 directly; during the TUI that goes to a log."""
    import os

    log = tmp_path / "logs" / "chat-stderr.log"
    before = os.fstat(2)
    with launch.stderr_to(log):
        os.write(2, b"||PaMacCore (AUHAL)|| Warning\n")
    after = os.fstat(2)
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert "PaMacCore" in log.read_text()


def test_stderr_to_starts_over_past_its_bound(tmp_path):
    import os

    log = tmp_path / "chat-stderr.log"
    log.write_bytes(b"x" * (launch._STDERR_LOG_LIMIT + 1))
    with launch.stderr_to(log):
        os.write(2, b"fresh\n")
    assert log.read_text() == "fresh\n"
