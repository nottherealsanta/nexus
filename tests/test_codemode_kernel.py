"""The persistent kernel on real Monty: cells, state, errors, reset, isolation (kernel plan §3.1, §3.5, §6)."""
from __future__ import annotations

import pytest

pytest.importorskip('pydantic_monty')

from test_codemode_tool import Rig, config, ok, text_of, tool  # noqa: E402


@pytest.fixture
async def kernel():
    made = []

    async def make(tools=None, **kw):
        rig = Rig(tools, real=True, **kw)
        rig.outcomes.update({'read': lambda args: ok('contents of ' + args['path']),
                             'grep': lambda args: ok('hit in ' + args['path'])})
        made.append(rig)
        await rig.start()
        return rig

    yield make
    for rig in made:
        await rig.aclose()


async def test_variables_functions_and_imports_persist_across_cells(kernel):
    r = await kernel()
    await r.cell('import json\nx = 41\ndef bump(v):\n    return v + 1\n')
    body = text_of(await r.cell('bump(x)'))
    assert body.startswith('In [2]  ✓') and 'Out [2]: 42' in body
    body = text_of(await r.cell("json.dumps({'a': x})"))
    assert 'Out [3]: \'{"a": 41}\'' in body


async def test_last_expression_is_out_and_assignment_only_has_none(kernel):
    r = await kernel()
    assignment = text_of(await r.cell('items = [1, 2, 3]'))
    assert 'Out [' not in assignment and assignment.startswith('In [1]  ✓')
    looked = text_of(await r.cell('items'))
    assert 'Out [2]: [1, 2, 3]    (list, 3 items)' in looked
    d = text_of(await r.cell("{'bug': 12, 'ui': 4}"))
    assert "Out [3]: {'bug': 12, 'ui': 4}    (dict, 2 keys)" in d


async def test_print_is_stdout_and_multiline_strings_are_shown_verbatim(kernel):
    r = await kernel()
    body = text_of(await r.cell('print("scanning 3 repos")\n"line one\\nline two"'))
    assert 'stdout:\n  scanning 3 repos' in body
    assert 'Out [1]: (str, 17 chars, 2 lines)\nline one\nline two' in body


async def test_an_error_keeps_state_and_earlier_changes(kernel):
    r = await kernel()
    await r.cell('kept = 1')
    failed = await r.cell('kept = 2\nboom = 1 / 0')
    assert failed.is_error
    assert 'ZeroDivisionError' in text_of(failed) and text_of(failed).startswith('In [2]  ✗ ZeroDivisionError')
    # Failed cells are not transactional: the rebinding before the error stays.
    assert 'Out [3]: 2' in text_of(await r.cell('kept'))
    assert r.kernels.get('s1', 'root').n == 3
    assert r.of('code.cell_finished')[1]['status'] == 'error'


async def test_tool_calls_fan_out_and_are_listed_under_the_cell(kernel):
    r = await kernel()
    cell = ('import asyncio\n'
            "texts = await asyncio.gather(*[read(path=p) for p in ['a.py', 'b.py']])\n"
            'len(texts)')
    result = await r.cell(cell, purpose='read two')
    body = text_of(result)
    assert 'Out [1]: 2' in body and '· 2 calls · purpose: read two' in body
    assert "1 read(path='a.py') ✓ 18 B" in body or "1 read(path='a.py')" in body
    calls = [d for d in r.of('code.call') if d['status'] == 'ok']
    assert sorted(c['args']['path'] for c in calls) == ['a.py', 'b.py']
    assert all(c['parent_call_id'] == c['call_id'] for c in calls)
    # The stored result can be looked at later without another tool call.
    assert "Out [2]: ['contents of a.py', 'contents of b.py']" in text_of(await r.cell('texts'))


async def test_tool_errors_are_catchable_toolerrors(kernel):
    from nexus.tools.spec import NestedOutcome, ToolExecutionResult
    r = await kernel()
    r.outcomes['read'] = NestedOutcome(ToolExecutionResult.text('File not found: nope', is_error=True), 'error')
    body = text_of(await r.cell("try:\n    await read(path='nope')\n    r = 'no error'\nexcept ToolError as e:\n    r = str(e)\nr"))
    assert "Out [1]: 'File not found: nope'" in body
    assert "1 read(path='nope') ✗ error: File not found: nope" in body


