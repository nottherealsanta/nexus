"""Phase 4 P4-D: the serialized hot-reload transaction and its watcher.

Covers the plan section 6.1-6.4 orchestration contract the whole self-extension
design rests on:

* one serialized, coalescing rebuild for API/tool/watcher triggers, with a
  single compare-and-swap and no generation churn on a no-op;
* workspace-over-user precedence, shadow reveal on deletion, underscore files
  ignored, and builtin names reserved (case-folded);
* unchanged exact bytes are reused object-for-object; changed bytes are
  re-quarantined and imported under a fresh generation-stamped module;
* **all-or-nothing**: a broken syntax, import crash, or import-time hang leaves
  the previous manifest exactly as it was, and a changed candidate that fails
  never replaces working code;
* config, ``SOUL.md``/``MEMORY.md``, and the skill tree participate in the same
  immutable manifest and diff;
* modules are retired only after the last ``ManifestLease`` pin is released, and
  a cleanup failure is reported without rolling back the swap;
* events go out through a sync/async-safe sink and a failed reload emits
  ``ext.failed`` but never ``ext.manifest_changed``;
* the directory watcher turns one batch of changes into one rebuild, and its
  close is idempotent.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ExtSection
from nexus.core.bus import Bus
from nexus.errors import ConfigError, ManagerClosed
from nexus.ext import ExtensionManager
from nexus.ext.quarantine import Quarantine
from nexus.tools.loader import MODULE_PREFIX, ToolLoader
from nexus.tools.spec import (
    RegisteredTool,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ConfigBox:
    """A mutable config loader so a test can change the effective config."""

    def __init__(self, config: Config):
        self.config = config
        self.error: Exception | None = None

    def __call__(self) -> Config:
        if self.error is not None:
            raise self.error
        return self.config


def make_config(
    *,
    enabled: bool = True,
    quarantine: bool = False,
    interval_ms: int = 20,
    dirs: list[str] | None = None,
    **flat: object,
) -> Config:
    ext = ExtSection(
        enabled=enabled,
        watch_interval_ms=interval_ms,
        dirs=dirs or [".agents/tools", ".nexus/tools", "~/.nexus/tools"],
        quarantine=quarantine,
        max_file_bytes=100_000,
    )
    return Config(v2=ConfigV2(ext=ext), **flat)


def tool_source(name: str, *, description: str = "a fixture tool", body: str = "ok") -> str:
    return (
        "from typing import Any\n"
        "from nexus.tools.spec import ToolContext, ToolExecutionResult\n"
        "SPEC = {\n"
        f"    'name': {name!r},\n"
        f"    'description': {description!r},\n"
        "    'input_schema': {'type': 'object'},\n"
        "    'bundle': 'fs',\n"
        "}\n\n"
        "async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:\n"
        f"    return ToolExecutionResult.text({body!r})\n"
    )


def write_tool(directory: Path, stem: str, name: str, **kwargs: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.py"
    path.write_text(tool_source(name, **kwargs), encoding="utf-8")
    return path


def write_skill(root: Path, name: str, *, description: str = "a skill", body: str = "BODY") -> Path:
    directory = root / ".nexus" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return directory


def write_raw(directory: Path, stem: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.py"
    path.write_text(text, encoding="utf-8")
    return path


def builtin_tool(name: str) -> RegisteredTool:
    async def _run(args: dict[str, object], ctx: ToolContext) -> ToolExecutionResult:
        return ToolExecutionResult.text("builtin")

    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description="a builtin",
            input_schema={"type": "object"},
            bundle="fs",
        ),
        run=_run,
        origin="builtin",
    )


class RecordingSink:
    """A synchronous callable sink that records the events it receives."""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.types: list[str] = []

    def __call__(self, event: object) -> None:
        self.events.append(event)
        self.types.append(event.type)


class AsyncRecordingSink(RecordingSink):
    async def __call__(self, event: object) -> None:  # type: ignore[override]
        self.events.append(event)
        self.types.append(event.type)


def manager_for(
    tmp_path: Path,
    config: Config,
    *,
    builtin_tools: list[RegisteredTool] | None = None,
    sink: object | None = None,
    quarantine_timeout: float = 0.3,
) -> tuple[ExtensionManager, ConfigBox, Path, Path]:
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    workspace.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    box = ConfigBox(config)
    quarantine = Quarantine(
        max_file_bytes=100_000,
        timeout_s=quarantine_timeout,
        root=workspace,
        stage_root=tmp_path / "stage",
    )
    manager = ExtensionManager(
        workspace,
        home=home,
        config_loader=box,
        loader=ToolLoader(),
        quarantine=quarantine,
        builtin_tools=builtin_tools or [],
        sink=sink,
    )
    return manager, box, workspace, home


def tool_dir(root: Path) -> Path:
    return root / ".nexus" / "tools"


def agents_tool_dir(root: Path) -> Path:
    return root / ".agents" / "tools"


async def wait_until(predicate, *, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.fixture(autouse=True)
def _clean_sys_modules():
    """Drop loader-owned modules a manager legitimately keeps live.

    A manager holds live generations in ``sys.modules`` until they are retired,
    so this is cleanup, not an assertion; the retirement tests assert removal.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules):
        if name.startswith(MODULE_PREFIX) and name not in before:
            sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Initial manifest, load, no-op, change, delete
