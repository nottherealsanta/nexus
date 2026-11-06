import subprocess

import pytest

from nexus import searchservice


@pytest.fixture
def home_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
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
