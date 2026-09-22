"""P4-H end-to-end: the self-extension money path and skill-scoped bundled tools.

These tests drive the whole hot-extension flow through a real ``Runtime`` and an
offline scripted provider, covering the plan section 6.5 walkthrough and the
P4-H additions:

* the model reads ``.nexus/tools/_template.py``, ``WriteTool`` authors a valid
  extension, ``ReloadExtensions`` swaps it in, the *next* iteration advertises
  it, the permission gate asks first, and the tool runs -- all in one turn;
* a broken extension reload is a model-visible error, the previous manifest
  stays live, and the model can fix and reload in the same turn;
* a skill's bundled ``tools/*.py`` are loaded during the rebuild but never
  globally registered: they are exposed only on the iteration after the skill
  is invoked, only for the invoking session/turn, and released with the
  generation;
* deletion falls back to the user tier (tools and skills);
* the advertised schema and the executed implementation always come from the
  same pinned generation;
* a slow call pinned to generation N survives a reload into N+1;
* API/tool/watcher reloads are serialized;
* config, ``SOUL.md``/``MEMORY.md``, and skills are hot for the next iteration.

Everything is offline: providers are scripts and extensions are real files.
"""
from __future__ import annotations

import asyncio
import sys
import time

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.ext.template import (
    TOOL_TEMPLATE_FILENAME,
    ensure_tool_template,
    read_tool_template,
)
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    *,
    profile: str = "coding",
    mode: str = "allow",
    write_roots: list[str] | None = None,
) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile=profile),
            permissions=PermissionsSection(
                mode=mode,
                on_unattended="allow",
                write_roots=write_roots if write_roots is not None else ["./"],
            ),
            tools=ToolsSection(),
        ),
    )


def make_runtime(tmp_path, provider, *, config=None, home=None) -> Runtime:
    kwargs = {}
    if home is not None:
        kwargs["home"] = home
    return Runtime(
        tmp_path,
        config=config or make_config(),
        providers={"scripted": provider},
        **kwargs,
    )


def write_tool(tmp_path, name, *, source, subdir=".nexus/tools"):
    tools = tmp_path / subdir
    tools.mkdir(parents=True, exist_ok=True)
    (tools / f"{name}.py").write_text(source, encoding="utf-8")


def write_skill(
    root,
    name,
    *,
    description="a skill",
    allowed=None,
    bundles=None,
    tools=None,
    subdir=".nexus/skills",
):
    directory = root / subdir / name
    directory.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name}", f"description: {description}"]
    if allowed is not None:
        lines.append(f"allowed-tools: {allowed}")
    if bundles is not None:
        lines.append(f"bundles: {bundles}")
    (directory / "SKILL.md").write_text(
        "---\n" + "\n".join(lines) + "\n---\nBODY\n", encoding="utf-8"
    )
    for filename, content in (tools or {}).items():
        target = directory / "tools" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


def tool_source(name, *, body="ok", bundle="ext", description=None):
    return (
        "from typing import Any\n"
        "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
        "SPEC = ToolSpec(\n"
        f"    name={name!r},\n"
        f"    description={description or name!r},\n"
        "    input_schema={'type': 'object', 'properties': {}, "
        "'additionalProperties': False},\n"
        f"    bundle={bundle!r},\n"
        ")\n\n"
        "async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:\n"
        f"    return ToolExecutionResult.text({body!r})\n"
    )


METRICS_SOURCE = (
    "from typing import Any\n"
    "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
    "SPEC = ToolSpec(\n"
    "    name='MetricsQuery',\n"
    "    description='Query the metrics API.',\n"
    "    input_schema={'type': 'object', 'properties': "
    "{'query': {'type': 'string'}}, 'required': ['query'], "
    "'additionalProperties': False},\n"
    "    bundle='ext',\n"
    "    mutates=False,\n"
    ")\n\n"
    "async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:\n"
    "    return ToolExecutionResult.text(f\"p99 is 412ms for {args.get('query')}\")\n"
)


def names(request):
    return [tool.name for tool in request.tools]


def tool_result_for(session, call_id):
    for message in session.messages:
        if message.role != "user":
            continue
        for block in message.content:
            if getattr(block, "tool_use_id", None) == call_id:
                return block
    return None


