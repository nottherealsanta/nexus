"""Authority stays per turn: attaching is a request, never a grant (kernel plan §3.6, §3.13, §6)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

pytest.importorskip('pydantic_monty')

from test_codemode_nested import Harness  # noqa: E402
from test_codemode_nested import call as nested_call  # noqa: E402
from test_codemode_tool import Rig, ScriptHost, config, ok, text_of, tool  # noqa: E402

from nexus.config import Config  # noqa: E402
from nexus.config.schema import ConfigV2, ModelSection  # noqa: E402
from nexus.tools.codemode.kernels import CodeKernels  # noqa: E402
from nexus.tools.codemode.tool import register  # noqa: E402
from nexus.tools.names import canonical_permission_rule, canonical_tool_name  # noqa: E402
from nexus.tools.permissions import Outcome, PermissionEngine  # noqa: E402
from nexus.tools.spec import ToolCall, ToolSpec  # noqa: E402

TOOLS = [tool('read'), tool('grep'), tool('write', mutates=True)]


@pytest.fixture
async def kernel():
    made = []

    async def make(tools=None, **kw):
        rig = Rig(tools or TOOLS, real=True, **kw)
        rig.outcomes.update({'read': lambda args: ok('r'), 'grep': lambda args: ok('g'),
                             'write': lambda args: ok('w')})
        made.append(rig)
        await rig.start()
        return rig

    yield make
    for rig in made:
        await rig.aclose()


async def test_a_tool_disabled_after_start_is_not_callable_next_cell_and_says_why(kernel):
    r = await kernel()
    assert 'Out [1]:' in text_of(await r.cell("await grep(path='a')"))
    # The next iteration's catalog no longer has grep (the session disabled it).
    later = {t.name: t for t in register(r.kernels, [t for t in TOOLS if t.name != 'grep'], r.config)}
    result = await later['code_run'].run({'cell': "await grep(path='a')", 'purpose': 'again'}, r.ctx())
    body = text_of(result)
    assert result.is_error
    assert "NameError at line 1: name 'grep' is not callable now (disabled for this session or not available this turn)" in body
    # The kernel is untouched and every other attached tool still works.
    assert 'Out [3]:' in text_of(await later['code_run'].run({'cell': "await read(path='a')", 'purpose': 'x'}, r.ctx()))


async def test_a_tool_that_was_never_attached_names_the_way_to_attach_it(kernel):
    r = await kernel()
    body = text_of(await r.cell("await write(path='a')"))
    assert "name 'write' is not callable now (not attached; attach it with code_attach(tools=[\"write\"]))" in body
    assert not r.of('code.call')
    await r.call('code_attach', {'tools': ['write']})
    assert 'Out [2]:' in text_of(await r.cell("await write(path='a')"))


async def test_unknown_names_stay_plain_nameerrors(kernel):
    r = await kernel()
    body = text_of(await r.cell('await nothing_here()'))
    assert 'NameError' in body and 'is not callable now' not in body


async def test_kernel_tools_cannot_be_called_from_a_cell_even_when_named(kernel):
    r = await kernel()
    for name in ('code_start', 'code_attach', 'code_run', 'code_reset'):
        body = text_of(await r.cell(f"await {name}(purpose='x')"))
        assert 'NameError' in body, body
        assert f"name '{name}' is not callable now" not in body or 'drives the loop' in body
    text = text_of(await r.call('code_attach', {'tools': ['code_run', 'code_start', 'code']}))
    for name in ('code_run', 'code_start', 'code'):
        assert f'{name}: drives the loop, the user or the agent tree' in text
    assert not ({'code_run', 'code_start', 'code'} & set(r.kernels.get('s1', 'root').attached))


async def test_expose_is_the_ceiling_for_implicit_attachment_only():
    mcp = [tool('mcp__gh__read', bundle='mcp'), tool('mcp__gh__merge', bundle='mcp', mutates=True)]
    r = Rig([*TOOLS, *mcp])  # expose = read_only
    started = text_of(await r.start(servers=['gh']))
    state = r.kernels.get('s1', 'root')
    assert 'mcp__gh__read' in state.attached and 'mcp__gh__merge' not in state.attached
    assert 'refused by [code_mode] expose' in started
    await r.call('code_attach', {'tools': ['mcp__gh__merge']})
    assert 'mcp__gh__merge' in state.attached  # named explicitly: allowed, still gated per call
    glob = Rig([*TOOLS, *mcp], cfg=config(expose=['read', 'mcp__gh__*']))
    text = text_of(await glob.start(tools=['grep']))
    assert 'grep' in glob.kernels.get('s1', 'root').attached  # explicit names ignore the expose list
    assert 'Built-in tools (1 tool)' in text  # only `read` passes the expose list
    await r.aclose()
    await glob.aclose()


async def test_attached_mutating_tool_still_goes_through_the_current_pipeline(kernel):
    """The cell reaches the loop's invoke_tool (hooks, permissions); a denial is a catchable ToolError."""
    from nexus.tools.spec import NestedOutcome, ToolExecutionResult
    r = await kernel(cfg=config(expose='all'))
    r.outcomes['write'] = NestedOutcome(ToolExecutionResult.text('Error: write denied by rule', is_error=True), 'denied',
                                        {'decision': 'deny', 'rule': 'write'})
    body = text_of(await r.cell("try:\n    await write(path='a')\n    out = 'wrote'\nexcept ToolError as e:\n    out = str(e)\nout"))
    assert "Out [1]: 'Error: write denied by rule'" in body
    assert "write(path='a') ⊘ denied" in body
    assert r.of('code.call')[-1]['permission'] == {'decision': 'deny', 'rule': 'write'}


