"""P4-H security: exact bytes, symlinks, TOCTOU, collisions, and sanitization.

The hot-load path is the one place arbitrary workspace code becomes executable.
These tests pin the boundary the plan states plainly (section 11): quarantine
validates the exact bytes, refuses symlinks and races, never lets a hot tool
claim a builtin name, keeps diagnostics free of secrets and control characters,
and honors ``ext.enabled = false``. The trust boundary is documented, not
implied.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from nexus.config import Config
from nexus.config.schema import ConfigV2, ExtSection
from nexus.ext import ExtensionManager
from nexus.ext import quarantine as quarantine_module
from nexus.ext.quarantine import Quarantine, QuarantineCode
from nexus.tools.builtin import BUILTIN_TOOLS
from nexus.tools.loader import ToolLoader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(*, enabled: bool = True) -> Config:
    return Config(
        v2=ConfigV2(
            ext=ExtSection(
                enabled=enabled,
                watch_interval_ms=0,
                dirs=[".nexus/tools"],
                quarantine=False,
                max_file_bytes=100_000,
            )
        )
    )


def tool_source(name: str, *, body: str = "ok") -> str:
    return (
        "from typing import Any\n"
        "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
        "SPEC = ToolSpec(name=" + repr(name) + ", description='d', "
        "input_schema={'type': 'object'}, bundle='ext')\n\n"
        "async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:\n"
        "    return ToolExecutionResult.text(" + repr(body) + ")\n"
    )


def make_manager(tmp_path: Path, config: Config) -> tuple[ExtensionManager, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    quarantine = Quarantine(
        max_file_bytes=100_000,
        timeout_s=0.3,
        root=workspace,
        stage_root=tmp_path / "stage",
    )
    manager = ExtensionManager(
        workspace,
        home=tmp_path / "home",
        config_loader=lambda: config,
        loader=ToolLoader(),
        quarantine=quarantine,
        builtin_tools=BUILTIN_TOOLS,
    )
    return manager, workspace


def write_skill(workspace: Path, name: str, *, tools: dict[str, str]) -> Path:
    directory = workspace / ".nexus" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\nBODY\n", encoding="utf-8"
    )
    for filename, content in tools.items():
        target = directory / "tools" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# Exact bytes
# ---------------------------------------------------------------------------


async def test_bundled_skill_tool_loads_the_exact_bytes(tmp_path: Path):
    manager, workspace = make_manager(tmp_path, make_config())
    source = tool_source("SkillPing", body="exact")
    write_skill(workspace, "reader", tools={"skill_ping.py": source})
    report = await manager.reload()
    assert report.ok is True

    entry = manager.manifest.skill_tools["reader"]
    module_name = entry.modules[0]
    handle = manager.manifest.modules[module_name]
    assert handle.sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert handle.origin == "skill"
    await manager.aclose()


def test_loader_refuses_a_skill_stage_that_changed_after_validation(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    quarantine = Quarantine(
        max_file_bytes=100_000, timeout_s=0.3, root=workspace,
        stage_root=tmp_path / "stage",
    )
    source = workspace / "skill_tool.py"
    source.write_text(tool_source("SkillPing"), encoding="utf-8")
    staged = quarantine.stage(quarantine.open(source, origin="skill"))
    assert staged.private is True

    # Swap the staged bytes after validation; the loader must refuse.
    staged.path.write_bytes(staged.data + b"\n# tampered\n")
    loader = ToolLoader()
    outcome = loader.load(staged, 1, origin="skill")
    assert outcome.code is QuarantineCode.HASH_MISMATCH
    assert loader.owned_modules == ()


# ---------------------------------------------------------------------------
# Symlinks
# ---------------------------------------------------------------------------


async def test_bundled_skill_tool_symlink_is_refused(tmp_path: Path):
    manager, workspace = make_manager(tmp_path, make_config())
    directory = write_skill(workspace, "reader", tools={})
    (directory / "tools").mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.py"
    outside.write_text(tool_source("Outside"), encoding="utf-8")
    (directory / "tools" / "linked.py").symlink_to(outside)

    report = await manager.reload()

    assert report.failed
    assert report.failed[0].kind == "skill"
    assert report.failed[0].name == "reader"
    assert "symlink" in report.failed[0].error.lower()
    assert "reader" not in manager.manifest.skills
    await manager.aclose()


def test_loader_refuses_a_symlinked_stage(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    quarantine = Quarantine(
        max_file_bytes=100_000, timeout_s=0.3, root=workspace,
        stage_root=tmp_path / "stage",
    )
    source = workspace / "tool.py"
    source.write_text(tool_source("SkillPing"), encoding="utf-8")
    staged = quarantine.stage(quarantine.open(source, origin="skill"))
    original = staged.path.read_bytes()
    staged.path.unlink()
    other = tmp_path / "other.py"
    other.write_bytes(original + b"\n# different\n")
    staged.path.symlink_to(other)

    loader = ToolLoader()
    outcome = loader.load(staged, 1, origin="skill")
    assert outcome.code is QuarantineCode.HASH_MISMATCH
    assert loader.owned_modules == ()


# ---------------------------------------------------------------------------
# Collisions
# ---------------------------------------------------------------------------


async def test_bundled_skill_tool_builtin_collision_fails(tmp_path: Path):
    manager, workspace = make_manager(tmp_path, make_config())
    write_skill(workspace, "reader", tools={"shadow.py": tool_source("Read")})
    report = await manager.reload()
    assert report.failed
    assert report.failed[0].error_type == "collision"
    assert "read" in manager.manifest.tools
    await manager.aclose()


async def test_bundled_skill_tool_collides_with_global_ext_tool(tmp_path: Path):
    manager, workspace = make_manager(tmp_path, make_config())
    tools_dir = workspace / ".nexus" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "global_ping.py").write_text(
        tool_source("SkillPing", body="global"), encoding="utf-8"
    )
    write_skill(workspace, "reader", tools={"skill_ping.py": tool_source("SkillPing")})

    report = await manager.reload()

    assert report.failed
    assert report.failed[0].error_type == "collision"
    assert "reader" not in manager.manifest.skills
    await manager.aclose()


async def test_two_skills_cannot_bundle_the_same_tool_name(tmp_path: Path):
    manager, workspace = make_manager(tmp_path, make_config())
    write_skill(workspace, "alpha", tools={"ping.py": tool_source("SharedPing")})
    write_skill(workspace, "beta", tools={"ping.py": tool_source("SharedPing")})
    report = await manager.reload()
    assert report.failed
    assert report.failed[0].error_type == "collision"
    await manager.aclose()


# ---------------------------------------------------------------------------
# Diagnostics: no secrets, no control characters
# ---------------------------------------------------------------------------


async def test_import_error_diagnostic_never_leaks_a_secret(tmp_path: Path):
    manager, workspace = make_manager(tmp_path, make_config())
    secret = "sk-" + "a" * 40
    write_skill(
        workspace,
        "reader",
        tools={
            "boom.py": (
                "raise RuntimeError('token=" + secret + "')\n"
                + tool_source("Boom")
            )
        },
    )
    report = await manager.reload()
    assert report.failed
    blob = str(report.to_dict()) + str(manager.diagnostics())
    assert secret not in blob
    assert "Traceback" not in blob
    await manager.aclose()


async def test_diagnostics_scrub_control_characters(tmp_path: Path):
    manager, workspace = make_manager(tmp_path, make_config())
    write_skill(
        workspace,
        "reader",
        tools={"bad.py": "def broken(:\n\x01\x02\x7f\n"},
    )
    await manager.reload()
    for row in manager.diagnostics():
        for value in row.values():
            if isinstance(value, str):
                assert all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)
    await manager.aclose()


# ---------------------------------------------------------------------------
# ext.enabled = false
# ---------------------------------------------------------------------------


async def test_ext_disabled_does_not_load_skill_tools(tmp_path: Path):
    manager, workspace = make_manager(tmp_path, make_config(enabled=False))
    write_skill(workspace, "reader", tools={"skill_ping.py": tool_source("SkillPing")})
    report = await manager.reload()
    assert report.changed is False
    assert manager.manifest.skills == {}
    assert manager.manifest.skill_tools == {}
    assert manager.manifest.modules == {}
    assert not (workspace / ".nexus" / "tools" / "_template.py").exists()
    await manager.aclose()


# ---------------------------------------------------------------------------
# Trust boundary is documented
# ---------------------------------------------------------------------------


def test_trust_boundary_is_documented():
    source = quarantine_module.__doc__ or ""
    assert "trusted code" in source
    assert "gates" in source
    assert "not" in source and "loading" in source
