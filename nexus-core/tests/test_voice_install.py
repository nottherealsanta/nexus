import tomllib
from pathlib import Path

from nexus.voice import install
from nexus.voice.store import KESTREL_VERSION, PHOTON_VERSION


def test_voice_extra_pins_the_reviewed_runtime_for_pip_installs():
    project = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = project["project"]["optional-dependencies"]["voice"]
    assert any(item.startswith(f"moondream=={PHOTON_VERSION};") for item in requirements)
    assert any(item.startswith(f"kestrel=={KESTREL_VERSION};") for item in requirements)


def test_requirements_drop_the_extra_marker(monkeypatch):
    monkeypatch.setattr(install.metadata, "requires", lambda name: [
        "httpx>=0.27",
        "numpy>=1.26; extra == 'voice'",
        "torch>=2.6; (sys_platform == 'darwin') and extra == 'voice'",
        "other>=1; extra == 'dev'",
    ])
    assert install.requirements() == ["numpy>=1.26", "torch>=2.6; (sys_platform == 'darwin')"]


def test_init_installs_into_this_environment(monkeypatch, tmp_path, capsys):
    ran = []
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(install, "requirements", lambda: ["numpy>=1.26"])
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: ran.append(cmd) or type("R", (), {"returncode": 0})())
    from nexus.voice import download, status
    downloads = []
    monkeypatch.setattr(download, "download_stt", lambda store: downloads.append("stt"))
    monkeypatch.setattr(download, "download_tts", lambda repo, revision, files: downloads.append((repo, revision, files)))
    monkeypatch.setattr(status, "status", lambda: {
        "stt": {"state": "absent", "repo": "moondream/parakeet-redux", "error": None},
        "tts": {"state": "absent", "repo": "example/tts", "error": None},
    })
    assert install.init() == 0
    assert ran[0][-1] == "numpy>=1.26" and install.sys.executable in ran[0]
    assert "--quiet" in ran[0]
    out = capsys.readouterr().out
    assert downloads == ["stt", ("example/tts", "main", [])]
    assert "Speech to text: model downloaded" in out
    assert "Text to speech: model downloaded" in out
    assert "nexus daemon stop" in out and "nexus daemon start" in out


def test_install_command_quiet_with_uv_and_pip(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: "/bin/uv")
    assert install.install_command(["x"]) == ["uv", "pip", "install", "--quiet", "--python", install.sys.executable, "x"]
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    assert install.install_command(["x"]) == [install.sys.executable, "-m", "pip", "install", "--quiet", "x"]


def test_init_remembers_the_environment(monkeypatch, tmp_path):
    from nexus.voice import download, status
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(install, "requirements", lambda: ["numpy>=1.26"])
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(status, "status", lambda: {
        "stt": {"state": "absent", "repo": "stt", "error": None},
        "tts": {"state": "absent", "repo": "tts", "error": None},
    })
    monkeypatch.setattr(download, "download_stt", lambda store: None)
    monkeypatch.setattr(download, "download_tts", lambda *args: None)
    assert not install.initialised()
    assert install.init() == 0
    assert install.initialised()
    remembered = install.marker().read_text()
    assert install.environment() in remembered
    assert install.sys.executable in remembered


def test_download_failure_continues_and_is_retryable(monkeypatch, tmp_path, capsys):
    from nexus.voice import download, status
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(install, "requirements", lambda: ["x"])
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(status, "status", lambda: {
        "stt": {"state": "absent", "repo": "stt", "error": None},
        "tts": {"state": "absent", "repo": "tts", "error": None},
    })
    def fail(_store):
        raise OSError("network down")
    monkeypatch.setattr(download, "download_stt", fail)
    called = []
    monkeypatch.setattr(download, "download_tts", lambda *args: called.append(args))
    assert install.init() == 1
    assert called and install.initialised()
    output = capsys.readouterr()
    assert "network down" in output.err and "again to retry" in output.err
    assert "Text to speech: model downloaded" in output.out


def test_unsupported_model_does_not_block_other_download(monkeypatch, tmp_path, capsys):
    from nexus.voice import download, status
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(install, "requirements", lambda: ["x"])
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(status, "status", lambda: {
        "stt": {"state": "unsupported", "repo": "stt", "error": "Unsupported platform"},
        "tts": {"state": "absent", "repo": "tts", "error": None},
    })
    called = []
    monkeypatch.setattr(download, "download_tts", lambda *args: called.append(args))
    assert install.init() == 1
    assert called and "Unsupported platform" in capsys.readouterr().err


def test_init_reports_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(install, "requirements", lambda: ["x"])
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: type("R", (), {"returncode": 3})())
    assert install.init() == 3
    assert not install.initialised()
    output = capsys.readouterr()
    assert "failed (exit 3)" in output.err
    assert "Voice runtime installed" not in output.out
