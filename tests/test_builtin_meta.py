"""Tests for the Phase 4 meta builtins (ReloadExtensions/ListExtensions/WriteTool).

Covers the plan sections 6.3-6.5 control surface:

* schemas and service absence for each tool;
* ``ReloadExtensions`` calls the serialized manager with ``trigger="tool"``,
  returns the concise diff on success, and reports a sanitized, actionable
  error (never a traceback or source body) on failure;
* ``ListExtensions`` is read-only and lists active tools/modules, skills with
  their scope, and quarantined/shadowed diagnostics without leaking source
  bodies or secrets;
* ``WriteTool`` validates a simple ``.py`` filename (no traversal, separator,
  symlink, reserved name, or leading underscore), bounds the UTF-8 payload,
  writes atomically under the configured filesystem roots, requires an explicit
  ``overwrite``, and never auto-reloads.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ExtSection,
    PermissionsSection,
)
from nexus.errors import ToolError
from nexus.ext import ExtensionManager
from nexus.tools.builtin import meta
from nexus.tools.builtin.meta import (
    LIST_EXTENSIONS_SPEC,
    RELOAD_EXTENSIONS_SPEC,
    WRITE_TOOL_SPEC,
    validate_tool_filename,
)
from nexus.tools.manager import ToolManager
from nexus.tools.spec import (
    RegisteredTool,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool(name: str) -> RegisteredTool:
    async def _run(args, ctx):
        return ToolExecutionResult.text("ok")

    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=f"{name} test tool",
            input_schema={"type": "object"},
            bundle="fs",
        ),
        run=_run,
        origin="builtin",
    )


TOOL_SOURCE = (
    "from typing import Any\n"
    "from nexus.tools.spec import ToolContext, ToolExecutionResult\n"
    "SPEC = {'name': 'MetricsQuery', 'description': 'query metrics',\n"
    "        'input_schema': {'type': 'object'}, 'bundle': 'ext'}\n\n"
    "async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:\n"
    "    return ToolExecutionResult.text('ok')\n"
)


def make_config(
    *,
    write_roots: list[str] | None = None,
    profile: str = "coding",
) -> Config:
    return Config(
        v2=ConfigV2(
            agent=AgentSection(profile=profile),
            permissions=PermissionsSection(write_roots=write_roots or ["./"]),
            ext=ExtSection(
                enabled=True,
                dirs=[".nexus/tools"],
                quarantine=False,
                max_file_bytes=100_000,
            ),
        )
    )


def make_manager(workspace: Path, config: Config, tool_names: list[str]) -> ExtensionManager:
    return ExtensionManager(
        workspace,
        home=workspace / "home",
        config=config,
        config_loader=lambda: config,
        builtin_tools=[_tool(name) for name in tool_names],
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, data: dict | None = None) -> None:
        self.events.append((event_type, dict(data or {})))

    def types(self) -> list[str]:
        return [event_type for event_type, _data in self.events]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "home").mkdir()
    return ws


def ctx_for(
    workspace: Path,
    *,
    extensions: object | None = None,
    config: Config | None = None,
    recorder: Recorder | None = None,
) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="s1",
        turn_id="t1",
        config=config or make_config(),
        extensions=extensions,
        emit=recorder.emit if recorder is not None else None,
    )


def write_source(workspace: Path, name: str, content: str) -> Path:
    directory = workspace / ".nexus" / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Specs and service absence
# ---------------------------------------------------------------------------


def test_reload_spec_shape():
    assert RELOAD_EXTENSIONS_SPEC.name == "ReloadExtensions"
    assert RELOAD_EXTENSIONS_SPEC.bundle == "meta"
    assert RELOAD_EXTENSIONS_SPEC.mutates is True
    assert RELOAD_EXTENSIONS_SPEC.concurrency == "exclusive"
    assert RELOAD_EXTENSIONS_SPEC.input_schema["additionalProperties"] is False


def test_list_spec_shape():
    assert LIST_EXTENSIONS_SPEC.name == "ListExtensions"
    assert LIST_EXTENSIONS_SPEC.bundle == "meta"
    assert LIST_EXTENSIONS_SPEC.mutates is False
    assert LIST_EXTENSIONS_SPEC.concurrency == "parallel"


def test_write_tool_spec_shape():
    assert WRITE_TOOL_SPEC.name == "WriteTool"
    assert WRITE_TOOL_SPEC.bundle == "meta"
    assert WRITE_TOOL_SPEC.mutates is True
    assert WRITE_TOOL_SPEC.concurrency == "exclusive"
    assert WRITE_TOOL_SPEC.input_schema["required"] == ["filename", "content"]
    assert WRITE_TOOL_SPEC.resolve_permission_key(
        {"filename": "x.py"}
    ) == ".agents/tools/x.py"


async def test_reload_requires_extension_service(workspace: Path):
    result = await meta.reload_extensions({}, ctx_for(workspace))
    assert result.is_error is True
    assert "no extension service" in result.content[0].text


def test_classify_diagnostics_recognizes_tool_shadow_kind():
    rows = [{"kind": "shadowed", "name": "user_shared", "error": "shadowed by x"}]
    shadowed, quarantined = meta._classify_diagnostics(rows)
    assert shadowed == rows
    assert quarantined == []


async def test_list_requires_extension_service(workspace: Path):
    result = await meta.list_extensions({}, ctx_for(workspace))
    assert result.is_error is True
    assert "no extension service" in result.content[0].text


# ---------------------------------------------------------------------------
# ReloadExtensions
# ---------------------------------------------------------------------------


async def test_reload_success_returns_concise_diff(workspace: Path):
    config = make_config(profile="coding_meta")
    manager = make_manager(workspace, config, ["Read"])
    write_source(workspace, "metrics_query.py", TOOL_SOURCE)
    ctx = ctx_for(workspace, extensions=manager, config=config)
    result = await meta.reload_extensions({}, ctx)
    assert result.is_error is False
    assert "ReloadExtensions" in result.content[0].text
    assert "+1 tool" in result.content[0].text
    assert "MetricsQuery" in manager.manifest.tools
    assert result.metrics["changed"] is True


async def test_reload_uses_tool_trigger_and_forwards_events(workspace: Path):
    config = make_config(profile="coding_meta")
    manager = make_manager(workspace, config, ["Read"])
    write_source(workspace, "metrics_query.py", TOOL_SOURCE)
    recorder = Recorder()
    ctx = ctx_for(workspace, extensions=manager, config=config, recorder=recorder)
    await meta.reload_extensions({}, ctx)
    assert "ext.manifest_changed" in recorder.types()


async def test_reload_failure_is_sanitized_and_actionable(workspace: Path):
    config = make_config()
    manager = make_manager(workspace, config, ["Read"])
    secret = "sk-" + "a" * 40
    write_source(workspace, "broken.py", f"def (:\n    password = '{secret}'\n")
    ctx = ctx_for(workspace, extensions=manager, config=config)
    result = await meta.reload_extensions({}, ctx)
    text = result.content[0].text
    # The previous manifest is intact and the failure is reported, sanitized.
    assert "broken" in text
    assert secret not in text
    assert "Traceback" not in text
    assert result.metrics["ok"] is False


async def test_reload_exception_is_sanitized(workspace: Path):
    class Exploding:
        generation = 0

        async def reload(self, trigger="api", sink=None):
            raise RuntimeError(f"boom {trigger} token=sk-{'b' * 40}")

    ctx = ctx_for(workspace, extensions=Exploding())
    result = await meta.reload_extensions({}, ctx)
    assert result.is_error is True
    text = result.content[0].text
    assert "reload failed" in text
    assert "sk-" + "b" * 40 not in text
    assert "previous manifest" in text


async def test_reload_accepts_service_without_sink_keyword(workspace: Path):
    class Minimal:
        generation = 0

        async def reload(self, trigger="api"):
            return {"summary": f"gen 0 -> 0: no changes ({trigger})", "ok": True}

    ctx = ctx_for(workspace, extensions=Minimal())
    result = await meta.reload_extensions({}, ctx)
    assert result.is_error is False
    assert "trigger" not in result.content[0].text  # summary is used verbatim


# ---------------------------------------------------------------------------
# ListExtensions
# ---------------------------------------------------------------------------


async def test_list_is_read_only_and_lists_the_world(workspace: Path):
    config = make_config()
    manager = make_manager(workspace, config, ["Read"])
    write_source(workspace, "metrics_query.py", TOOL_SOURCE)
    await manager.reload()
    skill_dir = workspace / ".nexus" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\nSECRET BODY\n",
        encoding="utf-8",
    )
    await manager.reload()
    generation = manager.generation

    ctx = ctx_for(workspace, extensions=manager, config=config)
    result = await meta.list_extensions({}, ctx)
    assert result.is_error is False
    text = result.content[0].text
    assert "Read" in text
    assert "MetricsQuery" in text
    assert "demo" in text
    assert "workspace" in text  # skill scope
    assert "SECRET BODY" not in text  # never a source body
    assert result.metrics["tools"] >= 2
    assert result.metrics["skills"] == 1
    # Read-only: no reload happened.
    assert manager.generation == generation


async def test_list_sanitizes_quarantine_secrets(workspace: Path):
    config = make_config()
    manager = make_manager(workspace, config, ["Read"])
    secret = "ghp_" + "c" * 30
    write_source(workspace, "bad.py", f"token = '{secret}'\ndef (:\n")
    await manager.reload()
    ctx = ctx_for(workspace, extensions=manager, config=config)
    result = await meta.list_extensions({}, ctx)
    text = result.content[0].text
    assert secret not in text
    assert "bad" in text
    assert result.metrics["quarantined"] >= 1


async def test_list_reports_shadowed_skills(workspace: Path):
    user_skill = workspace / "home" / ".nexus" / "skills" / "demo"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: user version\n---\nUSER\n",
        encoding="utf-8",
    )
    ws_skill = workspace / ".nexus" / "skills" / "demo"
    ws_skill.mkdir(parents=True)
    (ws_skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: workspace version\n---\nWS\n",
        encoding="utf-8",
    )
    config = make_config()
    manager = make_manager(workspace, config, ["Read"])
    await manager.reload()
    ctx = ctx_for(workspace, extensions=manager, config=config)
    result = await meta.list_extensions({}, ctx)
    text = result.content[0].text
    assert "workspace version" in text
    assert "Shadowed" in text
    assert result.metrics["shadowed"] >= 1


# ---------------------------------------------------------------------------
# WriteTool: filename validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "..",
        "../evil.py",
        "a/b.py",
        "a\\b.py",
        "/abs.py",
        "~/.nexus/tools/x.py",
        ".hidden.py",
        "_template.py",
        "__init__.py",
        "Read.py",  # reserved builtin name
        "read.py",  # reserved, case-folded
        "con.py",  # reserved device name
        "x.txt",
        "no-extension",
        "with space.py",
        "9lives.py",
        "a" * 200 + ".py",
    ],
)
def test_validate_tool_filename_rejects_bad_names(bad):
    with pytest.raises(ToolError):
        validate_tool_filename(bad)


@pytest.mark.parametrize("good", ["foo.py", "metrics_query.py", "A1_b2.py"])
def test_validate_tool_filename_accepts_simple_names(good):
    assert validate_tool_filename(good) == good


# ---------------------------------------------------------------------------
# WriteTool: writing
# ---------------------------------------------------------------------------


async def test_write_tool_creates_atomically_and_does_not_reload(workspace: Path):
    config = make_config()
    manager = make_manager(workspace, config, ["Read"])
    generation = manager.generation
    ctx = ctx_for(workspace, extensions=manager, config=config)
    result = await meta.write_tool(
        {"filename": "metrics_query.py", "content": TOOL_SOURCE}, ctx
    )
    assert result.is_error is False
    target = workspace / ".agents" / "tools" / "metrics_query.py"
    assert target.read_text(encoding="utf-8") == TOOL_SOURCE
    assert result.metrics["created"] is True
    assert result.metrics["reload_required"] is True
    # Atomic write leaves no temporary artifact behind.
    leftovers = [
        p.name for p in target.parent.iterdir() if p.name.startswith(".nexus-write-")
    ]
    assert leftovers == []
    # No auto-reload: the manifest is untouched.
    assert manager.generation == generation
    assert "MetricsQuery" not in manager.manifest.tools


async def test_write_tool_requires_overwrite_opt_in(workspace: Path):
    ctx = ctx_for(workspace)
    await meta.write_tool({"filename": "foo.py", "content": "x"}, ctx)
    again = await meta.write_tool({"filename": "foo.py", "content": "y"}, ctx)
    assert again.is_error is True
    assert "overwrite=true" in again.content[0].text
    replaced = await meta.write_tool(
        {"filename": "foo.py", "content": "z", "overwrite": True}, ctx
    )
    assert replaced.is_error is False
    assert (workspace / ".agents" / "tools" / "foo.py").read_text() == "z"


async def test_write_tool_rejects_oversize_and_nul(workspace: Path):
    config = make_config()
    config = Config(
        v2=ConfigV2(
            permissions=PermissionsSection(write_roots=["./"]),
            ext=ExtSection(
                enabled=True, dirs=[".nexus/tools"], max_file_bytes=32
            ),
        )
    )
    ctx = ctx_for(workspace, config=config)
    oversize = await meta.write_tool(
        {"filename": "big.py", "content": "A" * 64}, ctx
    )
    assert oversize.is_error is True
    assert "cap" in oversize.content[0].text
    nul = await meta.write_tool({"filename": "hasnul.py", "content": "a\x00b"}, ctx)
    assert nul.is_error is True
    assert "NUL" in nul.content[0].text


async def test_write_tool_refuses_symlink_target(workspace: Path):
    directory = workspace / ".agents" / "tools"
    directory.mkdir(parents=True)
    real = directory / "real.py"
    real.write_text("x", encoding="utf-8")
    (directory / "link.py").symlink_to(real)
    ctx = ctx_for(workspace)
    result = await meta.write_tool(
        {"filename": "link.py", "content": "y", "overwrite": True}, ctx
    )
    assert result.is_error is True
    assert "symlink" in result.content[0].text
    assert real.read_text(encoding="utf-8") == "x"


async def test_write_tool_refuses_symlinked_tools_dir(workspace: Path):
    real_dir = workspace / "real_tools"
    real_dir.mkdir()
    (workspace / ".agents").mkdir()
    (workspace / ".agents" / "tools").symlink_to(real_dir)
    ctx = ctx_for(workspace)
    result = await meta.write_tool({"filename": "foo.py", "content": "x"}, ctx)
    assert result.is_error is True
    assert not (real_dir / "foo.py").exists()


async def test_write_tool_permission_key_is_canonical_absolute(workspace: Path):
    config = make_config(profile="coding_meta")
    from nexus.tools.builtin import META_TOOLS_OPT_IN, OPT_IN_TOOLS

    manager = ToolManager(
        config,
        workspace=workspace,
        tools=(*ToolManager._builtin_catalog(), *OPT_IN_TOOLS, *META_TOOLS_OPT_IN),
    )
    entry = manager.prepare(
        [
            ToolCall(
                id="w1",
                name="WriteTool",
                input={"filename": "x.py", "content": "c"},
            )
        ]
    ).entries[0]
    assert entry.error is None
    # The declared key is workspace-relative; the manager canonicalizes it to an
    # absolute path so rules and grants match the same form ``Write`` uses.
    assert WRITE_TOOL_SPEC.resolve_permission_key({"filename": "x.py"}) == (
        ".agents/tools/x.py"
    )
    assert entry.key == str(workspace / ".agents" / "tools" / "x.py")
    assert os.path.isabs(entry.key)


async def test_write_tool_key_boundary_is_enforced_by_the_manager(workspace: Path):
    from nexus.tools.builtin import META_TOOLS_OPT_IN, OPT_IN_TOOLS

    restricted = ToolManager(
        make_config(profile="coding_meta", write_roots=["./src"]),
        workspace=workspace,
        tools=(*ToolManager._builtin_catalog(), *OPT_IN_TOOLS, *META_TOOLS_OPT_IN),
    )
    entry = restricted.prepare(
        [
            ToolCall(
                id="w1",
                name="WriteTool",
                input={"filename": "x.py", "content": "c"},
            )
        ]
    ).entries[0]
    assert entry.error is not None
    assert entry.code == "write_root"


async def test_write_tool_is_subject_to_fs_write_roots(workspace: Path):
    ctx = ctx_for(workspace, config=make_config(write_roots=["./src"]))
    result = await meta.write_tool(
        {"filename": "foo.py", "content": "x"}, ctx
    )
    assert result.is_error is True
    assert "write roots" in result.content[0].text
    assert not (workspace / ".agents" / "tools" / "foo.py").exists()


async def test_write_tool_validates_arguments(workspace: Path):
    ctx = ctx_for(workspace)
    missing = await meta.write_tool({"content": "x"}, ctx)
    assert missing.is_error is True
    not_object = await meta.write_tool([], ctx)  # type: ignore[arg-type]
    assert not_object.is_error is True
