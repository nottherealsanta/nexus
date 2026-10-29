import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Tests never see the developer's real `~/.agents/skills` (or other files under `$HOME`)."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _no_background_requests(monkeypatch):
    """Titles and automatic handoffs would consume a scripted provider's replies; their own tests turn them on."""
    from nexus.runtime import Runtime

    monkeypatch.setattr(Runtime, "background_requests", False)
