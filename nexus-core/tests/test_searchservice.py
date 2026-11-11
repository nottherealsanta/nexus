import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from nexus import searchservice


@pytest.fixture
def home_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(searchservice, "_json_forbidden", lambda url: False)
    return tmp_path


def _done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def test_running_when_the_endpoint_answers(home_dir, monkeypatch):
    monkeypatch.setattr(searchservice, "_answers", lambda url: True)
    assert searchservice.status()["state"] == "running"


def test_a_remote_endpoint_cannot_be_started(home_dir, monkeypatch):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_url", lambda: "https://search.example.com/search")
    status = searchservice.status()
    assert status["state"] == "unavailable"
    with pytest.raises(RuntimeError):
        searchservice.start()


def test_stopped_absent_and_no_runtime(home_dir, monkeypatch):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_runtime", lambda: None)
    assert searchservice.status()["state"] == "unavailable"
    monkeypatch.setattr(searchservice, "_runtime", lambda: "container")
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: True)
    assert searchservice.status()["state"] == "stopped"
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    assert searchservice.status()["state"] == "absent"


def test_start_runs_the_container_and_waits(home_dir, monkeypatch, tmp_path):
    folder = tmp_path / "nexus-search"
    folder.mkdir()
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "container")
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    answers = iter([False, False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    monkeypatch.setattr(searchservice.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(searchservice, "_run", lambda *a, cwd=None: calls.append(a) or _done())
    assert searchservice.start()["state"] == "running"
    run = next(a for a in calls if a[1] == "run")
    assert run[-1] == searchservice.DEFAULT_IMAGE
    secret = (folder / ".env").read_text()
    assert secret.startswith("SEARXNG_SECRET=") and (folder / ".env").stat().st_mode & 0o077 == 0


def test_a_failed_launch_reports_the_last_line(home_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(searchservice, "directory", lambda: tmp_path)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_run", lambda *a, cwd=None: _done(1, err="x\nCannot connect to the Docker daemon"))
    with pytest.raises(RuntimeError, match="Docker daemon"):
        searchservice.start()


def test_forbidden_existing_service_is_not_replaced(home_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(searchservice, "directory", lambda: tmp_path)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_json_forbidden", lambda url: True)
    monkeypatch.setattr(searchservice, "_launch", lambda *args: pytest.fail("must not replace the service"))
    with pytest.raises(RuntimeError, match="HTTP 403.*search.formats.*limiter"):
        searchservice.start()


def test_json_forbidden_detects_http_status(monkeypatch):
    monkeypatch.setattr(searchservice.httpx, "get", lambda *a, **kw: type("Response", (), {"status_code": 403})())
    assert searchservice._json_forbidden("http://localhost:18765/search")


def test_a_container_with_a_moved_mount_is_recreated(home_dir, monkeypatch, tmp_path):
    folder = tmp_path / "nexus-search"
    folder.mkdir()
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "container")
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: True)
    answers = iter([False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    monkeypatch.setattr(searchservice.time, "sleep", lambda s: None)
    calls = []

    def run(*a, cwd=None):
        calls.append(a)
        if a[1] == "inspect":
            return _done(out='[{"configuration": {"mounts": [{"source": "/old/place/searxng"}]}}]')
        return _done()
    monkeypatch.setattr(searchservice, "_run", run)
    assert searchservice.start()["state"] == "running"
    verbs = [a[1] for a in calls]
    assert verbs.index("delete") < verbs.index("run") and "start" not in verbs[verbs.index("inspect"):]


def test_existing_container_is_found_after_starting_container_system(home_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(searchservice, "directory", lambda: tmp_path)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "container")
    answers = iter([False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    system_running = False
    calls = []

    def run(*args, cwd=None):
        nonlocal system_running
        calls.append(args)
        if args[1:3] == ("system", "status"):
            return _done(0 if system_running else 1)
        if args[1:3] == ("system", "start"):
            system_running = True
        if args[1] == "inspect":
            if not system_running:
                return _done(1, err="system not running")
            return _done(out=f'[{{"configuration": {{"mounts": [{{"source": "{tmp_path}/searxng"}}]}}}}]')
        return _done()

    monkeypatch.setattr(searchservice, "_run", run)
    assert searchservice.start()["state"] == "running"
    assert ("container", "start", "nexus-search") in calls
    assert not any(args[1] in ("run", "delete") for args in calls)


def test_concurrent_start_requests_launch_once(home_dir, monkeypatch):
    launching = Event()
    release = Event()
    running = False
    calls = []

    def status():
        return {"state": "running" if running else "absent", "url": "http://localhost/search", "runtime": "container"}

    def launch(*args):
        nonlocal running
        calls.append(args)
        launching.set()
        assert release.wait(2)
        running = True

    monkeypatch.setattr(searchservice, "status", status)
    monkeypatch.setattr(searchservice, "_launch", launch)
    monkeypatch.setattr(searchservice, "_answers", lambda url: running)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(searchservice.start)
        assert launching.wait(2)
        second = pool.submit(searchservice.start)
        release.set()
        assert first.result()["state"] == second.result()["state"] == "running"
    assert len(calls) == 1
