"""P4-H stress: reload churn, module/lease lifecycle, and isolation.

The hot-reload design must stay leak-free and race-safe under sustained churn:

* 200 content-changing reloads leave exactly the active generation's modules in
  ``sys.modules``, retire the rest, collect them (weakrefs die), clean their
  staged copies, and keep allocated memory bounded;
* an import-time hang is killed and never wedges the harness;
* deleting a tool while a pinned call is in flight is safe: the call keeps the
  generation it started on and the module is dropped only after the lease ends;
* two sessions pinned to different generations (one with a skill activation)
  keep their modules and tool sets isolated.

Everything is offline; extensions are real files.
"""
from __future__ import annotations

import asyncio
import gc
import sys
import time
import tracemalloc
import weakref
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ExtSection
from nexus.ext import ExtensionManager
from nexus.ext.quarantine import Quarantine
from nexus.tools.loader import MODULE_PREFIX, ToolLoader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    *,
    enabled: bool = True,
    quarantine: bool = False,
    interval_ms: int = 0,
) -> Config:
    return Config(
        v2=ConfigV2(
            ext=ExtSection(
                enabled=enabled,
                watch_interval_ms=interval_ms,
                dirs=[".nexus/tools", "~/.nexus/tools"],
                quarantine=quarantine,
                max_file_bytes=100_000,
            )
        )
    )


class ConfigBox:
    def __init__(self, config: Config):
        self.config = config

    def __call__(self) -> Config:
        return self.config


def tool_source(name: str, *, body: str = "ok") -> str:
    return (
        "from typing import Any\n"
        "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
        "SPEC = ToolSpec(name=" + repr(name) + ", description='d', "
        "input_schema={'type': 'object'}, bundle='ext')\n\n"
        "async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:\n"
        "    return ToolExecutionResult.text(" + repr(body) + ")\n"
    )


SLOW_SOURCE = (
    "from typing import Any\n"
    "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
    "SPEC = ToolSpec(name='Slow', description='slow', "
    "input_schema={'type': 'object'}, bundle='ext')\n\n"
    "async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:\n"
    "    release = globals().get('RELEASE')\n"
    "    if release is not None:\n"
    "        await release.wait()\n"
    "    return ToolExecutionResult.text('old-value')\n"
)


def write_tool(root: Path, stem: str, source: str) -> Path:
    directory = root / ".nexus" / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.py"
    path.write_text(source, encoding="utf-8")
    return path


def manager_for(
    tmp_path: Path,
    config: Config,
    *,
    quarantine_timeout: float = 0.3,
) -> tuple[ExtensionManager, Path, Path]:
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    workspace.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    quarantine = Quarantine(
        max_file_bytes=100_000,
        timeout_s=quarantine_timeout,
        root=workspace,
        stage_root=tmp_path / "stage",
    )
    manager = ExtensionManager(
        workspace,
        home=home,
        config_loader=ConfigBox(config),
        loader=ToolLoader(),
        quarantine=quarantine,
    )
    return manager, workspace, tmp_path / "stage"


@pytest.fixture(autouse=True)
def _clean_sys_modules():
    before = set(sys.modules)
    yield
    for name in set(sys.modules):
        if name.startswith(MODULE_PREFIX) and name not in before:
            sys.modules.pop(name, None)


def _active_modules(manager: ExtensionManager) -> set[str]:
    return set(manager.manifest.modules)


def _owned_modules(manager: ExtensionManager) -> set[str]:
    """The modules *this* loader owns (scoped, so suite-wide churn cannot leak in)."""
    return set(manager.loader.owned_modules)


# ---------------------------------------------------------------------------
# 200 content-changing reloads
# ---------------------------------------------------------------------------


async def test_two_hundred_reloads_do_not_leak(tmp_path: Path):
    manager, workspace, stage = manager_for(tmp_path, make_config())
    write_tool(workspace, "alpha", tool_source("Alpha", body="v0"))
    await manager.reload()

    refs: list[weakref.ref] = []
    tracemalloc.start()
    try:
        # Warm up so one-time allocations do not count against the bound.
        for index in range(20):
            write_tool(workspace, "alpha", tool_source("Alpha", body=f"w{index}"))
            await manager.reload()
        gc.collect()
        baseline = tracemalloc.take_snapshot()

        for index in range(200):
            write_tool(workspace, "alpha", tool_source("Alpha", body=f"v{index}"))
            report = await manager.reload()
            assert report.ok is True
            assert report.diff.tools.changed == ("Alpha",)
            module = next(iter(manager.manifest.modules.values())).module
            refs.append(weakref.ref(module))

        gc.collect()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()

    assert manager.generation == 221  # 1 initial + 20 warmup + 200 churn
    # Only the active generation's module is importable and owned by the loader.
    assert _owned_modules(manager) == _active_modules(manager)
    assert len(_active_modules(manager)) == 1
    assert all(name in sys.modules for name in _active_modules(manager))
    # Every retired module was collected; the current one is alive.
    assert refs[-1]() is not None
    assert all(ref() is None for ref in refs[:-1]), "a retired module leaked"
    # The retired generations' private staged copies were cleaned up.
    staged = sorted(stage.glob("*.py"))
    assert len(staged) == 1
    # Allocated memory growth is bounded (generous; catches real leaks).
    growth = sum(stat.size_diff for stat in after.compare_to(baseline, "filename"))
    assert growth < 8 * 1024 * 1024, f"tracemalloc growth {growth} bytes"
    await manager.aclose()