async def test_research_profile_refuses_mutating_nested_calls_from_a_kernel(tmp_path):
    h = Harness(tmp_path, profile='research')
    seen = {}

    async def script(ctx):
        cfg = config()
        kernels = CodeKernels(cfg, host=ScriptHost())
        registered = {t.name: t for t in register(kernels, h.manager.tools, cfg)}
        ctx = NS(**{**ctx.__dict__, 'agent_id': 'root'}) if hasattr(ctx, '__dict__') else ctx
        started = await registered['code_start'].run({'tools': ['peek', 'poke'], 'purpose': 'p'}, ctx)
        seen['start'] = text_of(started)
        seen['attached'] = sorted(kernels.get(ctx.session_id, 'root').attached)
        # Even a call that reached the pipeline by name is refused by the profile.
        seen['direct'] = await nested_call(ctx, 'poke')
        await kernels.aclose()

    await h.run(script)
    assert 'peek' in seen['attached'] and 'poke' not in seen['attached']
    assert 'poke: unknown tool, or disabled for this session' in seen['start']
    assert seen['direct'].status == 'denied'
    assert 'research profile refuses' in seen['direct'].result.content[0].text
    assert h.runs == []


def test_a_permission_rule_naming_code_grants_code_run():
    assert canonical_tool_name('code') == 'code_run'
    assert canonical_permission_rule('code') == 'code_run'
    spec = ToolSpec(name='code_run', description='x', bundle='code', permission_key=lambda a: 'code',
                    input_schema={'type': 'object', 'properties': {'cell': {'type': 'string'}}})
    call = ToolCall(id='1', name='code_run', input={'cell': '1'})
    root = Path(tempfile.mkdtemp())
    for rules, outcome in (({'allow': ['code']}, Outcome.ALLOW), ({'deny': ['code']}, Outcome.DENY),
                           ({'allow': ['code_run']}, Outcome.ALLOW), ({}, Outcome.ASK)):
        engine = PermissionEngine(workspace=root, mode='ask', **rules)
        assert engine.evaluate(call, spec).outcome is outcome, rules


async def test_an_agent_tools_list_naming_code_grants_the_kernel_tools(tmp_path):
    from nexus.runtime import Runtime
    cfg = Config(model='scripted/m', version=2, v2=ConfigV2(model=ModelSection(default='scripted/m')))
    runtime = Runtime(tmp_path, config=cfg, providers={})
    try:
        manifest = runtime.manifest
        kernel = {'code_start', 'code_attach', 'code_run', 'code_reset'}
        granted = runtime._build_iteration_manager(cfg, manifest, restrict=('read', canonical_tool_name('code')))
        assert kernel <= set(granted.names) and 'read' in granted.names and 'grep' not in granted.names
        absent = runtime._build_iteration_manager(cfg, manifest, restrict=('read',))
        assert not kernel & set(absent.names)
    finally:
        await runtime.aclose()


def test_kernel_tool_descriptions_stay_short():
    r = Rig(TOOLS)
    for registered in r.registered.values():
        assert len(registered.spec.description) <= 600, registered.name

