"""Extension trash: path scoping, atomic rollback, pin-aware lifecycle.

``nexus ext trash`` is the only operation that removes a *trusted* hot extension
from disk. These tests pin the properties the task and PLAN sections 2.3/11
require:

* **managed roots only** -- a target must be a discoverable candidate under
  ``[ext].dirs``; an arbitrary path, a ``_``-prefixed file, or a config file is
  refused, and nothing outside the managed tree is ever deleted;
* **symlink/path-traversal protection** -- a symlinked candidate, a ``..``
  escape, a null byte, and a symlinked component below a root all fail closed;
* **atomic move + retention metadata** -- the file lands in the per-project
  machine-state ``trash/extensions`` directory with a durable record whose
  hash/retention match the removal;
* **failure rollback** -- a failed metadata write or an aborted rebuild leaves
  the original file exactly where it was;
* **pin-aware generation lifecycle** -- a live lease keeps the retired module
  alive until release while the next generation no longer exposes the tool;
* the facade/reload path makes the removed extension disappear from future runs.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import msgspec
import pytest

from nexus.config import Config
from nexus.config.paths import project_state_dir
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ExtSection,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.errors import ExtensionError, ExtensionTrashError
from nexus.ext import ExtensionManager
from nexus.ext import manager as manager_module
from nexus.ext.manager import ExtensionTrashOutcome, ExtensionTrashRecord
from nexus.ext.quarantine import Quarantine
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.tools.loader import MODULE_PREFIX, ToolLoader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ConfigBox:
    def __init__(self, config: Config) -> None:
        self.config = config

    def __call__(self) -> Config:
        return self.config


def make_config(*, dirs: list[str] | None = None, enabled: bool = True) -> Config:
    return Config(
        v2=ConfigV2(
            ext=ExtSection(
                enabled=enabled,
                watch_interval_ms=0,
                dirs=dirs or [".agents/tools", ".nexus/tools", "~/.nexus/tools"],
                quarantine=False,
                max_file_bytes=100_000,
            )
        )
    )


def tool_source(name: str, *, description: str = "a fixture tool") -> str:
    return (
        "from typing import Any\n"
        "from nexus.tools.spec import ToolExecutionResult\n"
        "SPEC = {\n"
        f"    'name': {name!r},\n"
        f"    'description': {description!r},\n"
        "    'input_schema': {'type': 'object'},\n"
        "    'bundle': 'ext',\n"
        "}\n\n"
        "async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:\n"
        "    return ToolExecutionResult.text('ok')\n"
    )


def write_tool(directory: Path, stem: str, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.py"
    path.write_text(tool_source(name), encoding="utf-8")
    return path


def make_manager(
    tmp_path: Path, config: Config | None = None
) -> tuple[ExtensionManager, ConfigBox, Path, Path]:
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    workspace.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    box = ConfigBox(config or make_config())
    manager = ExtensionManager(
        workspace,
        home=home,
        config_loader=box,
        loader=ToolLoader(),
        quarantine=Quarantine(
            max_file_bytes=100_000,
            timeout_s=0.3,
            root=workspace,
            stage_root=tmp_path / "stage",
        ),
        retention_seconds=3600.0,
    )
    return manager, box, workspace, home


def tool_dir(root: Path) -> Path:
    return root / ".agents" / "tools"


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _wait_for(path: Path, proc: subprocess.Popen, *, timeout: float = 30.0) -> None:
    """Block until a subprocess touches ``path``, failing if it dies first."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            _out, err = proc.communicate()
            raise AssertionError(f"producer exited early rc={proc.returncode}: {err!r}")
        if time.monotonic() > deadline:
            proc.kill()
            proc.communicate()
            raise AssertionError("producer never reached the paused point")
        time.sleep(0.005)


@pytest.fixture(autouse=True)
def _clean_sys_modules():
    before = set(sys.modules)
    yield
    for name in set(sys.modules):
        if name.startswith(MODULE_PREFIX) and name not in before:
            sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Security: managed roots, symlinks, traversal, arbitrary paths
# ---------------------------------------------------------------------------


