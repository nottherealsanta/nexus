"""File-loaded provider discovery precedence (plan section 8, STATE_PLAN §5.4).

``FileProviderLoader.directories`` orders ``<workspace>/.agents/providers``
above the legacy, read-only ``<workspace>/.nexus/providers`` above
``~/.nexus/providers``. This file was previously untested; it now covers the
precedence chain end to end, including the new ``.agents`` migration.
"""
from __future__ import annotations

from pathlib import Path

from nexus.model.providers.discovery import FileProviderLoader

_PROVIDER_SOURCE = """
class _P:
    name = {registered_name!r}
    label = {label!r}
    def stream(self, *args, **kwargs):
        raise NotImplementedError


PROVIDER = _P()
"""


def _write_provider(
    directory: Path, filename: str, *, registered_name: str, label: str | None = None
) -> Path:
    """Write a minimal provider file. ``registered_name`` is the dict key a

    same-name file in a higher-precedence directory shadows; ``label`` is an
    extra, non-contractual attribute the test reads back to tell *which*
    file's object won.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(
        _PROVIDER_SOURCE.format(
            registered_name=registered_name, label=label or registered_name
        ),
        encoding="utf-8",
    )
    return path


def test_directories_are_agents_then_legacy_nexus_then_user(tmp_path: Path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    (workspace / ".agents" / "providers").mkdir(parents=True)
    (workspace / ".nexus" / "providers").mkdir(parents=True)
    (home / ".nexus" / "providers").mkdir(parents=True)
    loader = FileProviderLoader()
    dirs = loader.directories(workspace, home)
    assert dirs == [
        workspace / ".agents" / "providers",
        workspace / ".nexus" / "providers",
        home / ".nexus" / "providers",
    ]


def test_missing_directories_are_skipped(tmp_path: Path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    loader = FileProviderLoader()
    assert loader.directories(workspace, home) == []


def test_legacy_nexus_providers_dir_is_a_read_only_fallback(tmp_path: Path):
    """STATE_PLAN §5.4: a provider file under the legacy dir still loads."""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    _write_provider(workspace / ".nexus" / "providers", "legacy.py", registered_name="legacy")
    loader = FileProviderLoader()
    result = loader.load(workspace, home)
    assert result.ok
    assert "legacy" in result.providers
    assert not (workspace / ".agents").exists()


def test_agents_provider_shadows_legacy_nexus_provider_of_the_same_name(
    tmp_path: Path,
):
    """STATE_PLAN §5.4: ``.agents`` outranks the legacy ``.nexus`` fallback."""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    _write_provider(
        workspace / ".nexus" / "providers",
        "shared.py",
        registered_name="shared",
        label="from-legacy",
    )
    _write_provider(
        workspace / ".agents" / "providers",
        "shared.py",
        registered_name="shared",
        label="from-agents",
    )
    loader = FileProviderLoader()
    result = loader.load(workspace, home)
    assert result.ok
    assert set(result.providers) == {"shared"}
    assert result.providers["shared"].label == "from-agents"


def test_workspace_provider_shadows_user_provider_of_the_same_name(tmp_path: Path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    _write_provider(
        workspace / ".agents" / "providers",
        "shared.py",
        registered_name="shared",
        label="workspace",
    )
    _write_provider(
        home / ".nexus" / "providers",
        "shared.py",
        registered_name="shared",
        label="user",
    )
    loader = FileProviderLoader()
    result = loader.load(workspace, home)
    assert result.ok
    assert set(result.providers) == {"shared"}
    assert result.providers["shared"].label == "workspace"