async def test_type_errors_are_caught_before_any_call_and_leave_no_trace(kernel):
    r = await kernel()
    result = await r.cell('y = 5\nawait read(path=123)')
    body = text_of(result)
    assert result.is_error and body.startswith('In [1]  ✗ TypeError') and 'line 2' in body
    assert not r.of('code.call')
    # Nothing ran: ``y`` was never bound.
    assert 'NameError' in text_of(await r.cell('y'))


async def test_reset_clears_variables_and_renumbers(kernel):
    r = await kernel()
    await r.cell('x = 1')
    await r.cell('x')
    result = await r.call('code_reset', {})
    assert not result.is_error and 'In [n] restarts at 1' in text_of(result)
    body = text_of(await r.cell('x'))
    assert body.startswith('In [1]  ✗ NameError')
    assert 'In [2]  ✓' in text_of(await r.cell('x = 2'))
    assert r.of('code.kernel_reset')[0]['stopped'] is False
    assert 'read' in r.kernels.get('s1', 'root').attached  # attachments survive a reset


async def test_stop_then_run_is_kernel_not_started_and_start_recovers(kernel):
    r = await kernel()
    await r.cell('x = 1')
    stopped = await r.call('code_reset', {'stop': True})
    assert 'freed' in text_of(stopped) and r.of('code.kernel_reset')[0]['stopped'] is True
    result = await r.cell('x')
    assert result.is_error and text_of(result).startswith('KernelNotStarted')
    await r.start()
    assert 'NameError' in text_of(await r.cell('x'))


async def test_each_agent_has_its_own_kernel(kernel):
    r = await kernel()
    await r.call('code_start', {}, agent_id='child-1')
    await r.cell('mine = 1')
    assert 'NameError' in text_of(await r.cell('mine', agent_id='child-1'))
    await r.cell('theirs = 2', agent_id='child-1')
    assert 'NameError' in text_of(await r.cell('theirs'))
    root, child = r.kernels.get('s1', 'root'), r.kernels.get('s1', 'child-1')
    assert root.kernel_id != child.kernel_id


async def test_whos_lists_names_types_and_sizes_but_never_values(kernel):
    r = await kernel()
    await r.cell("secret = 'hunter2'\nrows = [1, 2, 3]\ncount = 7")
    body = text_of(await r.cell('whos()'))
    for line in ('name  type  size', 'secret  str  7', 'rows  list  3', 'count  int'):
        assert line in body
    assert 'hunter2' not in body and 'ToolError' not in body and '_nx_vars' not in body
    assert 'whos  function' not in body


async def test_timeout_discards_the_worker_and_the_kernel_recovers_empty(kernel):
    r = await kernel()
    await r.cell('x = 1')
    result = await r.cell('while True:\n    pass', timeout_s=1)
    body = text_of(result)
    assert result.is_error and 'Kernel reset' in body and 'In [n] restarts at 1' in body
    assert r.of('code.cell_finished')[-1]['limit'] == 'time'
    after = text_of(await r.cell('x = 5\nx'))
    assert after.startswith('In [1]  ✓') and 'Out [1]: 5' in after


async def test_closing_a_session_closes_its_kernels(kernel):
    r = await kernel()
    await r.cell('x = 1')
    await r.kernels.close_session('s1')
    result = await r.cell('x')
    assert result.is_error and text_of(result).startswith('KernelNotStarted')


async def test_helpers_search_and_describe_do_not_attach(kernel):
    r = await kernel([tool('read'), tool('grep'), tool('write', mutates=True)])
    found = text_of(await r.cell("hits = await search_tools('write')\nhits"))
    assert "'name': 'write'" in found and "'attached': False" in found
    described = text_of(await r.cell("d = await describe_tool('write')\nd['stub']"))
    assert 'async def write(' in described
    assert 'write' not in r.kernels.get('s1', 'root').attached


async def test_cell_with_a_secret_is_redacted_in_events_but_runs(kernel):
    r = await kernel()
    secret = 'sk-ant-api03-' + 'a' * 40
    body = text_of(await r.cell(f's = "{secret}"\nlen(s)'))
    assert 'Out [1]: ' in body
    assert secret not in r.of('code.cell_started')[0]['source']


async def test_many_gathered_calls_stay_within_the_per_cell_suspension_budget(kernel):
    r = await kernel(cfg=config(max_nested_calls=64))
    cell = 'import asyncio\nrs = await asyncio.gather(*[read(path="a") for _ in range(60)])\nlen(rs)'
    body = text_of(await r.cell(cell))
    assert 'Out [1]: 60' in body
