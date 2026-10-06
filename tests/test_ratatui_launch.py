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