# ---------------------------------------------------------------------------


async def test_initial_manifest_is_builtins_only(tmp_path: Path):
    manager, _box, _ws, _home = manager_for(
        tmp_path, make_config(), builtin_tools=[builtin_tool("Read")]
    )
    manifest = manager.manifest
    assert manifest.generation == 0
    assert set(manifest.tools) == {"Read"}
    assert set(manifest.skills) == set()
    assert set(manifest.modules) == set()
    assert manager.ref.get() is manifest


async def test_reload_loads_extension_and_swaps_once(tmp_path: Path):
    manager, _box, ws, _home = manager_for(
        tmp_path, make_config(), builtin_tools=[builtin_tool("Read")]
    )
    write_tool(tool_dir(ws), "alpha", "Alpha")
    report = await manager.reload()
    assert report.ok is True
    assert report.changed is True
    assert report.previous_generation == 0
    assert report.generation == 1
    assert manager.generation == 1
    assert report.diff.tools.added == ("Alpha",)
    assert set(manager.manifest.tools) == {"Read", "Alpha"}
    assert manager.manifest.tools["Alpha"].spec.description == "a fixture tool"
    assert manager.manifest.tools["Alpha"].generation == 1
    assert len(manager.manifest.modules) == 1
    module_name = next(iter(manager.manifest.modules))
    assert module_name.endswith("__g1")


