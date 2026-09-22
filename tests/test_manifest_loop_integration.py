"""P4-G: the loop pins one manifest generation per iteration.

The core loop stays protocol-only; the runtime builds a per-iteration
environment from the pinned generation. These tests prove the contract from the
outside:

* a hot-loaded tool becomes visible **and callable in the same turn** after a
  reload (the section 6.5 walkthrough);
* exactly one generation is pinned during an iteration and released after;
* a slow call pinned to an old generation survives a reload, and its module is
  not dropped until the pin is released;
* an invalid reload keeps the previous generation intact;
* a reload that commits during iteration N is visible in N+1 (config, SOUL.md,
  MEMORY.md, skills, and tools), while N stays stable;
* a reload cannot weaken the turn-frozen security baseline;
* concurrent sessions pin different generations independently;
* ``nexus.core.loop`` imports no extension layer (AST).

Everything runs offline; providers are scripts and extension modules are real
hot-loaded files.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(*, profile="coding", mode="allow", write_roots=None) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile=profile),
            permissions=PermissionsSection(
                mode=mode,
                on_unattended="allow",
                write_roots=list(write_roots) if write_roots else ["./"],
            ),
            tools=ToolsSection(),
        ),
    )


def make_runtime(tmp_path, provider, config=None) -> Runtime:
    return Runtime(
        tmp_path,
        config=config or make_config(),
        providers={"scripted": provider},
    )


async def drain(session):
    return [event async for event in session.send("go")]


async def wait_for(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


async def wait_until_idle(session, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.active or session.queue_depth:
        if loop.time() > deadline:
            raise AssertionError("session did not settle within the timeout")
        await asyncio.sleep(0.001)


def ext_source(name: str, *, body: str = "pong") -> str:
    return f'''
from nexus.tools.spec import ToolExecutionResult, ToolSpec

SPEC = ToolSpec(
    name="{name}",
    description="test tool {name}",
    input_schema={{"type": "object", "properties": {{}}, "additionalProperties": False}},
    bundle="fs",
)

async def run(args, ctx):
    return ToolExecutionResult.text("{body}")
'''


def write_tool(tmp_path, name="ExtPing", *, body="pong", source=None):
    tools = tmp_path / ".nexus" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / f"{name}.py").write_text(source or ext_source(name, body=body), encoding="utf-8")


def write_skill(tmp_path, name="reader", description="read things", allowed="[Read]"):
    skill = tmp_path / ".nexus" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"allowed-tools: {allowed}\n---\nBODY\n",
        encoding="utf-8",
    )


def tool_result_for(session, call_id):
    for message in session.messages:
        if message.role != "user":
            continue
        for block in message.content:
            if getattr(block, "tool_use_id", None) == call_id:
                return block
    return None


# ---------------------------------------------------------------------------
# The section 6.5 walkthrough: same turn, new tool
# ---------------------------------------------------------------------------


async def test_reload_then_call_new_tool_in_the_same_turn(tmp_path):
    def write_then_reload(request):
        write_tool(tmp_path, "ExtPing", body="pong")
        return tool_response(("r1", "ReloadExtensions", {}))

    provider = ScriptedProvider(
        [write_then_reload],
        tool_response(("c1", "ExtPing", {})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("walkthrough")

    events = await drain(session)

    assert events[-1].type == "turn.completed"
    # The new tool was advertised on the iteration after the reload...
    assert "ExtPing" in {t.name for t in provider.requests[1].tools}
    assert "ExtPing" not in {t.name for t in provider.requests[0].tools}
    # ...and executed.
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is False
    assert result.content[0].text == "pong"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# One generation pinned per iteration
# ---------------------------------------------------------------------------


async def test_exactly_one_generation_is_pinned_per_iteration(tmp_path):
    observed = {}

    def respond(request):
        observed["generation"] = runtime.manifest_ref.generation
        observed["pins"] = dict(runtime.manifest_ref.pinned_generations)
        return text_response("ok")

    provider = ScriptedProvider([respond])
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("pinned")

    await drain(session)

    assert observed["generation"] in observed["pins"]
    assert observed["pins"][observed["generation"]] >= 1
    # Every pin is released after the turn.
    assert runtime.manifest_ref.pinned_generations == {}
    await runtime.aclose()


# ---------------------------------------------------------------------------
# A pinned old tool call survives a reload
# ---------------------------------------------------------------------------


SLOW_TOOL = '''
import asyncio
from nexus.tools.spec import ToolExecutionResult, ToolSpec

SPEC = ToolSpec(
    name="Slow",
    description="slow",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    bundle="fs",
)

async def run(args, ctx):
    (ctx.workspace / "started.txt").write_text("x", encoding="utf-8")
    release = globals().get("RELEASE")
    if release is not None:
        await release.wait()
    return ToolExecutionResult.text("old-value")
'''


async def test_pinned_old_tool_slow_call_survives_a_reload(tmp_path):
    write_tool(tmp_path, "Slow", source=SLOW_TOOL)
    provider = ScriptedProvider(
        tool_response(("c1", "Slow", {})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)

    # Install the release latch on the loaded module before the call starts.
    await runtime.ensure_started()
    module_name = next(iter(runtime.manifest.modules))
    module = sys.modules[module_name]
    release = asyncio.Event()
    module.RELEASE = release
    pinned_generation = runtime.manifest.generation

    session = runtime.session("slow")
    task = asyncio.create_task(session.start_turn("go"))
    await task
    await wait_for(lambda: (tmp_path / "started.txt").exists())

    # The in-flight call holds a pin on its generation.
    assert runtime.manifest_ref.pinned_generations.get(pinned_generation, 0) >= 1

    # A reload that swaps in a changed tool must not drop the running module.
    write_tool(tmp_path, "Slow", source=SLOW_TOOL.replace("old-value", "new-value"))
    await runtime.extensions.reload(trigger="test")
    assert runtime.manifest.generation > pinned_generation
    assert module_name in sys.modules

    release.set()
    await wait_until_idle(session)

    result = tool_result_for(session, "c1")
    assert result is not None
    assert result.content[0].text == "old-value"  # the pinned implementation
    # Retired once the pin is gone.
    assert module_name not in sys.modules
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Invalid reload keeps the old generation
# ---------------------------------------------------------------------------


async def test_invalid_reload_keeps_the_previous_generation(tmp_path):
    write_tool(tmp_path, "Good", body="good")
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    await runtime.ensure_started()
    generation = runtime.manifest.generation
    assert "Good" in runtime.manifest.tools

    # A broken file makes the rebuild all-or-nothing.
    write_tool(tmp_path, "Broken", source="def broken(:\n")
    report = await runtime.extensions.reload(trigger="test")

    assert report.failed
    assert runtime.manifest.generation == generation
    assert "Good" in runtime.manifest.tools
    assert "Broken" not in runtime.manifest.tools
    await runtime.aclose()


# ---------------------------------------------------------------------------
# A reload during iteration N is visible in N+1
# ---------------------------------------------------------------------------


async def test_reload_refreshes_config_soul_memory_skills_next_iteration(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL-A", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("MEMORY-A", encoding="utf-8")
    holder = {"config": make_config(profile="coding")}

    def mutate(request):
        holder["config"] = make_config(profile="research")
        (tmp_path / "SOUL.md").write_text("SOUL-B", encoding="utf-8")
        (tmp_path / "MEMORY.md").write_text("MEMORY-B", encoding="utf-8")
        write_skill(tmp_path, "reader", "read things")
        return tool_response(("r1", "ReloadExtensions", {}))

    provider = ScriptedProvider([mutate], text_response("done"))
    runtime = Runtime(
        tmp_path,
        config_loader=lambda: holder["config"],
        providers={"scripted": provider},
    )
    session = runtime.session("refresh")

    await drain(session)

    first, second = provider.requests[0], provider.requests[1]
    # Iteration 1: the turn-start snapshot.
    assert "SOUL-A" in first.system and "MEMORY-A" in first.system
    assert "reader:" not in first.system
    assert len(first.tools) == 15
    # Iteration 2: the reloaded manifest.
    assert "SOUL-B" in second.system and "MEMORY-B" in second.system
    assert "reader: read things" in second.system
    assert len(second.tools) == 4  # research profile
    await runtime.aclose()


# ---------------------------------------------------------------------------
# A reload cannot weaken the turn-frozen security baseline
# ---------------------------------------------------------------------------


async def test_reload_cannot_widen_write_roots_mid_turn(tmp_path):
    (tmp_path / "sub").mkdir()
    holder = {"config": make_config(write_roots=["sub"])}

    def mutate(request):
        # Widen the roots on disk and in the loader; the running turn must keep
        # the roots it started with.
        holder["config"] = make_config(write_roots=["./"])
        return tool_response(("r1", "ReloadExtensions", {}))

    provider = ScriptedProvider(
        [mutate],
        tool_response(("c1", "Write", {"path": "outside.txt", "content": "x"})),
        text_response("done"),
    )
    runtime = Runtime(
        tmp_path,
        config_loader=lambda: holder["config"],
        providers={"scripted": provider},
    )
    session = runtime.session("secure")
    await drain(session)

    assert not (tmp_path / "outside.txt").exists()
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is True
    await runtime.aclose()


async def test_reload_cannot_switch_a_denied_mode_to_allow_mid_turn(tmp_path):
    holder = {"config": make_config(mode="deny")}

    def mutate(request):
        holder["config"] = make_config(mode="allow")
        return tool_response(("r1", "ReloadExtensions", {}))

    provider = ScriptedProvider(
        [mutate],
        tool_response(("c1", "Write", {"path": "x.txt", "content": "x"})),
        text_response("done"),
    )
    runtime = Runtime(
        tmp_path,
        config_loader=lambda: holder["config"],
        providers={"scripted": provider},
    )
    session = runtime.session("deny")
    await drain(session)

    assert not (tmp_path / "x.txt").exists()
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is True
    await runtime.aclose()


# ---------------------------------------------------------------------------
# The permission gate stays bound across a reload
# ---------------------------------------------------------------------------


async def test_pending_permission_survives_a_reload(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "out.txt", "content": "x"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=make_config(mode="ask"))
    session = runtime.session("approval").mark_attended(True)

    await session.start_turn("go")
    await wait_for(lambda: session.pending_permissions)
    request_id = session.pending_permissions[0]

    # A reload while the approval is parked must not detach the broker.
    write_tool(tmp_path, "ExtPing", body="pong")
    await runtime.extensions.reload(trigger="test")

    assert session.resolve_permission(request_id, "allow_once") is True
    await wait_until_idle(session)

    assert session.events[-1].type == "turn.completed"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Provider unavailable fails visibly
# ---------------------------------------------------------------------------


async def test_provider_unavailable_after_reload_fails_the_turn(tmp_path):
    holder = {"config": make_config()}

    def break_provider(request):
        holder["config"] = Config(
            model="missing/model",
            version=2,
            v2=ConfigV2(
                model=ModelSection(default="missing/model"),
                agent=AgentSection(profile="coding"),
                permissions=PermissionsSection(mode="allow", on_unattended="allow"),
                tools=ToolsSection(),
            ),
        )
        return tool_response(("r1", "ReloadExtensions", {}))

    provider = ScriptedProvider([break_provider], text_response("never"))
    runtime = Runtime(
        tmp_path,
        config_loader=lambda: holder["config"],
        providers={"scripted": provider},
    )
    session = runtime.session("unavailable")
    events = await drain(session)

    assert events[-1].type == "turn.failed"
    assert "missing" in events[-1].data["error"]
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Concurrent sessions pin different generations
# ---------------------------------------------------------------------------


async def test_concurrent_sessions_pin_different_generations(tmp_path):
    write_tool(tmp_path, "First", body="first")
    release = asyncio.Event()
    recorded = {}

    async def a_respond(request):
        recorded["a_gen"] = runtime.manifest_ref.generation
        recorded["a_pins"] = dict(runtime.manifest_ref.pinned_generations)
        await release.wait()
        return text_response("a-done")

    def b_respond(request):
        recorded["b_gen"] = runtime.manifest_ref.generation
        return text_response("b-done")

    provider = ScriptedProvider([a_respond], [b_respond])
    runtime = make_runtime(tmp_path, provider)
    a = runtime.session("a")
    b = runtime.session("b")

    await a.start_turn("go")
    await wait_for(lambda: "a_gen" in recorded)
    # A reload between A's iterations must not disturb A's pin, and must give
    # B's next iteration a newer generation.
    write_tool(tmp_path, "Second", body="second")
    await runtime.extensions.reload(trigger="test")

    await b.start_turn("go")
    await wait_until_idle(b)
    assert recorded["b_gen"] != recorded["a_gen"]
    assert recorded["a_gen"] in recorded["a_pins"]

    release.set()
    await wait_until_idle(a)
    assert runtime.manifest_ref.pinned_generations == {}
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Core stays protocol-only
# ---------------------------------------------------------------------------


def test_core_loop_imports_no_extension_or_manager_layer():
    tree = ast.parse(
        (REPO_ROOT / "nexus" / "core" / "loop.py").read_text(encoding="utf-8")
    )
    forbidden = (
        "nexus.ext",
        "nexus.runtime",
        "nexus.session",
        "nexus.context",
        "nexus.tools",
        "nexus.skills",
    )
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    violations = [
        module for module in modules if module.startswith(forbidden)
    ]
    assert not violations, f"core/loop.py imports {violations}"