async def drain(session, *, deny=()):
    """Run a turn, resolving every permission request (deny the named tools)."""
    events = []
    async for event in session.send("go"):
        events.append(event)
        if event.type == "permission.requested":
            request_id = event.data.get("id")
            tool = event.data.get("tool")
            if request_id:
                decision = "deny_once" if tool in deny else "allow_once"
                session.resolve_permission(request_id, decision)
    return events


# ---------------------------------------------------------------------------
# The section 6.5 walkthrough, with the permission ask
# ---------------------------------------------------------------------------


async def test_walkthrough_read_template_write_reload_call(tmp_path):
    provider = ScriptedProvider(
        tool_response(
            ("r1", "Read", {"path": f".nexus/tools/{TOOL_TEMPLATE_FILENAME}"})
        ),
        tool_response(
            ("w1", "WriteTool", {"filename": "metrics_query.py", "content": METRICS_SOURCE})
        ),
        tool_response(("l1", "ReloadExtensions", {})),
        tool_response(("c1", "MetricsQuery", {"query": "p99"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=make_config(mode="ask"))
    session = runtime.session("walkthrough")
    session.mark_attended(True)

    events = await drain(session)

    assert events[-1].type == "turn.completed"
    # The model read the real template (seeded by the harness).
    read_result = tool_result_for(session, "r1")
    assert read_result is not None and read_result.is_error is False
    assert "SPEC" in read_result.content[0].text
    assert "async def run" in read_result.content[0].text
    # The new tool was not advertised before the reload...
    assert "MetricsQuery" not in names(provider.requests[0])
    assert "MetricsQuery" not in names(provider.requests[2])
    # ...and was advertised and callable on the next iteration.
    assert "MetricsQuery" in names(provider.requests[3])
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is False
    assert result.content[0].text == "p99 is 412ms for p99"
    # The permission ask for the new tool was raised and honored.
    asked = [
        event.data.get("tool")
        for event in events
        if event.type == "permission.requested"
    ]
    assert "MetricsQuery" in asked
    await runtime.aclose()


async def test_permission_denial_stops_the_new_tool(tmp_path):
    provider = ScriptedProvider(
        tool_response(
            ("w1", "WriteTool", {"filename": "metrics_query.py", "content": METRICS_SOURCE})
        ),
        tool_response(("l1", "ReloadExtensions", {})),
        tool_response(("c1", "MetricsQuery", {"query": "p99"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=make_config(mode="ask"))
    session = runtime.session("denied")
    session.mark_attended(True)

    events = await drain(session, deny={"MetricsQuery"})

    assert events[-1].type == "turn.completed"
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is True
    assert "denied" in result.content[0].text
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Broken reload: model-visible error, previous manifest stays, then fix
# ---------------------------------------------------------------------------


async def test_broken_syntax_reload_errors_then_recovers(tmp_path):
    observed = {}

    def record_before_fix(request):
        # Runs while the model is producing the post-failure iteration: the
        # broken reload has already failed and the fix has not been reloaded.
        observed["generation"] = runtime.manifest.generation
        observed["has_fixed"] = "Fixed" in runtime.manifest.tools
        return tool_response(("l2", "ReloadExtensions", {}))

    provider = ScriptedProvider(
        tool_response(
            (
                "w1",
                "WriteTool",
                {"filename": "broken.py", "content": "SPEC = {'name': 'Broken'\n"},
            )
        ),
        tool_response(("l1", "ReloadExtensions", {})),
        tool_response(
            (
                "w2",
                "WriteTool",
                {
                    "filename": "broken.py",
                    "content": tool_source("Fixed", body="fixed-value"),
                    "overwrite": True,
                },
            )
        ),
        [record_before_fix],
        tool_response(("c1", "Fixed", {})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("recover")

    events = await drain(session)

    assert events[-1].type == "turn.completed"
    # The broken reload is a model-visible error and loaded nothing.
    first_reload = tool_result_for(session, "l1")
    assert first_reload is not None and first_reload.is_error is True
    assert "syntax" in first_reload.content[0].text.lower()
    # The previous manifest stayed live across the failed reload.
    assert observed["generation"] == 0
    assert observed["has_fixed"] is False
    # The fix reloads and the tool becomes callable in the same turn.
    second_reload = tool_result_for(session, "l2")
    assert second_reload is not None and second_reload.is_error is False
    assert "Fixed" in runtime.manifest.tools
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is False
    assert result.content[0].text == "fixed-value"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Skill-scoped bundled tools
# ---------------------------------------------------------------------------


SKILL_PING = tool_source("SkillPing", body="skill-pong")


async def test_bundled_skill_tool_exposed_next_iteration_only(tmp_path):
    write_skill(
        tmp_path,
        "reader",
        tools={"skill_ping.py": SKILL_PING},
    )
    provider = ScriptedProvider(
        tool_response(("s1", "Skill", {"name": "reader"})),
        tool_response(("c1", "SkillPing", {})),
        text_response("done"),
        text_response("second turn"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("skill-bundled")

    await drain(session)

    assert "SkillPing" not in runtime.manifest.tools
    assert "SkillPing" not in names(provider.requests[0])
    assert "SkillPing" in names(provider.requests[1])
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is False
    assert result.content[0].text == "skill-pong"
    # Cleared for the next turn.
    await drain(session)
    assert "SkillPing" not in names(provider.requests[-1])
    await runtime.aclose()


async def test_bundled_skill_tool_does_not_leak_across_sessions(tmp_path):
    write_skill(tmp_path, "reader", tools={"skill_ping.py": SKILL_PING})
    release = asyncio.Event()
    recorded = {}

    def a_skill(request):
        return tool_response(("s1", "Skill", {"name": "reader"}))

    async def a_park(request):
        recorded["a_tools"] = names(request)
        await release.wait()
        return text_response("a-done")

    def b_respond(request):
        recorded["b_tools"] = names(request)
        return text_response("b-done")

    provider = ScriptedProvider([a_skill], [a_park], [b_respond])
    runtime = make_runtime(tmp_path, provider)
    a = runtime.session("session-a")
    b = runtime.session("session-b")

    await a.start_turn("go")
    while "a_tools" not in recorded:
        await asyncio.sleep(0)
    assert "SkillPing" in recorded["a_tools"]

    await b.start_turn("go")
    await _wait_idle(b)
    assert "SkillPing" not in recorded["b_tools"]

    release.set()
    await _wait_idle(a)
    await runtime.aclose()


async def test_bundled_skill_tool_declaration_narrows_it_away(tmp_path):
    write_skill(
        tmp_path,
        "reader",
        allowed="[Read]",
        tools={"skill_ping.py": SKILL_PING},
    )
    provider = ScriptedProvider(
        tool_response(("s1", "Skill", {"name": "reader"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("narrowed")

    await drain(session)

    assert names(provider.requests[1]) == ["Read"]
    await runtime.aclose()


async def test_noop_reload_with_bundled_skill_tools_does_not_churn(tmp_path):
    write_skill(tmp_path, "reader", tools={"skill_ping.py": SKILL_PING})
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    first = await runtime.extensions.reload(trigger="test")
    second = await runtime.extensions.reload(trigger="test")

    assert first.changed is True
    assert second.changed is False
    assert second.generation == first.generation
    assert runtime.manifest.skill_tools["reader"].tool_names == ("SkillPing",)
    await runtime.aclose()


async def test_invalid_bundled_skill_tool_aborts_the_rebuild(tmp_path):
    write_skill(
        tmp_path,
        "reader",
        tools={"broken.py": "SPEC = {'name': 'Broken'\n"},
    )
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    report = await runtime.extensions.reload(trigger="test")

    assert report.failed
    assert report.failed[0].kind == "skill"
    assert report.failed[0].name == "reader"
    assert runtime.manifest.generation == 0
    assert "reader" not in runtime.manifest.skills
    await runtime.aclose()


async def test_bundled_skill_tool_builtin_collision_fails(tmp_path):
    write_skill(
        tmp_path,
        "reader",
        tools={"shadow.py": tool_source("Read", body="shadow")},
    )
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    report = await runtime.extensions.reload(trigger="test")

    assert report.failed
    assert report.failed[0].error_type == "collision"
    assert "Read" in runtime.manifest.tools
    assert runtime.manifest.tools["Read"].spec.description != "shadow"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Deletion / user-workspace fallback
# ---------------------------------------------------------------------------


async def test_deleting_workspace_skill_falls_back_to_user_tools(tmp_path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    write_skill(
        workspace, "reader", description="workspace", tools={"ws_ping.py": SKILL_PING}
    )
    write_skill(
        home,
        "reader",
        description="user",
        tools={"user_ping.py": tool_source("UserPing", body="user-pong")},
    )
    runtime = Runtime(
        workspace,
        home=home,
        config=make_config(),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    await runtime.ensure_started()
    assert runtime.manifest.skill_tools["reader"].tool_names == ("SkillPing",)

    # Deleting the workspace skill reveals the user skill and its bundled tool.
    import shutil

    shutil.rmtree(workspace / ".nexus" / "skills" / "reader")
    await runtime.extensions.reload(trigger="test")
    assert runtime.manifest.skills["reader"].description == "user"
    assert runtime.manifest.skill_tools["reader"].tool_names == ("UserPing",)
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Schema and implementation share one generation
# ---------------------------------------------------------------------------


async def test_schema_and_implementation_share_one_generation(tmp_path):
    write_tool(tmp_path, "Ping", source=tool_source("Ping", body="v1", description="v1"))

    def mutate(request):
        write_tool(
            tmp_path,
            "Ping",
            source=tool_source("Ping", body="v2", description="v2"),
        )
        return tool_response(("r1", "ReloadExtensions", {}))

    provider = ScriptedProvider(
        [mutate],
        tool_response(("c1", "Ping", {})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("same-gen")

    await drain(session)

    first = {tool.name: tool for tool in provider.requests[0].tools}["Ping"]
    second = {tool.name: tool for tool in provider.requests[1].tools}["Ping"]
    assert first.description == "v1"
    assert second.description == "v2"
    result = tool_result_for(session, "c1")
    assert result is not None and result.content[0].text == "v2"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# A slow call pinned to generation N survives a reload into N+1
# ---------------------------------------------------------------------------


SLOW_SKILL_TOOL = (
    "import asyncio\n"
    "from typing import Any\n"
    "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
    "SPEC = ToolSpec(name='SlowSkill', description='slow', "
    "input_schema={'type': 'object', 'properties': {}, "
    "'additionalProperties': False}, bundle='ext')\n"
    "async def run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:\n"
    "    (ctx.workspace / 'skill_started.txt').write_text('x', encoding='utf-8')\n"
    "    release = globals().get('RELEASE')\n"
    "    if release is not None:\n"
    "        await release.wait()\n"
    "    return ToolExecutionResult.text('old-value')\n"
)


async def test_pinned_slow_skill_tool_survives_a_reload(tmp_path):
    write_skill(tmp_path, "reader", tools={"slow_skill.py": SLOW_SKILL_TOOL})
    provider = ScriptedProvider(
        tool_response(("s1", "Skill", {"name": "reader"})),
        tool_response(("c1", "SlowSkill", {})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    await runtime.ensure_started()

    skill_module = runtime.manifest.skill_tools["reader"].modules[0]
    module = sys.modules[skill_module]
    release = asyncio.Event()
    module.RELEASE = release

    session = runtime.session("slow-skill")
    task = asyncio.create_task(session.start_turn("go"))
    await task
    while not (tmp_path / "skill_started.txt").exists():
        await asyncio.sleep(0)

    pinned = runtime.manifest.generation
    assert runtime.manifest_ref.pinned_generations.get(pinned, 0) >= 1

    # Change the bundled tool and reload while the call is parked.
    write_skill(
        tmp_path,
        "reader",
        tools={
            "slow_skill.py": SLOW_SKILL_TOOL.replace("old-value", "new-value")
        },
    )
    await runtime.extensions.reload(trigger="test")
    assert runtime.manifest.generation > pinned
    assert skill_module in sys.modules

    release.set()
    await _wait_idle(session)
    result = tool_result_for(session, "c1")
    assert result is not None and result.content[0].text == "old-value"
    assert skill_module not in sys.modules
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Serialized API/tool/watcher reloads
# ---------------------------------------------------------------------------


async def test_api_tool_watcher_reloads_are_serialized(tmp_path):
    from nexus.config.schema import ExtSection
    from nexus.ext import ExtensionManager
    from nexus.tools.loader import ToolLoader

    config = Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="allow", on_unattended="allow"),
            tools=ToolsSection(),
            ext=ExtSection(enabled=True, watch_interval_ms=20),
        ),
    )
    manager = ExtensionManager(
        tmp_path,
        home=tmp_path,
        config_loader=lambda: config,
        loader=ToolLoader(),
    )
    write_tool(tmp_path, "alpha", source=tool_source("Alpha"))

    active = 0
    max_active = 0
    original = manager._build

    def slow_build(previous, generation):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return original(previous, generation)
        finally:
            active -= 1

    manager._build = slow_build
    await manager.reload(trigger="api")
    manager.start()

    write_tool(tmp_path, "beta", source=tool_source("Beta"))
    await asyncio.gather(
        manager.reload(trigger="api"),
        manager.reload(trigger="tool"),
        manager.reload(trigger="watcher"),
    )
    assert max_active == 1
    assert "Beta" in manager.manifest.tools
    await manager.aclose()


# ---------------------------------------------------------------------------
# Config / system files / skills are hot for the next iteration
# ---------------------------------------------------------------------------


async def test_config_system_files_and_skills_are_hot_next_iteration(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL-A", encoding="utf-8")
    holder = {"config": make_config(profile="coding")}

    def mutate(request):
        holder["config"] = make_config(profile="research")
        (tmp_path / "SOUL.md").write_text("SOUL-B", encoding="utf-8")
        write_skill(tmp_path, "reader", description="read things")
        return tool_response(("r1", "ReloadExtensions", {}))

    provider = ScriptedProvider([mutate], text_response("done"))
    runtime = Runtime(
        tmp_path,
        config_loader=lambda: holder["config"],
        providers={"scripted": provider},
    )
    session = runtime.session("hot")

    await drain(session)

    assert "SOUL-A" in provider.requests[0].system
    assert "SOUL-B" in provider.requests[1].system
    assert "reader: read things" in provider.requests[1].system
    assert len(provider.requests[0].tools) == 15
    assert len(provider.requests[1].tools) == 4  # research profile
    await runtime.aclose()


# ---------------------------------------------------------------------------
# The template itself
# ---------------------------------------------------------------------------


async def test_template_is_seeded_ignored_and_never_overwritten(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    await runtime.ensure_started()

    template = tmp_path / ".nexus" / "tools" / TOOL_TEMPLATE_FILENAME
    assert template.is_file()
    assert "SPEC" in template.read_text(encoding="utf-8")
    # Ignored by the loader: never a tool and never a loaded module.
    assert "ExampleTool" not in runtime.manifest.tools
    assert runtime.manifest.modules == {}

    # A user edit is never overwritten by a later rebuild.
    template.write_text("# my custom template\n", encoding="utf-8")
    await runtime.extensions.reload(trigger="test")
    assert template.read_text(encoding="utf-8") == "# my custom template\n"
    await runtime.aclose()


def test_ensure_tool_template_never_overwrites(tmp_path):
    tools = tmp_path / "tools"
    created = ensure_tool_template(tools)
    assert created is not None and created.is_file()
    assert read_tool_template() in created.read_text(encoding="utf-8")
    created.write_text("custom", encoding="utf-8")
    assert ensure_tool_template(tools) is None
    assert created.read_text(encoding="utf-8") == "custom"


def test_packaged_template_is_a_valid_extension(tmp_path):
    from nexus.ext.quarantine import Quarantine
    from nexus.tools.loader import ToolLoader

    path = tmp_path / "example.py"
    path.write_text(read_tool_template(), encoding="utf-8")
    quarantine = Quarantine(
        max_file_bytes=100_000, timeout_s=2.0, root=tmp_path,
        stage_root=tmp_path / "stage",
    )
    staged = quarantine.stage(quarantine.open(path))
    loader = ToolLoader()
    outcome = loader.load(staged, 1)
    assert outcome.ok is True
    assert [tool.name for tool in outcome.tools] == ["ExampleTool"]
    loader.release(loader.owned_modules)


# ---------------------------------------------------------------------------
# Small async helper
# ---------------------------------------------------------------------------


async def _wait_idle(session, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.active or session.queue_depth:
        if loop.time() > deadline:
            raise AssertionError("session did not settle within the timeout")
        await asyncio.sleep(0.001)
