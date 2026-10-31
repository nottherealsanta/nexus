import json
import os
import socket
import subprocess
import sys

import pytest

from nexus import daemon


@pytest.fixture
def home_dir(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("NEXUS10_HOME", str(path))
    monkeypatch.setenv("NEXUS_PORT", str(_free_port()))
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listener(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(4)
    return sock


def _write(data: dict) -> None:
    daemon.info_path().write_text(json.dumps(data))


def test_an_unreadable_file_is_not_a_daemon(home_dir):
    daemon.info_path().write_text("{not json")
    assert daemon.discover() is None
    assert daemon._registered_pid() is None


def test_stop_does_not_block_the_next_start_on_a_stale_file(home_dir):
    _write({"url": "http://127.0.0.1:1", "pid": 999_999_999, "token": "t"})
    assert daemon.stop() is False
    assert not daemon.info_path().exists()


def _fake_daemon() -> subprocess.Popen:
    """A process whose command line looks like `nexus serve`."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "nexus", "serve"])


def test_stop_kills_the_registered_pid_even_when_it_does_not_answer(home_dir):
    child = _fake_daemon()
    try:
        _write({"url": "http://127.0.0.1:1", "pid": child.pid, "token": "t"})
        assert daemon.stop() is True
        assert child.wait(timeout=5) is not None
        assert not daemon.info_path().exists()
    finally:
        child.kill()
        child.wait()


def test_a_daemon_that_cannot_bind_leaves_the_running_daemons_file_alone(home_dir):
    port = int(os.environ["NEXUS_PORT"])
    holder = _listener(port)
    try:
        running = {"url": f"http://127.0.0.1:{port}", "pid": os.getpid(), "token": "kept"}
        _write(running)
        with pytest.raises(SystemExit) as failed:
            daemon.serve(port)
        assert str(port) in str(failed.value)
        assert json.loads(daemon.info_path().read_text()) == running
    finally:
        holder.close()


def test_start_names_an_unregistered_process_holding_the_port(home_dir):
    port = int(os.environ["NEXUS_PORT"])
    holder = _listener(port)
    try:
        with pytest.raises(OSError, match=f"127.0.0.1:{port} is already in use"):
            daemon.start()
    finally:
        holder.close()


def test_start_waits_for_a_registered_daemon_that_is_still_starting(home_dir, monkeypatch):
    """A daemon that took the port but is still building its runtime must not be duplicated."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write({"url": "http://127.0.0.1:1", "pid": child.pid, "token": "t"})
        monkeypatch.setattr(daemon, "_is_daemon", lambda pid: True)
        monkeypatch.setattr(daemon, "START_TIMEOUT_S", 0.4)
        spawned = []
        monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: spawned.append(a) or child)
        with pytest.raises(OSError, match="did not start"):
            daemon.start()
        assert spawned == []
    finally:
        child.kill()
        child.wait()
