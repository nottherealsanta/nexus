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


def test_init_installs_into_this_environment(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(install, "requirements", lambda: ["numpy>=1.26"])
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: ran.append(cmd) or type("R", (), {"returncode": 0})())
    assert install.init() == 0
    assert ran[0][-1] == "numpy>=1.26" and install.sys.executable in ran[0]
    assert "stt:" in capsys.readouterr().out


def test_init_remembers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(install, "requirements", lambda: ["numpy>=1.26"])
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: type("R", (), {"returncode": 0})())
    assert not install.initialised()
    assert install.init() == 0
    assert install.initialised()
    remembered = install.marker().read_text()
    assert install.environment() in remembered
    assert install.sys.executable in remembered


def test_init_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setattr(install, "requirements", lambda: ["x"])
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: type("R", (), {"returncode": 3})())
    assert install.init() == 3
    assert not install.initialised()
