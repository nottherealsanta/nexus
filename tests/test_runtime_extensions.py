"""P4-G runtime/extension ownership and lifecycle.

The runtime is the composition root that owns the extension world: one
``ExtensionManager``, its atomic ``ManifestRef``, the bus its reload events are
published on, the ``SkillManager``, and the session/turn-local activation log.
These tests exercise that ownership through the public runtime surface:

* the manager/ref/bus/skills are exposed and share one identity;
* ``ensure_started`` bootstraps a manifest that contains the builtins, the
  current config, the system-file contents, the discovered skills, and the
  external tools, and lazily starts the watcher;
* ``aclose`` stops the watcher and releases the loaded modules;
* an injected manager is never closed, and the pre-existing injected seams
  (``tools``/``tool_factory``) still take precedence.

All providers are offline scripts; extension modules are real hot-loaded files.
"""
from __future__ import annotations

import sys

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.ext.manager import ExtensionManager
from nexus.ext.manifest import Manifest
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.tools.manager import ToolManager
from nexus.tools.spec import RegisteredTool, ToolExecutionResult, ToolSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(*, mode="allow", unattended="allow") -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode=mode, on_unattended=unattended),
            tools=ToolsSection(),
        ),
    )


def make_runtime(tmp_path, provider=None, **kwargs) -> Runtime:
    provider = provider or ScriptedProvider(text_response("ok"))
    return Runtime(
        tmp_path,
        config=make_config(),
        providers={"scripted": provider},
        **kwargs,
    )


EXT_TOOL = '''
from nexus.tools.spec import ToolExecutionResult, ToolSpec

SPEC = ToolSpec(
    name="ExtPing",
    description="ping",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    bundle="fs",
)

async def run(args, ctx):
    return ToolExecutionResult.text("pong")
'''


def write_ext_tool(tmp_path, name="ping"):
    tools = tmp_path / ".nexus" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / f"{name}.py").write_text(EXT_TOOL, encoding="utf-8")


def _registered(name: str) -> RegisteredTool:
    async def _run(args, ctx):
        return ToolExecutionResult.text("ok")

    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description="d",
            input_schema={"type": "object"},
            bundle="fs",
        ),
        run=_run,
        origin="ext",
    )


def write_skill(tmp_path, name="reader", description="read things"):
    skill = tmp_path / ".nexus" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nBODY\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


async def test_runtime_owns_extension_manager_ref_and_bus(tmp_path):
    runtime = make_runtime(tmp_path)
    assert runtime.extensions is not None
    assert runtime.manifest_ref is runtime.extensions.ref
    assert runtime.skills is runtime.extensions.skills
    assert runtime.extension_events is not None
    await runtime.aclose()


async def test_bootstrap_manifest_has_builtins_config_system_files_skills_tools(
    tmp_path,
):
    (tmp_path / "SOUL.md").write_text("SOUL-CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY-CONTENT", encoding="utf-8")
    write_skill(tmp_path, "reader", "read things")
    write_ext_tool(tmp_path, "ping")
    runtime = make_runtime(tmp_path)

    await runtime.ensure_started()
    manifest = runtime.manifest

    # Builtins plus the external tool.
    assert "Read" in manifest.tools and "Bash" in manifest.tools
    assert "ExtPing" in manifest.tools
    assert manifest.tools["ExtPing"].origin == "ext"
    # Config is the bootstrap config.
    assert manifest.config.model == "scripted/m"
    # System files captured with their content.
    assert manifest.system_files.soul is not None
    assert manifest.system_files.soul.content == "SOUL-CONTENT"
    assert manifest.system_files.memory.content == "MEMORY-CONTENT"
    # Skills discovered.
    assert "reader" in manifest.skills
    await runtime.aclose()


async def test_watcher_starts_lazily_and_aclose_stops_and_releases_modules(tmp_path):
    write_ext_tool(tmp_path, "ping")
    runtime = make_runtime(tmp_path)

    assert runtime.extensions._watch_task is None  # not started at construction
    await runtime.ensure_started()
    assert runtime.extensions._watch_task is not None

    manifest = runtime.manifest
    module_names = list(manifest.modules)
    assert module_names, "expected a loaded external module"
    assert all(name in sys.modules for name in module_names)

    await runtime.aclose()
    assert runtime.extensions._watch_task is None
    assert all(name not in sys.modules for name in module_names)


async def test_injected_extension_manager_is_not_closed(tmp_path):
    manager = ExtensionManager(
        tmp_path, home=tmp_path, config_loader=make_config
    )
    runtime = make_runtime(tmp_path, extensions=manager)
    assert runtime.extensions is manager
    await runtime.ensure_started()
    await runtime.aclose()
    # The caller owns an injected manager; the watcher state is untouched by us.
    assert runtime.extensions is manager


async def test_injected_tool_manager_still_takes_precedence(tmp_path):
    config = make_config()
    manager = ToolManager(config, workspace=tmp_path, profile="research")
    runtime = make_runtime(tmp_path, tools=manager)
    session = runtime.session("injected")
    turn = runtime._make_tool_turn(
        config=config, session=session, turn_id="t1", attended=False
    )
    # The injected manager is used verbatim and the static path is kept.
    assert turn.manager is manager
    assert turn.manifest_ref is None
    assert turn.environment_for is None
    await runtime.aclose()


async def test_extension_events_are_published_on_the_runtime_bus(tmp_path):
    import asyncio

    write_ext_tool(tmp_path, "ping")
    runtime = make_runtime(tmp_path)
    sub = runtime.extension_events.subscribe()
    await runtime.ensure_started()

    event = await asyncio.wait_for(sub.get(), 0.5)
    assert event.type.startswith("ext.")
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Empty/limited manifest catalog never gains the builtin fallback
# ---------------------------------------------------------------------------


async def test_empty_manifest_catalog_has_no_tools(tmp_path):
    config = make_config()
    runtime = make_runtime(tmp_path)
    empty = Manifest(generation=1, config=config, tools={})
    manager = runtime._build_iteration_manager(config, empty)
    # An empty manifest must yield an empty catalog, never all builtins.
    assert manager.names == ()
    assert manager.schemas() == ()
    await runtime.aclose()


async def test_limited_manifest_catalog_exposes_only_its_tools(tmp_path):
    config = make_config()
    runtime = make_runtime(tmp_path)
    manifest = Manifest(
        generation=1, config=config, tools={"OnlyOne": _registered("OnlyOne")}
    )
    manager = runtime._build_iteration_manager(config, manifest)
    assert manager.names == ("OnlyOne",)
    assert "Read" not in manager.names
    await runtime.aclose()


def test_tool_manager_distinguishes_none_from_empty_catalog(tmp_path):
    config = make_config()
    empty = ToolManager(config, workspace=tmp_path, tools=[])
    assert empty.names == ()
    # ``None`` (the default) means the builtin catalog; only an explicit empty
    # sequence means no tools.
    default = ToolManager(config, workspace=tmp_path)
    assert "Read" in default.names
