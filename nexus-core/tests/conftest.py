import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Tests never see the developer's real `~/.agents/skills` (or other files under `$HOME`)."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