async def test_trash_refuses_an_arbitrary_path(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    secret = ws / "secret.py"
    secret.write_text("TOKEN = 'x'\n", encoding="utf-8")

    with pytest.raises(ExtensionTrashError):
        await manager.trash(str(secret))
    assert secret.exists()

    with pytest.raises(ExtensionTrashError):
        await manager.trash("/etc/hosts")


async def test_trash_refuses_a_traversal_target(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    outside = ws.parent / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ExtensionTrashError):
        await manager.trash("../outside.py")
    assert outside.exists()


async def test_trash_refuses_an_empty_or_null_target(tmp_path: Path):
    manager, _box, _ws, _home = make_manager(tmp_path)
    with pytest.raises(ExtensionTrashError):
        await manager.trash("   ")
    with pytest.raises(ExtensionTrashError):
        await manager.trash("bad\x00name.py")


async def test_trash_refuses_a_symlinked_candidate(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text(tool_source("Outside"), encoding="utf-8")
    directory = tool_dir(ws)
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "linked.py"
    link.symlink_to(outside)

    with pytest.raises(ExtensionTrashError):
        await manager.trash(str(link))
    assert outside.exists()


async def test_trash_works_for_a_user_tier_extension(tmp_path: Path):
    manager, _box, _ws, home = make_manager(tmp_path)
    path = write_tool(home / ".nexus" / "tools", "user_alpha", "UserAlpha")

    outcome = await manager.trash(str(path), reason="cleanup")

    assert not path.exists()
    assert outcome.record.relative_path == ""
    assert "UserAlpha" not in manager.manifest.tools


async def test_trash_refuses_non_candidate_files(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    await manager.reload()  # seeds .nexus/tools/_template.py
    template = tool_dir(ws) / "_template.py"
    assert template.exists()
    with pytest.raises(ExtensionTrashError):
        await manager.trash(str(template))

    config = ws / "nexus.toml"
    config.write_text("config_version = 2\n", encoding="utf-8")
    with pytest.raises(ExtensionTrashError):
        await manager.trash(str(config))


async def test_reject_symlinked_components_below_a_root(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    root = ws / "r"
    root.mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (root / "sub").symlink_to(outside)

    with pytest.raises(ExtensionTrashError):
        manager._reject_symlinked_components(root / "sub" / "f.py", root)

    (root / "real").mkdir()
    manager._reject_symlinked_components(root / "real" / "f.py", root)


# ---------------------------------------------------------------------------
# Integration: retention metadata and disappearance
# ---------------------------------------------------------------------------


async def test_trash_removes_the_tool_and_records_retention(tmp_path: Path):
    manager, _box, ws, home = make_manager(tmp_path)
    path = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    assert "Alpha" in manager.manifest.tools
    digest = manager.manifest.modules[next(iter(manager.manifest.modules))].sha256
    previous = manager.generation

    outcome = await manager.trash(str(path))

    assert isinstance(outcome, ExtensionTrashOutcome)
    record = outcome.record
    assert isinstance(record, ExtensionTrashRecord)
    assert not path.exists()
    assert "Alpha" not in manager.manifest.tools
    assert manager.generation == previous + 1
    assert outcome.report is not None and outcome.report.changed is True
    assert record.sha256 == digest
    assert record.modules and record.modules[0].endswith(f"__g{previous}")
    assert record.delete_after > record.trashed_at
    assert record.delete_after - record.trashed_at == pytest.approx(3600.0)
    assert manager.trash_dir == project_state_dir(ws, home) / "trash" / "extensions"
    assert (manager.trash_dir / record.trash_id / "meta.json").exists()
    assert manager.list_trashed() == (record,)
    assert all(row["name"] != record.modules[0] for row in manager.list_extensions())


async def test_trash_rolls_back_when_the_rebuild_aborts(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    # A broken sibling makes the next rebuild abort; the previous manifest, with
    # Alpha in it, is retained. Trashing Alpha must therefore roll back.
    (tool_dir(ws) / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(ExtensionTrashError):
        await manager.trash(str(alpha))

    assert alpha.exists()
    assert manager.list_trashed() == ()
    assert "Alpha" in manager.manifest.tools


async def test_trash_force_keeps_the_file_when_the_rebuild_aborts(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    (tool_dir(ws) / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    outcome = await manager.trash(str(alpha), force=True)

    assert not alpha.exists()
    assert len(manager.list_trashed()) == 1
    assert outcome.record.trash_id


async def test_trash_rolls_back_when_metadata_write_fails(
    tmp_path: Path, monkeypatch
):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()

    def explode(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(manager_module, "_write_trash_meta", explode)
    with pytest.raises(RuntimeError):
        await manager.trash(str(alpha))

    assert alpha.exists()
    assert manager.list_trashed() == ()


async def test_trash_rolls_back_when_the_reload_raises(
    tmp_path: Path, monkeypatch
):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("reload exploded")

    monkeypatch.setattr(manager, "reload", explode)
    with pytest.raises(RuntimeError):
        await manager.trash(str(alpha))

    # The move is rolled back, so disk and the live manifest stay consistent.
    assert alpha.exists()
    assert manager.list_trashed() == ()
    assert "Alpha" in manager.manifest.tools


async def test_trash_does_not_mask_a_reload_cancellation(tmp_path: Path, monkeypatch):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(manager, "reload", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await manager.trash(str(alpha))

    # The rollback ran, but the original cancellation still propagates.
    assert alpha.exists()
    assert manager.list_trashed() == ()


async def test_trash_and_validate_refuse_when_extensions_are_disabled(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path, make_config(enabled=False))
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")

    with pytest.raises(ExtensionTrashError):
        await manager.trash(str(alpha))
    with pytest.raises(ExtensionError):
        manager.validate(str(alpha))

    # A whole-tree validation is a truthful empty pass, not a refusal.
    report = manager.validate()
    assert report.valid is True and report.checked == 0 and report.results == ()
    assert alpha.exists()
    assert manager.list_trashed() == ()


def test_listing_ignores_an_entry_whose_name_is_not_its_trash_id(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    trash_dir = manager.trash_dir
    trash_dir.mkdir(parents=True, exist_ok=True)
    entry = trash_dir / "shadow"
    entry.mkdir()
    (entry / "evil.py").write_text(tool_source("Evil"), encoding="utf-8")
    record = ExtensionTrashRecord(
        trash_id="alpha-abc123",
        source_path=str(ws / ".agents" / "tools" / "evil.py"),
        trashed_at=1.0,
        delete_after=9_999_999_999.0,
    )
    (entry / "meta.json").write_bytes(msgspec.json.encode(record))

    assert manager.list_trashed() == ()
    assert manager.purge_expired(now=9_999_999_999.0) == []
    with pytest.raises(ExtensionTrashError):
        manager.restore("alpha-abc123")
    assert (entry / "evil.py").exists()


async def test_published_entry_name_is_its_recorded_trash_id(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()

    outcome = await manager.trash(str(alpha))
    entry = manager.trash_dir / outcome.record.trash_id
    assert entry.is_dir()
    assert entry.name == outcome.record.trash_id
    assert manager.list_trashed() == (outcome.record,)


async def test_trash_is_pin_aware_and_defers_module_release(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    old_module = next(iter(manager.manifest.modules))

    lease = manager.ref.pin()
    try:
        outcome = await manager.trash(str(alpha))
        # The new generation no longer exposes the tool...
        assert "Alpha" not in manager.manifest.tools
        # ...but the pinned generation still owns its module until release.
        assert old_module in sys.modules
        assert manager.ref.retired_generations == (outcome.report.previous_generation,)
    finally:
        lease.release()

    assert old_module not in sys.modules
    assert manager.ref.retired_generations == ()
    assert manager.cleanup_failures == ()


async def test_restore_round_trip(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()

    outcome = await manager.trash(str(alpha))
    assert not alpha.exists()

    record = manager.restore(outcome.record.trash_id)
    assert record.trash_id == outcome.record.trash_id
    assert alpha.exists()
    assert manager.list_trashed() == ()

    report = await manager.reload()
    assert report.changed is True
    assert "Alpha" in manager.manifest.tools


async def test_trash_round_trips_a_non_ascii_candidate_stem(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    path = write_tool(tool_dir(ws), "caf\u00e9", "Cafe")
    await manager.reload()

    outcome = await manager.trash(str(path))

    assert not path.exists()
    assert [record.trash_id for record in manager.list_trashed()] == [
        outcome.record.trash_id
    ]
    assert manager.restore(outcome.record.trash_id).trash_id == outcome.record.trash_id
    assert path.exists()


async def test_purge_expired_removes_entries(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    outcome = await manager.trash(str(alpha))

    removed = manager.purge_expired(now=outcome.record.delete_after + 1.0)

    assert removed == [outcome.record.trash_id]
    assert not (manager.trash_dir / outcome.record.trash_id).exists()


# ---------------------------------------------------------------------------
# Facade / real Runtime
# ---------------------------------------------------------------------------


async def test_facade_refuses_trash_without_an_extension_manager():
    facade = HostFacade(SimpleNamespace(extensions=None))
    result = await facade.handle(p.ExtensionsTrash(target="a.py"))
    assert isinstance(result, p.ErrorResult)
    assert result.kind == "ExtensionTrashError"


def _runtime_config(tmp_path: Path) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
            ext=ExtSection(
                enabled=True,
                watch_interval_ms=0,
                dirs=[".agents/tools", ".nexus/tools"],
                quarantine=False,
                max_file_bytes=100_000,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# validate: read-only, scoped to managed candidates
# ---------------------------------------------------------------------------


async def test_validate_refuses_an_arbitrary_target_without_executing_it(
    tmp_path: Path,
):
    manager, _box, ws, _home = make_manager(tmp_path)
    sentinel = tmp_path / "pwned"
    payload = ws / "payload.py"
    payload.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )

    with pytest.raises(ExtensionError):
        manager.validate(str(payload))
    with pytest.raises(ExtensionError):
        manager.validate("/etc/hosts")
    with pytest.raises(ExtensionError):
        manager.validate("../../payload.py")

    assert payload.exists()
    assert not sentinel.exists()


async def test_validate_refuses_symlink_template_and_non_candidate(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text(tool_source("Outside"), encoding="utf-8")
    directory = tool_dir(ws)
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "linked.py"
    link.symlink_to(outside)
    await manager.reload()  # seeds _template.py
    template = directory / "_template.py"

    for target in (str(link), str(template), str(ws / "nexus.toml")):
        with pytest.raises(ExtensionError):
            manager.validate(target)
    assert outside.exists()


async def test_validate_accepts_a_discovered_candidate(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    good = write_tool(tool_dir(ws), "good", "Good")
    broken = tool_dir(ws) / "broken.py"
    broken.write_text("def broken(:\n", encoding="utf-8")

    report = manager.validate(str(good))
    assert report.valid is True
    assert report.checked == 1
    assert report.results[0]["ok"] is True

    report = manager.validate(str(broken))
    assert report.valid is False
    assert report.results[0]["ok"] is False


async def test_reload_reruns_after_a_failed_rebuild(tmp_path: Path, monkeypatch):
    manager, _box, _ws, _home = make_manager(tmp_path)
    await manager.reload()

    real = manager._rebuild
    calls = {"n": 0}

    async def flaky(trigger, sink):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real(trigger, sink)

    monkeypatch.setattr(manager, "_rebuild", flaky)
    with pytest.raises(RuntimeError):
        await manager.reload()
    # The failed request produced no report, so the next caller must rebuild
    # rather than being handed the pre-failure report.
    report = await manager.reload()
    assert calls["n"] == 2
    assert report is not None


async def test_facade_validate_scopes_the_target(tmp_path: Path):
    manager, _box, ws, _home = make_manager(tmp_path)
    facade = HostFacade(SimpleNamespace(extensions=manager))
    secret = ws / "secret.py"
    secret.write_text("TOKEN = 'sk-live-supersecret123456'\n", encoding="utf-8")

    result = await facade.handle(p.ExtensionsValidate(target=str(secret)))
    assert isinstance(result, p.ErrorResult)
    assert secret.exists()

    good = write_tool(tool_dir(ws), "good", "Good")
    ok = await facade.handle(p.ExtensionsValidate(target=str(good)))
    assert isinstance(ok, p.ExtensionsValidateResult)
    assert ok.valid is True and ok.checked == 1


# ---------------------------------------------------------------------------
# Undo-safe trash: parent-swap containment and durable bytes
# ---------------------------------------------------------------------------


async def test_trash_fails_closed_when_parent_is_swapped_for_a_symlink(
    tmp_path: Path, monkeypatch
):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("safe", encoding="utf-8")
    # A decoy with the same name as the candidate, to prove the pre-move
    # containment check stops the rename rather than moving the decoy.
    decoy = outside / alpha.name
    decoy.write_text("decoy", encoding="utf-8")

    def swap(*_args, **_kwargs):
        root = tool_dir(ws)
        root.rename(root.with_name("tools-real"))
        root.symlink_to(outside)

    monkeypatch.setattr(manager_module, "_write_trash_meta", swap)
    with pytest.raises(ExtensionTrashError):
        await manager.trash(str(alpha))

    assert canary.read_text(encoding="utf-8") == "safe"
    assert decoy.read_text(encoding="utf-8") == "decoy"
    assert sorted(child.name for child in outside.iterdir()) == sorted(
        ["canary.txt", "alpha.py"]
    )
    assert manager.list_trashed() == ()


async def test_trash_fsyncs_the_moved_file_before_publishing(tmp_path: Path, monkeypatch):
    manager, _box, ws, _home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()

    synced: list[Path] = []
    monkeypatch.setattr(manager_module, "_fsync_file", lambda path: synced.append(path))
    await manager.trash(str(alpha))

    assert any(path.name == "alpha.py" for path in synced)


async def test_restore_refuses_a_crafted_source_path_outside_the_roots(
    tmp_path: Path,
):
    manager, _box, _ws, _home = make_manager(tmp_path)
    trash_dir = manager.trash_dir
    trash_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    escaped = outside_dir / "evil.py"
    entry = trash_dir / "crafted"
    entry.mkdir()
    (entry / "evil.py").write_text(tool_source("Evil"), encoding="utf-8")
    record = ExtensionTrashRecord(
        trash_id="crafted",
        source_path=str(escaped),
        trashed_at=1.0,
        delete_after=9_999_999_999.0,
    )
    (entry / "meta.json").write_bytes(msgspec.json.encode(record))

    with pytest.raises(ExtensionTrashError):
        manager.restore("crafted")

    assert not escaped.exists()
    assert sorted(child.name for child in outside_dir.iterdir()) == []


async def test_purge_ignores_a_traversal_trash_id(tmp_path: Path):
    manager, _box, _ws, _home = make_manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("safe", encoding="utf-8")
    trash_dir = manager.trash_dir
    trash_dir.mkdir(parents=True, exist_ok=True)
    entry = trash_dir / "entry"
    entry.mkdir()
    record = ExtensionTrashRecord(
        trash_id="../../outside",
        source_path=str(tmp_path / "ws" / ".nexus" / "tools" / "x.py"),
        trashed_at=1.0,
        delete_after=0.0,
    )
    (entry / "meta.json").write_bytes(msgspec.json.encode(record))

    assert manager.list_trashed() == ()
    assert manager.purge_expired(now=9_999_999_999.0) == []
    assert canary.read_text(encoding="utf-8") == "safe"
    assert outside.exists()


async def test_facade_trash_makes_the_tool_disappear_from_future_runs(tmp_path: Path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(
        tmp_path, config=_runtime_config(tmp_path), providers={"scripted": provider}
    )
    facade = HostFacade(runtime)
    path = write_tool(tmp_path / ".agents" / "tools", "ping", "Ping")

    await facade.reload_extensions(trigger="test")
    tools = [row["name"] for row in await facade.list_tools()]
    assert "Ping" in tools

    result = await facade.handle(p.ExtensionsTrash(target=".agents/tools/ping.py"))

    assert isinstance(result, p.ExtensionsTrashResult)
    assert result.changed is True
    assert result.trash_id
    assert result.sha256
    assert result.delete_after > result.trashed_at
    assert result.generation > result.previous_generation
    assert not path.exists()

    tools = [row["name"] for row in await facade.list_tools()]
    assert "Ping" not in tools
    assert facade.list_extensions() == ()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Cross-process recovery safety: trash lock ordering and crash states
# ---------------------------------------------------------------------------


def test_recover_leaves_a_corrupt_staging_meta_intact(tmp_path: Path):
    """Never delete the only copy when a staging record is unreadable.

    Extension trash writes durable metadata *before* moving the file, so an
    unreadable record cannot be trusted to mean "the file never moved". Recovery
    must leave the staging directory alone rather than discard a possible only
    copy.
    """
    manager, _box, _ws, _home = make_manager(tmp_path)
    staging = manager.trash_dir / ".ext-staging-corrupt"
    staging.mkdir(parents=True)
    (staging / "meta.json").write_bytes(b'{"trash_id": "alpha-abc", "sou')
    (staging / "meta.json.tmp").write_bytes(b'{"partial":')
    only = staging / "alpha.py"
    only.write_text(tool_source("Alpha"), encoding="utf-8")

    assert manager.list_trashed() == ()
    assert staging.exists()
    assert only.exists()


def test_recover_discards_a_staging_dir_that_never_moved_a_file(tmp_path: Path):
    """No metadata and no staged file means the move never happened; discard."""
    manager, _box, _ws, _home = make_manager(tmp_path)
    staging = manager.trash_dir / ".ext-staging-empty"
    staging.mkdir(parents=True)
    (staging / "meta.json.tmp").write_bytes(b'{"partial":')

    assert manager.list_trashed() == ()
    assert not staging.exists()


def test_recover_skips_while_a_producer_holds_the_trash_lock(tmp_path: Path):
    """Deterministic two-process check: recovery never discards a live stage.

    A real ``_publish_trash`` runs in a child process and pauses *after* writing
    durable metadata but before moving the file, while holding the cross-process
    trash lock. The parent's ``list_trashed`` recovery must skip (non-blocking)
    rather than discard the staging directory, then resume the producer and see
    a clean publish.
    """
    manager, _box, ws, home = make_manager(tmp_path)
    alpha = write_tool(tool_dir(ws), "alpha", "Alpha")

    sentinel = tmp_path / "paused"
    resume = tmp_path / "resume"
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from nexus.config import Config
        from nexus.config.schema import ConfigV2, ExtSection
        from nexus.ext import ExtensionManager
        from nexus.ext import manager as m

        ws, home, target, trash, sentinel, resume = map(Path, sys.argv[1:7])
        real = m._write_trash_meta

        def paused(path, record):
            real(path, record)
            sentinel.write_text("paused")
            deadline = time.monotonic() + 30
            while not resume.exists():
                if time.monotonic() > deadline:
                    raise SystemExit("resume timeout")
                time.sleep(0.005)

        m._write_trash_meta = paused
        cfg = Config(
            v2=ConfigV2(
                ext=ExtSection(
                    enabled=True,
                    watch_interval_ms=0,
                    dirs=[".agents/tools", ".nexus/tools"],
                    quarantine=False,
                    max_file_bytes=100_000,
                )
            )
        )
        mgr = ExtensionManager(ws, home=home, config_loader=lambda: cfg, trash_dir=trash)
        mgr._publish_trash(target, m._ext_section(cfg), reason="race")
        """
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(ws),
            str(home),
            str(alpha),
            str(manager.trash_dir),
            str(sentinel),
            str(resume),
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(sentinel, proc)
        staging = list(manager.trash_dir.glob(".ext-staging-*"))
        assert len(staging) == 1
        assert (staging[0] / "meta.json").exists()
        assert not (staging[0] / "alpha.py").exists()
        assert alpha.exists()

        # Recovery must skip, not discard the durable metadata out from under
        # the paused producer.
        assert manager.list_trashed() == ()
        assert staging[0].is_dir()
        assert alpha.exists()
    finally:
        resume.write_text("go")
        proc.wait(timeout=30)
    assert proc.returncode == 0

    assert not alpha.exists()
    records = manager.list_trashed()
    assert [record.source_path for record in records] == [str(alpha)]
    assert manager.restore(records[0].trash_id).trash_id == records[0].trash_id
    assert alpha.exists()