async def test_noop_reload_does_not_churn_generation(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    first = await manager.reload()
    assert first.changed is True
    second = await manager.reload()
    assert second.changed is False
    assert second.previous_generation == first.generation
    assert second.generation == first.generation
    assert second.diff.tools == first.diff.tools.__class__()  # empty delta
    assert manager.generation == 1


async def test_changed_bytes_reload_to_a_new_generation_module(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha", description="first")
    await manager.reload()
    old_module = next(iter(manager.manifest.modules))
    write_tool(tool_dir(ws), "alpha", "Alpha", description="second")
    report = await manager.reload()
    assert report.changed is True
    assert report.generation == 2
    assert report.diff.tools.changed == ("Alpha",)
    new_module = next(iter(manager.manifest.modules))
    assert new_module.endswith("__g2")
    assert new_module != old_module
    assert manager.manifest.tools["Alpha"].spec.description == "second"
    # No pins are held, so the retired generation's module is gone immediately.
    assert old_module not in sys.modules


async def test_delete_removes_tool_and_retires_module(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    write_tool(tool_dir(ws), "beta", "Beta")
    await manager.reload()
    before = set(manager.manifest.modules)
    (tool_dir(ws) / "alpha.py").unlink()
    report = await manager.reload()
    assert report.changed is True
    assert report.diff.tools.removed == ("Alpha",)
    assert "Alpha" not in manager.manifest.tools
    after = set(manager.manifest.modules)
    retired = before - after
    assert len(retired) == 1
    # The retired module is gone; the retained, reused module is still live.
    assert all(name not in sys.modules for name in retired)
    assert all(name in sys.modules for name in after)


# ---------------------------------------------------------------------------
# Precedence, shadowing, collisions, reserved builtins
# ---------------------------------------------------------------------------


async def test_workspace_precedence_shadows_user(tmp_path: Path):
    manager, _box, ws, home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "ws_shared", "Shared", description="from-workspace")
    write_tool(tool_dir(home), "user_shared", "Shared", description="from-user")
    report = await manager.reload()
    assert report.changed is True
    assert manager.manifest.tools["Shared"].spec.description == "from-workspace"
    assert len(manager.manifest.modules) == 1
    assert len(manager.loader.owned_modules) == 1


async def test_agents_tools_shadow_legacy_workspace_tools(tmp_path: Path):
    """STATE_PLAN §5.4: .agents/tools takes precedence over legacy .nexus/tools."""
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(agents_tool_dir(ws), "agents_shared", "Shared", description="from-agents")
    write_tool(tool_dir(ws), "legacy_shared", "Shared", description="from-legacy")

    report = await manager.reload()

    assert report.changed is True
    assert manager.manifest.tools["Shared"].spec.description == "from-agents"
    assert len(manager.manifest.modules) == 1


async def test_legacy_workspace_tools_remain_a_fallback(tmp_path: Path):
    """STATE_PLAN §5.4: legacy .nexus/tools remains readable without .agents/tools."""
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "legacy_only", "LegacyOnly", description="from-legacy")

    report = await manager.reload()

    assert report.changed is True
    assert manager.manifest.tools["LegacyOnly"].spec.description == "from-legacy"


async def test_deleting_workspace_tool_reveals_user_shadow(tmp_path: Path):
    manager, _box, ws, home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "ws_shared", "Shared", description="from-workspace")
    write_tool(tool_dir(home), "user_shared", "Shared", description="from-user")
    await manager.reload()
    (tool_dir(ws) / "ws_shared.py").unlink()
    report = await manager.reload()
    assert report.changed is True
    assert manager.manifest.tools["Shared"].spec.description == "from-user"
    assert len(manager.manifest.modules) == 1


async def test_same_tier_name_collision_aborts_the_whole_rebuild(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "a_first", "Clash")
    write_tool(tool_dir(ws), "b_second", "Clash")
    report = await manager.reload()
    assert report.changed is False
    assert report.failed
    assert "Clash" not in manager.manifest.tools
    assert manager.generation == 0
    assert manager.loader.owned_modules == ()


async def test_builtin_name_collision_is_reserved(tmp_path: Path):
    manager, _box, ws, _home = manager_for(
        tmp_path, make_config(), builtin_tools=[builtin_tool("Read")]
    )
    write_tool(tool_dir(ws), "shadow", "Read")
    report = await manager.reload()
    assert report.changed is False
    assert report.failed
    assert report.failed[0].error_type == "collision"
    assert "Read" in manager.manifest.tools
    assert manager.manifest.tools["Read"].spec.description == "a builtin"


async def test_builtin_casefold_collision_is_reserved(tmp_path: Path):
    manager, _box, ws, _home = manager_for(
        tmp_path, make_config(), builtin_tools=[builtin_tool("Read")]
    )
    write_tool(tool_dir(ws), "shadow", "read")
    report = await manager.reload()
    assert report.changed is False
    assert report.failed


async def test_underscore_prefixed_files_are_ignored(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "_template", "Template")
    write_tool(tool_dir(ws), "alpha", "Alpha")
    report = await manager.reload()
    assert report.changed is True
    assert set(manager.manifest.tools) == {"Alpha"}


# ---------------------------------------------------------------------------
# All-or-nothing failures
# ---------------------------------------------------------------------------


async def test_broken_syntax_aborts_and_loads_nothing(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "good", "Good")
    write_raw(
        tool_dir(ws),
        "broken",
        "SPEC = {'name': 'Broken', 'description': 'unterminated\n",
    )
    report = await manager.reload()
    assert report.changed is False
    assert report.failed
    assert "Good" not in manager.manifest.tools
    assert manager.generation == 0
    assert manager.loader.owned_modules == ()


