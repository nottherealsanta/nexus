import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from nexus import searchservice


@pytest.fixture
def home_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(searchservice, "_json_forbidden", lambda url: False)
    searchservice._clear_start()
    return tmp_path


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(searchservice, "_runtime", lambda: "container")
    monkeypatch.setattr(searchservice, "runtime_status", lambda: {"kind": "container", "state": "ready", "detail": "Apple container"})


def _done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_packaged_settings_are_copied_into_the_home_and_kept(home_dir):
    folder = searchservice._ensure_config()
    assert folder == home_dir / "search"
    target = folder / "searxng" / "settings.yml"
    assert target.read_bytes() == searchservice.SETTINGS.read_bytes()
    assert "json" in target.read_text()
    assert folder.stat().st_mode & 0o077 == 0
    target.write_text("mine\n")
    searchservice._ensure_config()
    assert target.read_text() == "mine\n"  # a user's later edit wins


def test_running_when_the_endpoint_answers(home_dir, monkeypatch):
    monkeypatch.setattr(searchservice, "_answers", lambda url: True)
    assert searchservice.status()["state"] == "running"


def test_answers_checks_for_searxng_json(monkeypatch):
    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    monkeypatch.setattr(searchservice.httpx, "get", lambda *a, **kw: Response(200, {"results": []}))
    assert searchservice._answers("http://localhost/search")
    monkeypatch.setattr(searchservice.httpx, "get", lambda *a, **kw: Response(200, {"error": "no"}))
    assert not searchservice._answers("http://localhost/search")
    monkeypatch.setattr(searchservice.httpx, "get", lambda *a, **kw: Response(200, ValueError("html")))
    assert not searchservice._answers("http://localhost/search")


def test_a_remote_endpoint_cannot_be_started(home_dir, monkeypatch, ready):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_url", lambda: "https://search.example.com/search")
    status = searchservice.status()
    assert status["state"] == "unavailable"
    with pytest.raises(RuntimeError):
        searchservice.start()
    with pytest.raises(RuntimeError):
        searchservice.start_async()


def test_search_url_without_a_port_cannot_be_managed(home_dir, monkeypatch, ready):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_url", lambda: "http://127.0.0.1/search")
    assert searchservice.status()["state"] == "unavailable"
    assert "explicit loopback port" in searchservice.status()["detail"]


def test_stopped_absent_and_no_runtime(home_dir, monkeypatch):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_runtime", lambda: None)
    assert searchservice.status()["state"] == "unavailable"
    monkeypatch.setattr(searchservice, "_runtime", lambda: "container")
    monkeypatch.setattr(searchservice, "runtime_status", lambda: {"kind": "container", "state": "ready", "detail": None})
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: True)
    assert searchservice.status()["state"] == "stopped"
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    assert searchservice.status()["state"] == "absent"


def test_services_shape_and_runtime(home_dir, monkeypatch):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    monkeypatch.setattr(searchservice, "runtime_status", lambda: {"kind": "docker", "state": "ready", "detail": "Docker"})
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    found = searchservice.services()
    assert found["runtime"] == {"kind": "docker", "state": "ready", "detail": "Docker"}
    row = found["services"][0]
    assert row == {"id": "search", "label": "Web search", "state": "absent",
                   "url": searchservice._url(), "detail": None}


def test_docker_without_a_daemon_is_unavailable(home_dir, monkeypatch):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    monkeypatch.setattr(searchservice, "runtime_status", lambda: {
        "kind": "docker", "state": "stopped", "detail": "Docker is installed but its daemon is not running"})
    status = searchservice.status()
    assert status["state"] == "unavailable" and "daemon is not running" in status["detail"]
    with pytest.raises(RuntimeError, match="daemon is not running"):
        searchservice.start_async()


def test_runtime_prefers_apple_container(monkeypatch):
    monkeypatch.setattr(searchservice.shutil, "which", lambda name: f"/bin/{name}")
    assert searchservice._runtime() == "container"


def test_runtime_status_reports_apple_and_docker(monkeypatch):
    monkeypatch.setattr(searchservice, "_runtime", lambda: "container")
    monkeypatch.setattr(searchservice, "_run", lambda *a, cwd=None, timeout=None: _done(0))
    assert searchservice.runtime_status() == {"kind": "container", "state": "ready", "detail": "Apple container"}
    monkeypatch.setattr(searchservice, "_run", lambda *a, cwd=None, timeout=None: _done(1))
    stopped = searchservice.runtime_status()
    assert stopped["kind"] == "container" and stopped["state"] == "stopped" and "Nexus starts it" in stopped["detail"]
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    assert searchservice.runtime_status()["state"] == "stopped"
    monkeypatch.setattr(searchservice, "_run", lambda *a, cwd=None, timeout=None: _done(0))
    assert searchservice.runtime_status() == {"kind": "docker", "state": "ready", "detail": "Docker"}


