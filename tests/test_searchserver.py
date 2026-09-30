"""Local search service startup and persistent configuration."""
import io
from types import SimpleNamespace

from nexus import cli
from nexus.host_support import searchserver


def test_start_preserves_secret_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path))
    monkeypatch.setattr(searchserver.shutil, "which", lambda _: "/usr/bin/docker")
    calls = []
    monkeypatch.setattr(searchserver.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout=""))
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["searchserver", "start"]) == 0
    root = tmp_path / "searchserver"
    secret = (root / ".env").read_text()
    assert len(secret.strip().split("=")[1]) == 64
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    settings = root / "searxng" / "settings.yml"
    settings.write_text("custom settings")
    assert searchserver.start(out, err) == 0
    assert (root / ".env").read_text() == secret
    assert settings.read_text() == "custom settings"
    assert calls[1][0][-2:] == ["up", "-d"]
    assert calls[1][1]["cwd"] == root
    assert "127.0.0.1:18765:8080" in (root / "compose.yaml").read_text()


def test_missing_docker_is_actionable(monkeypatch):
    monkeypatch.setattr(searchserver.shutil, "which", lambda _: None)
    err = io.StringIO()
    assert searchserver.start(io.StringIO(), err) == 1
    assert "Docker is required" in err.getvalue()


def test_existing_searxng_is_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path))
    monkeypatch.setattr(searchserver.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(searchserver.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='{"Image":"searxng/searxng:latest","Ports":"127.0.0.1:18765->8080/tcp"}\n'))
    out = io.StringIO()
    assert searchserver.start(out, io.StringIO()) == 0
    assert "already running" in out.getvalue()
    assert not (tmp_path / "searchserver").exists()


def test_compose_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path))
    monkeypatch.setattr(searchserver.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(searchserver.subprocess, "run", lambda args, **kwargs: SimpleNamespace(returncode=1 if "compose" in args else 0, stdout=""))
    err = io.StringIO()
    assert searchserver.start(io.StringIO(), err) == 1
    assert "failed to start" in err.getvalue()