async def test_two_hundred_skill_tool_reloads_do_not_leak(tmp_path: Path):
    manager, workspace, stage = manager_for(tmp_path, make_config())
    skill = workspace / ".nexus" / "skills" / "reader"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: reader\ndescription: read\n---\nBODY\n", encoding="utf-8"
    )
    bundled = skill / "tools"
    bundled.mkdir()

    refs: list[weakref.ref] = []
    for index in range(200):
        (bundled / "skill_ping.py").write_text(
            tool_source("SkillPing", body=f"v{index}"), encoding="utf-8"
        )
        report = await manager.reload()
        assert report.ok is True
        entry = manager.manifest.skill_tools.get("reader")
        assert entry is not None and entry.tool_names == ("SkillPing",)
        module = next(iter(manager.manifest.modules.values())).module
        refs.append(weakref.ref(module))

    gc.collect()
    assert manager.generation == 200
    assert _owned_modules(manager) == _active_modules(manager)
    assert all(ref() is None for ref in refs[:-1])
    assert refs[-1]() is not None
    assert len(list(stage.glob("*.py"))) == 1
    await manager.aclose()


# ---------------------------------------------------------------------------
# Import-time hang is killed
# ---------------------------------------------------------------------------


async def test_deleting_a_skill_releases_its_bundled_modules(tmp_path: Path):
    import shutil

    manager, workspace, stage = manager_for(tmp_path, make_config())
    skill = workspace / ".nexus" / "skills" / "reader"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: reader\ndescription: d\n---\nBODY\n", encoding="utf-8"
    )
    (skill / "tools").mkdir()
    (skill / "tools" / "skill_ping.py").write_text(
        tool_source("SkillPing"), encoding="utf-8"
    )
    await manager.reload()
    module = next(iter(manager.manifest.modules))
    assert module in sys.modules

    shutil.rmtree(skill)
    report = await manager.reload()

    assert report.ok is True
    assert "reader" not in manager.manifest.skills
    assert manager.manifest.skill_tools == {}
    assert manager.manifest.modules == {}
    assert module not in sys.modules
    assert list(stage.glob("*.py")) == []
    await manager.aclose()


async def test_import_hang_is_killed_and_keeps_previous_manifest(tmp_path: Path):
    manager, workspace, _stage = manager_for(
        tmp_path, make_config(quarantine=True), quarantine_timeout=0.3
    )
    write_tool(workspace, "good", tool_source("Good", body="good"))
    await manager.reload()
    assert manager.generation == 1

    write_tool(workspace, "hangs", tool_source("Hangs") + "while True:\n    pass\n")
    started = time.monotonic()
    report = await manager.reload()
    elapsed = time.monotonic() - started

    assert report.failed
    assert report.failed[0].error_type == "import_timeout"
    assert elapsed < 5.0  # the child was killed, not awaited forever
    assert manager.generation == 1
    assert "Good" in manager.manifest.tools
    assert "Hangs" not in manager.manifest.tools
    await manager.aclose()


# ---------------------------------------------------------------------------
# Delete mid-call
# ---------------------------------------------------------------------------


async def test_delete_mid_call_is_safe(tmp_path: Path):
    manager, workspace, _stage = manager_for(tmp_path, make_config())
    path = write_tool(workspace, "slow", SLOW_SOURCE)
    await manager.reload()
    generation = manager.generation
    module_name = next(iter(manager.manifest.modules))
    module = sys.modules[module_name]
    release = asyncio.Event()
    module.RELEASE = release

    lease = manager.ref.pin()
    tool = manager.manifest.tools["Slow"]
    task = asyncio.ensure_future(tool.run({}, None))
    await asyncio.sleep(0.02)
    assert not task.done()

    # Delete the source and reload while the call is parked.
    path.unlink()
    report = await manager.reload()
    assert report.ok is True
    assert "Slow" not in manager.manifest.tools
    assert module_name in sys.modules  # pinned generation still owns it

    release.set()
    result = await task
    assert result.content[0].text == "old-value"

    lease.release()
    assert module_name not in sys.modules
    assert manager.ref.retired_generations == ()
    assert manager.cleanup_failures == ()
    assert generation == 1
    await manager.aclose()


# ---------------------------------------------------------------------------
# Two sessions pinned to different generations stay isolated
# ---------------------------------------------------------------------------


async def test_two_pinned_generations_keep_their_modules(tmp_path: Path):
    manager, workspace, _stage = manager_for(tmp_path, make_config())
    write_tool(workspace, "alpha", tool_source("Alpha", body="v1"))
    await manager.reload()
    first_generation = manager.generation
    first_module = next(iter(manager.manifest.modules))

    # Pin generation 1, then retire it with a newer generation.
    first_lease = manager.ref.pin()
    write_tool(workspace, "alpha", tool_source("Alpha", body="v2"))
    await manager.reload()
    second_generation = manager.generation
    second_module = next(iter(manager.manifest.modules))
    assert second_generation > first_generation

    # Pin generation 2 and retire it too, so both are retired-but-pinned.
    second_lease = manager.ref.pin()
    write_tool(workspace, "alpha", tool_source("Alpha", body="v3"))
    await manager.reload()
    third_generation = manager.generation
    third_module = next(iter(manager.manifest.modules))
    assert third_generation > second_generation

    # Every pinned generation keeps its module importable.
    assert first_module in sys.modules
    assert second_module in sys.modules
    assert third_module in sys.modules
    assert first_generation in manager.ref.pinned_generations
    assert second_generation in manager.ref.pinned_generations

    first_lease.release()
    assert first_module not in sys.modules
    assert second_module in sys.modules
    assert third_module in sys.modules
    second_lease.release()
    assert second_module not in sys.modules
    # The current generation's module stays loaded until it is itself retired.
    assert third_module in sys.modules
    await manager.aclose()