async def test_import_error_aborts_the_rebuild(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    source = tool_source("Explodes")
    write_raw(tool_dir(ws), "explodes", "raise RuntimeError('boom')\n" + source)
    report = await manager.reload()
    assert report.changed is False
    assert report.failed
    assert report.failed[0].error_type in {"import_error", "import_exit"}


async def test_import_hang_aborts_the_rebuild(tmp_path: Path):
    manager, _box, ws, _home = manager_for(
        tmp_path, make_config(quarantine=True), quarantine_timeout=0.3
    )
    write_raw(
        tool_dir(ws), "hangs", tool_source("Hangs") + "while True:\n    pass\n"
    )
    report = await manager.reload()
    assert report.changed is False
    assert report.failed
    assert report.failed[0].error_type == "import_timeout"


async def test_changed_candidate_failure_keeps_working_generation(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "good", "Good", description="v1")
    await manager.reload()
    assert manager.generation == 1
    live_module = next(iter(manager.manifest.modules))
    write_tool(tool_dir(ws), "good", "Good", description="v2")
    (tool_dir(ws) / "good.py").write_text(
        "SPEC = {'name': 'Good', 'description': 'v2'\n", encoding="utf-8"
    )
    report = await manager.reload()
    assert report.changed is False
    assert report.failed
    assert manager.generation == 1
    assert manager.manifest.tools["Good"].spec.description == "v1"
    assert live_module in sys.modules


# ---------------------------------------------------------------------------
# Config, system files, and diagnostics
# ---------------------------------------------------------------------------


async def test_invalid_config_keeps_the_old_manifest(tmp_path: Path):
    manager, box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    assert manager.generation == 1
    box.error = ConfigError("bad toml")
    report = await manager.reload()
    assert report.changed is False
    assert report.failed
    assert report.failed[0].kind == "config"
    assert manager.generation == 1
    assert "Alpha" in manager.manifest.tools


async def test_config_change_swaps_with_no_entry_change(tmp_path: Path):
    manager, box, _ws, _home = manager_for(tmp_path, make_config(interval_ms=20))
    first = await manager.reload()
    assert first.changed is False
    box.config = make_config(interval_ms=99)
    report = await manager.reload()
    assert report.changed is True
    assert report.diff.config_changed is True
    assert report.generation == 1


async def test_soul_and_memory_are_manifest_system_files(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    (ws / "SOUL.md").write_text("soul-v1", encoding="utf-8")
    (ws / "MEMORY.md").write_text("memory-v1", encoding="utf-8")
    report = await manager.reload()
    assert report.changed is True
    assert report.diff.system_files.added == ("memory", "soul")
    files = manager.manifest.system_files
    assert files.soul is not None and files.soul.content == "soul-v1"
    assert files.memory is not None and files.memory.content == "memory-v1"
    # Changing only SOUL is a real manifest change.
    (ws / "SOUL.md").write_text("soul-v2", encoding="utf-8")
    report = await manager.reload()
    assert report.changed is True
    assert report.diff.system_files.changed == ("soul",)
    assert manager.manifest.system_files.soul.content == "soul-v2"


async def test_list_extensions_and_diagnostics_are_json_safe(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    listing = manager.list_extensions()
    assert len(listing) == 1
    assert listing[0]["name"].endswith("__g1")
    json.dumps(listing)
    json.dumps(manager.diagnostics())

    write_raw(tool_dir(ws), "bad", "def broken(:\n")
    await manager.reload()
    diagnostics = manager.diagnostics()
    assert diagnostics
    json.dumps(diagnostics)
    for row in diagnostics:
        for value in row.values():
            if isinstance(value, str):
                assert all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def test_events_emitted_on_load_and_change(tmp_path: Path):
    sink = RecordingSink()
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload(sink=sink)
    assert "ext.loaded" in sink.types
    assert "ext.manifest_changed" in sink.types
    loaded = next(e for e in sink.events if e.type == "ext.loaded")
    assert loaded.data["name"].endswith("__g1")
    assert loaded.data["generation"] == 1

    # A no-op emits nothing.
    sink.events.clear()
    sink.types.clear()
    await manager.reload(sink=sink)
    assert sink.types == []

    # Deleting emits unloaded and manifest_changed.
    (tool_dir(ws) / "alpha.py").unlink()
    await manager.reload(sink=sink)
    assert "ext.unloaded" in sink.types
    assert "ext.manifest_changed" in sink.types


async def test_failure_emits_failed_but_not_manifest_changed(tmp_path: Path):
    sink = RecordingSink()
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_raw(tool_dir(ws), "bad", "def broken(:\n")
    report = await manager.reload(sink=sink)
    assert report.changed is False
    assert "ext.failed" in sink.types
    assert "ext.manifest_changed" not in sink.types


async def test_async_sink_is_supported(tmp_path: Path):
    sink = AsyncRecordingSink()
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload(sink=sink)
    assert "ext.manifest_changed" in sink.types


async def test_broken_sink_never_breaks_a_reload(tmp_path: Path):
    def exploding(event):
        raise RuntimeError("sink is closed")

    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    report = await manager.reload(sink=exploding)
    assert report.changed is True


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_reloads_are_serialized_not_stale(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    for index in range(4):
        write_tool(tool_dir(ws), f"tool_{index}", f"Tool{index}")
    reports = await asyncio.gather(*(manager.reload() for _ in range(8)))
    final = manager.generation
    assert final == 1
    assert all(report.generation <= final for report in reports)
    assert max(report.generation for report in reports) == final
    assert len(manager.manifest.tools) == 4
    names = [name for name in sys.modules if name.startswith(MODULE_PREFIX)]
    assert len(names) == len(set(names))


async def test_concurrent_reloads_are_coalesced(tmp_path: Path, monkeypatch):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    for index in range(4):
        write_tool(tool_dir(ws), f"tool_{index}", f"Tool{index}")

    original = manager._build
    calls = 0

    def slow_build(previous, generation):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return original(previous, generation)

    monkeypatch.setattr(manager, "_build", slow_build)
    await asyncio.gather(*(manager.reload() for _ in range(6)))
    # One rebuild runs, the rest coalesce into at most one follow-up.
    assert calls <= 2
    assert manager.generation == 1


# ---------------------------------------------------------------------------
# Retirement and cleanup
# ---------------------------------------------------------------------------


async def test_pinned_generation_keeps_module_until_lease_release(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha", description="v1")
    await manager.reload()
    old_module = next(iter(manager.manifest.modules))

    lease = manager.ref.pin()
    write_tool(tool_dir(ws), "alpha", "Alpha", description="v2")
    await manager.reload()
    assert manager.generation == 2
    assert old_module in sys.modules  # pinned generation still owns it
    assert manager.ref.retired_generations == (1,)

    lease.release()
    assert old_module not in sys.modules
    assert manager.ref.retired_generations == ()
    assert manager.cleanup_failures == ()


async def test_cleanup_failure_is_reported_without_rollback(tmp_path: Path, monkeypatch):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha", description="v1")
    await manager.reload()
    lease = manager.ref.pin()

    def exploding_release(module_name):
        raise RuntimeError("cannot release")

    monkeypatch.setattr(manager.loader, "release_module", exploding_release)
    write_tool(tool_dir(ws), "alpha", "Alpha", description="v2")
    await manager.reload()
    assert manager.generation == 2
    assert "Alpha" in manager.manifest.tools

    lease.release()
    failures = manager.cleanup_failures
    assert failures
    assert failures[0].generation == 1
    # The swap was not rolled back by the cleanup failure.
    assert manager.generation == 2
    assert manager.manifest.tools["Alpha"].spec.description == "v2"


# ---------------------------------------------------------------------------
# ext.enabled
# ---------------------------------------------------------------------------


async def test_ext_disabled_does_not_discover_or_watch(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config(enabled=False))
    write_tool(tool_dir(ws), "alpha", "Alpha")
    report = await manager.reload()
    assert report.changed is False
    assert "Alpha" not in manager.manifest.tools
    assert manager.start() is False
    assert manager._watch_task is None


async def test_toggling_ext_disabled_removes_extensions(tmp_path: Path):
    manager, box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    assert "Alpha" in manager.manifest.tools
    box.config = make_config(enabled=False)
    report = await manager.reload()
    assert report.changed is True
    assert report.diff.tools.removed == ("Alpha",)
    assert "Alpha" not in manager.manifest.tools
    assert manager.manifest.modules == {}


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


async def test_skills_discovered_and_unchanged_objects_reused(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_skill(ws, "alpha")
    first = await manager.reload()
    assert first.changed is True
    assert first.diff.skills.added == ("alpha",)
    skill = manager.manifest.skills["alpha"]

    second = await manager.reload()
    assert second.changed is False
    # The unchanged skill is the *same* immutable object, so the diff is a no-op.
    assert manager.manifest.skills["alpha"] is skill

    write_skill(ws, "alpha", body="CHANGED")
    third = await manager.reload()
    assert third.changed is True
    assert third.diff.skills.changed == ("alpha",)


async def test_runtime_bus_sink_seam(tmp_path: Path):
    bus = Bus(maxsize=64)
    subscription = bus.subscribe()
    manager, _box, ws, _home = manager_for(tmp_path, make_config(), sink=bus)
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    seen: list[object] = []
    for _ in range(3):
        try:
            seen.append(await asyncio.wait_for(subscription.__anext__(), timeout=0.3))
        except TimeoutError:
            break
    assert "ext.manifest_changed" in [event.type for event in seen]

    # A closed bus is swallowed by the sink seam and never breaks a reload.
    await bus.aclose()
    (tool_dir(ws) / "alpha.py").unlink()
    report = await manager.reload()
    assert report.changed is True


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


async def test_watcher_create_change_delete_and_close(tmp_path: Path):
    manager, _box, ws, _home = manager_for(
        tmp_path, make_config(interval_ms=20)
    )
    await manager.reload()
    assert manager.start() is True
    assert manager.start() is False  # idempotent

    write_tool(tool_dir(ws), "alpha", "Alpha", description="one")
    assert await wait_until(lambda: "Alpha" in manager.manifest.tools)

    write_tool(tool_dir(ws), "alpha", "Alpha", description="two")
    assert await wait_until(
        lambda: manager.manifest.tools["Alpha"].spec.description == "two"
    )

    (tool_dir(ws) / "alpha.py").unlink()
    assert await wait_until(lambda: "Alpha" not in manager.manifest.tools)

    await manager.aclose()
    assert manager._watch_task is None
    await manager.aclose()  # idempotent close


async def test_start_is_disabled_when_interval_is_zero(tmp_path: Path):
    manager, _box, _ws, _home = manager_for(tmp_path, make_config(interval_ms=0))
    await manager.reload()
    assert manager.start() is False
    assert manager._watch_task is None


# ---------------------------------------------------------------------------
# Cancellation and unexpected-failure safety
# ---------------------------------------------------------------------------


def _staged_python_files(tmp_path: Path) -> list[Path]:
    stage = tmp_path / "stage"
    return sorted(stage.glob("*.py")) if stage.exists() else []


def _loader_modules() -> list[str]:
    return [name for name in sys.modules if name.startswith(MODULE_PREFIX)]


async def test_cancel_during_slow_build_drains_releases_and_serializes(
    tmp_path: Path, monkeypatch
):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    baseline_modules = set(_loader_modules())

    original = manager._build
    running = 0
    max_running = 0
    started = threading.Event()

    def slow_build(previous, generation):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        started.set()
        try:
            time.sleep(0.2)
            return original(previous, generation)
        finally:
            running -= 1

    monkeypatch.setattr(manager, "_build", slow_build)

    for _ in range(3):
        started.clear()
        task = asyncio.ensure_future(manager.reload())
        assert await asyncio.to_thread(started.wait, 2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The orphan build was drained, its module/staged copy released, and its
        # cancelled result never swapped in.
        assert manager.generation == 0
        assert manager.loader.owned_modules == ()
        assert set(_loader_modules()) == baseline_modules
        assert _staged_python_files(tmp_path) == []

    # A second build can only ever run after the orphan is drained.
    report = await manager.reload()
    assert report.generation == 1
    assert max_running == 1
    await manager.aclose()


async def test_cancel_during_entry_drain_reraised_and_starts_no_build(
    tmp_path: Path, monkeypatch
):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    loop = asyncio.get_running_loop()
    # Simulate a worker orphaned by a previous cancellation still touching the
    # loader when the next reload begins its entry drain.
    orphan = loop.run_in_executor(None, time.sleep, 0.3)
    manager._inflight_build = orphan

    original = manager._build
    calls = 0

    def counting_build(previous, generation):
        nonlocal calls
        calls += 1
        return original(previous, generation)

    monkeypatch.setattr(manager, "_build", counting_build)
    task = asyncio.ensure_future(manager.reload())
    await asyncio.sleep(0.05)  # let it enter the entry drain
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.shield(orphan) is None  # the orphan was drained
    assert calls == 0  # no second build started
    assert manager.generation == 0
    assert manager.loader.owned_modules == ()
    assert _staged_python_files(tmp_path) == []
    await manager.aclose()


async def test_build_staged_is_cleared_on_success_and_close(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    # A successful build's staged copies are owned by the manifest, so the
    # transient build list must not retain them.
    assert manager._build_staged == []
    await manager.aclose()
    assert manager._build_staged == []


async def test_unexpected_build_exception_is_sanitized_and_cleaned(
    tmp_path: Path, monkeypatch
):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    baseline_modules = set(_loader_modules())
    original = manager._build

    def exploding(previous, generation):
        original(previous, generation)  # load a module, then blow up
        raise RuntimeError("boom token=sk-should-not-leak")

    monkeypatch.setattr(manager, "_build", exploding)
    sink = RecordingSink()
    report = await manager.reload(sink=sink)

    assert report.changed is False
    assert report.failed
    assert report.failed[0].error_type == "RuntimeError"
    assert "sk-should-not-leak" not in str(report.to_dict())
    assert "ext.failed" in sink.types
    assert manager.generation == 0
    assert manager.loader.owned_modules == ()
    assert set(_loader_modules()) == baseline_modules
    assert _staged_python_files(tmp_path) == []
    await manager.aclose()


async def test_aclose_releases_live_modules_and_staged_copies(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    modules = list(manager.manifest.modules)
    assert modules
    assert _staged_python_files(tmp_path)
    assert all(name in sys.modules for name in modules)

    await manager.aclose()
    assert all(name not in sys.modules for name in modules)
    assert _staged_python_files(tmp_path) == []
    await manager.aclose()  # idempotent


async def test_aclose_deferrs_a_pinned_retired_generation_until_lease_release(
    tmp_path: Path,
):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha", description="v1")
    await manager.reload()
    lease = manager.ref.pin()
    write_tool(tool_dir(ws), "alpha", "Alpha", description="v2")
    await manager.reload()
    assert manager.ref.retired_generations == (1,)

    modules = set(manager.manifest.modules) | set(manager._staged_paths)
    assert len(modules) == 2  # both the retired and the live generation
    live_module = next(iter(manager.manifest.modules))
    retired_module = next(iter(modules - {live_module}))
    live_staged = manager._staged_paths[live_module]
    retired_staged = manager._staged_paths[retired_module]

    await manager.aclose()
    # The unpinned live generation is retired by the terminal swap and cleaned.
    assert live_module not in sys.modules
    assert live_staged.exists() is False
    # The pinned retired generation survives close: releasing it directly would
    # drop a module an in-flight call can still be running.
    assert retired_module in sys.modules
    assert retired_staged.exists() is True

    lease.release()
    assert retired_module not in sys.modules
    assert retired_staged.exists() is False
    assert manager.ref.retired_generations == ()
    assert manager.cleanup_failures == ()


async def test_aclose_defers_a_pinned_current_generation(tmp_path: Path):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    await manager.reload()
    live_module = next(iter(manager.manifest.modules))
    live_staged = manager._staged_paths[live_module]
    lease = manager.ref.pin()

    await manager.aclose()
    # ``aclose`` returns while a pin is live: the empty terminal generation is
    # installed but its predecessor is retired, not released.
    assert manager.closed is True
    assert live_module in sys.modules
    assert live_staged.exists() is True
    assert manager.ref.pinned_generations == {1: 1}
    assert manager.ref.retired_generations == (1,)

    lease.release()
    assert live_module not in sys.modules
    assert live_staged.exists() is False
    assert manager.ref.retired_generations == ()
    assert manager.cleanup_failures == ()


async def test_reload_after_aclose_is_rejected(tmp_path: Path):
    manager, _box, _ws, _home = manager_for(tmp_path, make_config())
    await manager.aclose()
    assert manager.closed is True
    with pytest.raises(ManagerClosed):
        await manager.reload()


async def test_start_after_aclose_returns_false(tmp_path: Path):
    manager, _box, _ws, _home = manager_for(tmp_path, make_config(interval_ms=20))
    await manager.reload()
    assert manager.start() is True
    await manager.aclose()
    assert manager.start() is False


async def test_close_refuses_a_racing_rebuild_swap(tmp_path: Path, monkeypatch):
    manager, _box, ws, _home = manager_for(tmp_path, make_config())
    write_tool(tool_dir(ws), "alpha", "Alpha")
    baseline_modules = set(_loader_modules())
    original = manager._build
    started = threading.Event()

    def slow_build(previous, generation):
        started.set()
        time.sleep(0.2)
        return original(previous, generation)

    monkeypatch.setattr(manager, "_build", slow_build)
    task = asyncio.ensure_future(manager.reload())
    assert await asyncio.to_thread(started.wait, 2.0)

    await manager.aclose()
    report = await task

    # The racing rebuild observed the closed flag and never swapped; everything
    # it loaded was released.
    assert report.changed is False
    assert manager.generation == 0
    assert "Alpha" not in manager.manifest.tools
    assert manager.loader.owned_modules == ()
    assert set(_loader_modules()) == baseline_modules
    assert _staged_python_files(tmp_path) == []


async def test_shadow_is_recorded_and_emitted(tmp_path: Path):
    sink = RecordingSink()
    manager, _box, ws, home = manager_for(tmp_path, make_config(), sink=sink)
    write_tool(tool_dir(ws), "ws_shared", "Shared", description="from-workspace")
    write_tool(tool_dir(home), "user_shared", "Shared", description="from-user")
    report = await manager.reload()

    assert report.changed is True
    assert manager.manifest.tools["Shared"].spec.description == "from-workspace"
    assert "ext.tool_shadowed" in sink.types
    shadow_events = [e for e in sink.events if e.type == "ext.tool_shadowed"]
    assert shadow_events
    assert "shadowed" in shadow_events[0].data["error"]
    assert shadow_events[0].data["kind"] == "shadowed"
    shadow_rows = [
        row for row in manager.diagnostics() if row.get("kind") == "shadowed"
    ]
    assert shadow_rows
    await manager.aclose()


async def test_shadow_event_not_repeated_on_a_noop_reload(tmp_path: Path):
    sink = RecordingSink()
    manager, _box, ws, home = manager_for(tmp_path, make_config(), sink=sink)
    write_tool(tool_dir(ws), "ws_shared", "Shared", description="from-workspace")
    write_tool(tool_dir(home), "user_shared", "Shared", description="from-user")
    first = await manager.reload()
    assert first.changed is True
    assert [e for e in sink.events if e.type == "ext.tool_shadowed"]

    sink.events.clear()
    sink.types.clear()
    second = await manager.reload()
    assert second.changed is False
    # The shadow set did not change, so no shadow event is re-emitted.
    assert [e for e in sink.events if e.type == "ext.tool_shadowed"] == []
    await manager.aclose()


async def test_watcher_survives_an_unexpected_poll_exception(tmp_path, monkeypatch):
    sink = RecordingSink()
    manager, _box, _ws, _home = manager_for(
        tmp_path, make_config(interval_ms=20), sink=sink
    )
    await manager.reload()
    calls = 0
    original = manager._poll_watchers

    def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("poll blew up")
        return original()

    monkeypatch.setattr(manager, "_poll_watchers", flaky)
    assert manager.start() is True
    assert await wait_until(lambda: "ext.failed" in sink.types)
    # The watcher loop survived the raise and polled again.
    assert await wait_until(lambda: calls >= 2)
    assert manager._watch_task is not None and not manager._watch_task.done()
    await manager.aclose()