def test_missing_packaged_settings_are_unavailable(home_dir, monkeypatch, tmp_path, ready):
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "SETTINGS", tmp_path / "missing.yml")
    found = searchservice.status()
    assert found["state"] == "unavailable" and "packaged SearXNG settings are missing" in found["detail"]


def test_start_runs_the_apple_container_and_waits(home_dir, monkeypatch, tmp_path, ready):
    folder = tmp_path / "search"
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    answers = iter([False, False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    monkeypatch.setattr(searchservice.time, "sleep", lambda s: None)
    calls = []

    def run(*args, cwd=None, timeout=None):
        calls.append(args)
        if args[1:3] == ("image", "inspect"):
            return _done(1)
        return _done()

    monkeypatch.setattr(searchservice, "_run", run)
    assert searchservice.start()["state"] == "running"
    assert ("container", "image", "pull", searchservice.DEFAULT_IMAGE) in calls
    run_call = next(a for a in calls if a[1] == "run")
    assert run_call[-1] == searchservice.DEFAULT_IMAGE
    assert "127.0.0.1:18765:8080" in run_call
    assert "--env-file" in run_call and str(folder / ".env") in run_call
    secret = (folder / ".env").read_text()
    assert secret.startswith("SEARXNG_SECRET=") and (folder / ".env").stat().st_mode & 0o077 == 0
    assert (folder / "searxng" / "settings.yml").read_text() == searchservice.SETTINGS.read_text()


def test_start_uses_docker_run_with_loopback_and_restart(home_dir, monkeypatch, tmp_path):
    folder = tmp_path / "search"
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    monkeypatch.setattr(searchservice, "runtime_status", lambda: {"kind": "docker", "state": "ready", "detail": "Docker"})
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    answers = iter([False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    monkeypatch.setattr(searchservice.time, "sleep", lambda s: None)
    calls = []

    def run(*args, cwd=None, timeout=None):
        calls.append(args)
        if args[1:3] == ("container", "inspect"):
            return _done(1)
        return _done()

    monkeypatch.setattr(searchservice, "_run", run)
    assert searchservice.start()["state"] == "running"
    run_call = next(a for a in calls if a[1] == "run")
    assert run_call[:2] == ("docker", "run")
    assert "127.0.0.1:18765:8080" in run_call
    assert run_call[run_call.index("--restart") + 1] == "unless-stopped"
    assert not any(a[1] == "compose" for a in calls)


def test_port_comes_from_the_search_url(home_dir, monkeypatch, tmp_path):
    folder = tmp_path / "search"
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    monkeypatch.setattr(searchservice, "runtime_status", lambda: {"kind": "docker", "state": "ready", "detail": None})
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    monkeypatch.setattr(searchservice, "_url", lambda: "http://127.0.0.1:19999/search")
    answers = iter([False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    monkeypatch.setattr(searchservice.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(searchservice, "_run", lambda *a, cwd=None, timeout=None: calls.append(a) or _done())
    assert searchservice.start()["state"] == "running"
    run_call = next(a for a in calls if a[1] == "run")
    assert "127.0.0.1:19999:8080" in run_call
    assert "SEARXNG_BASE_URL=http://127.0.0.1:19999/" in run_call


def test_a_failed_launch_reports_the_last_line(home_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(searchservice, "directory", lambda: tmp_path)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    monkeypatch.setattr(searchservice, "runtime_status", lambda: {"kind": "docker", "state": "ready", "detail": None})
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)

    def run(*args, cwd=None, timeout=None):
        if args[1] == "run":
            return _done(1, err="x\nCannot connect to the Docker daemon")
        return _done()

    monkeypatch.setattr(searchservice, "_run", run)
    with pytest.raises(RuntimeError, match="Docker daemon"):
        searchservice.start()
    assert "Docker daemon" in searchservice.status()["detail"]


def test_forbidden_existing_service_is_not_replaced(home_dir, monkeypatch, tmp_path, ready):
    monkeypatch.setattr(searchservice, "directory", lambda: tmp_path)
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)
    monkeypatch.setattr(searchservice, "_json_forbidden", lambda url: True)
    monkeypatch.setattr(searchservice, "_launch", lambda *args: pytest.fail("must not replace the service"))
    with pytest.raises(RuntimeError, match="HTTP 403.*search.formats.*limiter"):
        searchservice.start()


def test_json_forbidden_detects_http_status(monkeypatch):
    monkeypatch.setattr(searchservice.httpx, "get", lambda *a, **kw: type("Response", (), {"status_code": 403})())
    assert searchservice._json_forbidden("http://localhost:18765/search")


def test_an_apple_container_with_a_moved_mount_is_recreated(home_dir, monkeypatch, tmp_path, ready):
    folder = tmp_path / "search"
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: True)
    answers = iter([False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    monkeypatch.setattr(searchservice.time, "sleep", lambda s: None)
    calls = []

    def run(*a, cwd=None, timeout=None):
        calls.append(a)
        if a[1] == "inspect":
            return _done(out='[{"configuration": {"mounts": [{"source": "/old/place/searxng"}]}}]')
        return _done()

    monkeypatch.setattr(searchservice, "_run", run)
    assert searchservice.start()["state"] == "running"
    verbs = [a[1] for a in calls]
    assert verbs.index("delete") < verbs.index("run") and "start" not in verbs[verbs.index("inspect"):]


def test_a_docker_container_with_a_moved_mount_is_recreated(home_dir, monkeypatch, tmp_path):
    folder = tmp_path / "search"
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_runtime", lambda: "docker")
    monkeypatch.setattr(searchservice, "runtime_status", lambda: {"kind": "docker", "state": "ready", "detail": None})
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: True)
    answers = iter([False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    monkeypatch.setattr(searchservice.time, "sleep", lambda s: None)
    calls = []

    def run(*a, cwd=None, timeout=None):
        calls.append(a)
        if a[1:3] == ("container", "inspect"):
            return _done(out='[{"Mounts": [{"Source": "/old/place/searxng"}]}]')
        return _done()

    monkeypatch.setattr(searchservice, "_run", run)
    assert searchservice.start()["state"] == "running"
    verbs = [a[1] for a in calls]
    assert "rm" in verbs and verbs.index("rm") < verbs.index("run")


def test_existing_container_is_found_after_starting_container_system(home_dir, monkeypatch, tmp_path, ready):
    monkeypatch.setattr(searchservice, "directory", lambda: tmp_path)
    answers = iter([False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    system_running = False
    calls = []

    def run(*args, cwd=None, timeout=None):
        nonlocal system_running
        calls.append(args)
        if args[1:3] == ("system", "status"):
            return _done(0 if system_running else 1)
        if args[1:3] == ("system", "start"):
            system_running = True
        if args[1] == "inspect":
            if not system_running:
                return _done(1, err="system not running")
            return _done(out=f'[{{"configuration": {{"mounts": [{{"source": "{tmp_path}/searxng"}}], '
                             '"publishedPorts": [{"containerPort": 8080, "hostPort": 18765}]}}]')
        return _done()

    monkeypatch.setattr(searchservice, "_run", run)
    assert searchservice.start()["state"] == "running"
    assert ("container", "start", "nexus-search") in calls
    assert not any(args[1] in ("run", "delete") for args in calls)


def test_a_container_published_on_another_port_is_recreated(home_dir, monkeypatch, tmp_path, ready):
    folder = tmp_path / "search"
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_url", lambda: "http://127.0.0.1:19999/search")
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: True)
    answers = iter([False, True, True])
    monkeypatch.setattr(searchservice, "_answers", lambda url: next(answers))
    monkeypatch.setattr(searchservice.time, "sleep", lambda s: None)
    calls = []

    def run(*a, cwd=None, timeout=None):
        calls.append(a)
        if a[1] == "inspect":
            return _done(out=f'[{{"configuration": {{"mounts": [{{"source": "{folder}/searxng"}}], '
                             '"publishedPorts": [{"containerPort": 8080, "hostPort": 18765}]}}]')
        return _done()

    monkeypatch.setattr(searchservice, "_run", run)
    assert searchservice.start()["state"] == "running"
    verbs = [a[1] for a in calls]
    assert "delete" in verbs, calls
    assert verbs.index("delete") < verbs.index("run")
    run_call = next(a for a in calls if a[1] == "run")
    assert "127.0.0.1:19999:8080" in run_call


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


def test_async_start_returns_immediately_and_reports_starting(home_dir, monkeypatch, tmp_path, ready):
    folder = tmp_path / "search"
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    running = False
    launched = Event()
    release = Event()

    def launch(*args):
        launched.set()
        assert release.wait(2)

    def wait(url):
        nonlocal running
        running = True

    monkeypatch.setattr(searchservice, "_launch", launch)
    monkeypatch.setattr(searchservice, "_wait", wait)
    monkeypatch.setattr(searchservice, "_answers", lambda url: running)
    started = time.monotonic()
    row = searchservice.start_async()
    assert time.monotonic() - started < 1
    assert row["state"] == "starting"
    assert launched.wait(2)
    assert searchservice.status()["state"] == "starting"
    assert searchservice.service_row()["state"] == "starting"
    release.set()
    _wait_for(lambda: searchservice.status()["state"] == "running")
    assert searchservice.status()["detail"] is None


def test_async_start_failure_is_kept_in_the_status_detail(home_dir, monkeypatch, tmp_path, ready):
    folder = tmp_path / "search"
    monkeypatch.setattr(searchservice, "directory", lambda: folder)
    monkeypatch.setattr(searchservice, "_exists", lambda runtime: False)
    monkeypatch.setattr(searchservice, "_answers", lambda url: False)

    def launch(*args):
        raise RuntimeError("Docker is installed but its daemon is not running")

    monkeypatch.setattr(searchservice, "_launch", launch)
    searchservice.start_async()
    _wait_for(lambda: searchservice.status()["state"] != "starting")
    found = searchservice.status()
    assert found["state"] == "absent"
    assert "daemon is not running" in found["detail"]
