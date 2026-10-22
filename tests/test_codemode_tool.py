"""Code tool: result format, appendix, store, images, events, cancellation (plan 03 section 7 test 7).

A fake runner drives the tool's host functions directly, so these tests need
neither Monty nor a subprocess.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from nexus.errors import OperationCancelled
from nexus.model.message import Image, Text
from nexus.tools.codemode.catalog import build_catalog
from nexus.tools.codemode.runner import CodeToolError
from nexus.tools.codemode.tool import register
from nexus.tools.spec import (NestedAuthorization, NestedOutcome, RegisteredTool,
                              ToolExecutionResult, ToolSpec)


async def _noop(args, ctx):
    return None


def tool(name, mutates=False):
    spec = ToolSpec(name=name, description=f'{name} tool', bundle='fs', mutates=mutates,
                    input_schema={'type': 'object', 'properties': {'path': {'type': 'string'}}})
    return RegisteredTool(spec, _noop)


CONFIG = NS(enabled=True, expose='read_only', context=False, max_tool_seconds=120,
            max_nested_calls=64, inline_budget_tokens=2500, pinned=[])


class FakeRunner:
    context_provider = None

    def __init__(self, script):
        self.script = script
        self.kwargs = None

    async def run(self, program, *, functions, inputs=None, stubs='', limits=None, deadline=None, cancel=None):
        self.kwargs = dict(functions=functions, inputs=inputs, stubs=stubs, limits=limits)
        return await self.script(functions)


def done(value=None, output=''):
    return {'kind': 'done', 'value': value, 'output': output, 'diagnostic': None}


def failed(kind, message, line=None, output='', traceback=''):
    return {'kind': 'failed', 'value': None, 'output': output,
            'diagnostic': {'kind': kind, 'message': message, 'line': line, 'traceback': traceback}}


def make_ctx(outcomes, events, **extra):
    async def invoke(name, args, *, parent_call_id, nested_id):
        outcome = outcomes[name]
        return outcome(args) if callable(outcome) else outcome

    def emit(kind, data):
        events.append((kind, data))

    return NS(invoke_tool=invoke, authorize_nested=extra.pop('authorize_nested', None), emit=emit,
              call_id='c1', session_id='s1', cancel_token=extra.pop('cancel_token', None), **extra)


def ok(text, **kw):
    return NestedOutcome(ToolExecutionResult.text(text, **kw), 'ok', {'decision': 'allow', 'rule': None})


async def invoke_code(script, outcomes, *, program='1', events=None, saved=(), config=CONFIG, **extra):
    events = [] if events is None else events
    catalog = build_catalog([tool('read'), tool('grep')], 'read_only')
    runner = FakeRunner(script)
    registered = register(runner, catalog, config, events=lambda sid: saved)
    ctx = make_ctx(outcomes, events, **extra)
    result = await registered.run({'program': program, 'purpose': 'test'}, ctx)
    return result, events, runner


def text_of(result):
    return ''.join(b.text for b in result.content if isinstance(b, Text))


async def test_result_format_and_appendix_lists_every_call():
    async def script(f):
        a = await f['read'](path='a.py')
        try:
            await f['read'](path='/etc/hosts')
        except CodeToolError as exc:
            assert 'outside read roots' in str(exc)
        await f['grep'](path='x')
        return done({'n': len(a)}, 'hello\n')

    denied = NestedOutcome(ToolExecutionResult.text('Error: outside read roots', is_error=True), 'denied',
                           {'decision': 'deny', 'rule': 'read_denyroots'})
    outcomes = {'read': lambda args: ok('x' * 3000) if args['path'] == 'a.py' else denied,
                'grep': ok('l1\nl2')}
    result, events, _ = await invoke_code(script, outcomes)
    body = text_of(result)
    assert not result.is_error
    assert body.startswith('Program completed in ')
    assert '3 nested calls (2 ok, 1 denied)' in body
    assert '"n": 3000' in body
    assert 'Printed output:\nhello' in body
    assert '1. read {"path": "a.py"} → ok, 1 lines (2.9 KiB)' in body
    assert '2. read {"path": "/etc/hosts"} → denied: Error: outside read roots' in body
    assert '3. grep {"path": "x"} → ok, 2 lines' in body
    assert result.context_note == 'code: test → ok, 3 calls'


async def test_error_text_preserved_and_failure_format():
    async def script(f):
        with pytest.raises(CodeToolError) as info:
            await f['read'](path='nope')
        assert str(info.value) == 'File not found: nope'
        return failed('ToolError', 'File not found: nope', line=4, output='partial\n',
                      traceback='Traceback (most recent call last):\n  line 4, in <module>')

    outcomes = {'read': NestedOutcome(ToolExecutionResult.text('File not found: nope', is_error=True), 'error')}
    result, events, _ = await invoke_code(script, outcomes)
    body = text_of(result)
    assert result.is_error
    assert body.startswith('Program failed in ')
    assert 'ToolError at line 4: File not found: nope' in body
    assert 'Traceback (most recent call last)' in body
    assert 'Printed output:\npartial' in body
    assert '1. read {"path": "nope"} → error: File not found: nope' in body
    finished = [d for k, d in events if k == 'code.finished'][0]
    assert finished['status'] == 'error' and finished['errors'] == 1
    assert finished['diagnostic'] == {'kind': 'ToolError', 'message': 'File not found: nope', 'line': 4}


@pytest.mark.parametrize('kind,status,limit', [
    ('limit:memory', 'limit', 'memory'), ('limit:calls', 'limit', 'calls'),
    ('timeout', 'limit', 'time'), ('crashed', 'crashed', None)])
async def test_finished_statuses(kind, status, limit):
    async def script(f):
        return failed(kind, 'boom')

    result, events, _ = await invoke_code(script, {})
    finished = [d for k, d in events if k == 'code.finished'][0]
    assert (finished['status'], finished['limit']) == (status, limit)
    assert result.is_error


async def test_event_shape_redaction_and_clipping():
    secret = 'sk-ant-api03-' + 'a' * 40
    async def script(f):
        await f['read'](path=secret)
        await f['grep'](path='y')
        return done()

    outcomes = {'read': ok('ab ' * 50_000), 'grep': ok('fine')}
    _, events, _ = await invoke_code(script, outcomes)
    kinds = [k for k, _ in events]
    assert kinds[0] == 'code.started' and kinds[-1] == 'code.finished'
    started = events[0][1]
    assert started['call_id'] == 'c1' and started['callable'] == ['read', 'grep']
    assert {'purpose', 'program_digest', 'timeout_s', 'exposure', 'targets', 'monty_version', 'limits'} <= set(started)
    calls = [d for k, d in events if k == 'code.call']
    assert [(c['nested_id'], c['status']) for c in calls] == [
        ('c1.1', 'running'), ('c1.1', 'ok'), ('c1.2', 'running'), ('c1.2', 'ok')]
    first = calls[1]
    assert first['kind'] == 'tool' and first['index'] == 1 and first['permission'] == {'decision': 'allow', 'rule': None}
    assert secret not in str(calls[0]['args'])
    assert first['bytes'] == 150_000 and first['clipped'] and len(first['result']) < 101_000
    assert calls[3]['clipped'] is False
    assert [d['ok'] for k, d in events if k == 'code.finished'] == [2]


async def test_store_commits_only_on_success_and_replays():
    async def script_ok(f):
        await f['store']('cursor', [1, 2])
        await f['store_delete']('gone')
        assert await f['load']('cursor') == [1, 2]
        assert await f['load']('kept') == 7
        return done(1)

    saved = [{'type': 'code.store', 'data': {'set': {'kept': 7, 'gone': 1}, 'delete': []}}]
    _, events, _ = await invoke_code(script_ok, {}, saved=saved)
    stores = [d for k, d in events if k == 'code.store']
    assert stores == [{'call_id': 'c1', 'set': {'cursor': [1, 2]}, 'delete': ['gone']}]

    async def script_fail(f):
        await f['store']('cursor', 1)
        return failed('NameError', 'x')

    _, events, _ = await invoke_code(script_fail, {})
    assert not [k for k, _ in events if k == 'code.store']

    # replay: a later call sees the committed value
    seen = {}
    async def script_later(f):
        seen['v'] = await f['load']('cursor')
        return done()

    await invoke_code(script_later, {}, saved=[{'type': 'code.store', 'data': stores[0]}])
    assert seen['v'] == [1, 2]


async def test_store_caps_are_catchable():
    async def script(f):
        with pytest.raises(CodeToolError):
            await f['store']('big', 'x' * 70000)
        return done()

    _, events, _ = await invoke_code(script, {})
    rows = [d for k, d in events if k == 'code.call' and d['status'] != 'running']
    assert rows[0]['kind'] == 'store' and rows[0]['status'] == 'error'
    assert not [k for k, _ in events if k == 'code.store']


async def test_image_ref_attached_when_returned():
    image = Image(media_type='image/png', data=b'\x89PNG')
    shot = NestedOutcome(ToolExecutionResult(content=[Text(text='caption'), image]), 'ok')
    got = {}

    async def script(f):
        got['v'] = await f['read'](path='p.png')
        await f['read'](path='q.png')
        return done(got['v'][1])

    result, _, _ = await invoke_code(script, {'read': shot})
    assert got['v'] == ['caption', {'type': 'image', 'ref': 'img-1'}]
    assert [b for b in result.content if isinstance(b, Image)] == [image]

    async def printed(f):
        await f['read'](path='p.png')
        return done('x', 'see img-1\n')

    result, _, _ = await invoke_code(printed, {'read': shot})
    assert len([b for b in result.content if isinstance(b, Image)]) == 1

    async def unreferenced(f):
        await f['read'](path='p.png')
        return done('nothing')

    result, _, _ = await invoke_code(unreferenced, {'read': shot})
    assert not [b for b in result.content if isinstance(b, Image)]


async def test_bash_exit_code_and_structured_metrics():
    shell = tool('bash')
    catalog = build_catalog([shell], 'all')
    got = []

    async def script(f):
        got.append(await f['bash'](path='x'))
        return done()

    outcome = NestedOutcome(ToolExecutionResult.text('out', metrics={'exit_code': 3}), 'ok')
    registered = register(FakeRunner(script), catalog, NS(**{**CONFIG.__dict__, 'expose': 'all'}))
    await registered.run({'program': '1', 'purpose': 'p'}, make_ctx({'bash': outcome}, []))
    assert got == [{'exit_code': 3, 'output': 'out'}]


@pytest.mark.parametrize('name,kwargs', [('shell', {'command': 'sleep 9', 'background': True}),
                                         ('bash', {'command': 'sleep 9', 'run_in_background': True})])
async def test_background_shell_jobs_are_refused_without_dispatch(name, kwargs):
    catalog = build_catalog([tool(name, mutates=True)], 'all')
    dispatched = []
    raised = []

    async def script(f):
        try:
            await f[name](**kwargs)
        except CodeToolError as exc:
            raised.append(str(exc))
        return done()

    def invoke(tool_name, args):
        dispatched.append(tool_name)
        return ok('x')

    registered = register(FakeRunner(script), catalog, NS(**{**CONFIG.__dict__, 'expose': 'all'}))
    await registered.run({'program': '1', 'purpose': 'p'}, make_ctx({name: invoke}, []))
    assert dispatched == []
    assert raised and 'background shell jobs are not available inside code' in raised[0]


async def test_result_clipping_is_announced():
    async def script(f):
        return done('v' * 300_000)

    result, _, _ = await invoke_code(script, {})
    body = text_of(result)
    assert len(body) < 101_000
    assert '[result clipped: showing' in body


async def test_cancellation_emits_finished_and_reraises():
    async def script(f):
        raise asyncio.CancelledError

    events = []
    with pytest.raises(asyncio.CancelledError):
        await invoke_code(script, {}, events=events)
    assert events[-1][0] == 'code.finished' and events[-1][1]['status'] == 'cancelled'

    async def cancelled(f):
        return failed('cancelled', 'program cancelled')

    events = []
    with pytest.raises(OperationCancelled):
        await invoke_code(cancelled, {}, events=events)
    assert events[-1][1]['status'] == 'cancelled'


class Provider:
    def __init__(self, deny_check=False):
        self.bound = None
        self.deny_check = deny_check

    def resolve(self, caller, ref):
        return {'ref': ref, 'name': None if ref == 'self' else ref, 'logical_id': f'L-{ref}', 'stored_id': f'id-{ref}'}

    def check(self, caller, stored):
        if self.deny_check:
            from nexus.session.context_authz import ContextDenied
            raise ContextDenied('not your session')

    def bind(self, caller, approved, *, depth=0):
        self.bound = dict(approved)

        async def context_meta(target='self'):
            return {'target': target}

        async def llm(prompt, text, max_tokens=None):
            return {'untrusted': True, 'text': 'hi', 'model': 'm1', 'input_tokens': 5, 'output_tokens': 2}
        return {'context_meta': context_meta, 'llm': llm}

    def stubs(self):
        return 'targets: list[str]\n'


CONTEXT_CONFIG = NS(**{**CONFIG.__dict__, 'context': True})


async def test_context_targets_authorized_before_running_and_llm_row():
    asked = []

    async def authorize(key, *, parent_call_id, summary):
        asked.append((key, parent_call_id, summary))
        return NestedAuthorization(True, 'ask:allow')

    ran = []

    async def script(f):
        ran.append(True)
        await f['context_meta']('quiet-cobalt-otter')
        await f['llm']('p', 't')
        return done()

    events = []
    catalog = build_catalog([tool('read')], 'read_only')
    runner = FakeRunner(script)
    runner.context_provider = Provider()
    registered = register(runner, catalog, CONTEXT_CONFIG)
    ctx = make_ctx({}, events, authorize_nested=authorize)
    result = await registered.run({'program': 'x', 'purpose': 'why', 'targets': ['self', 'quiet-cobalt-otter']}, ctx)
    assert not result.is_error
    assert [a[0] for a in asked] == ['ContextQuery(quiet-cobalt-otter)']
    assert asked[0][2]['target'] == 'quiet-cobalt-otter' and asked[0][2]['purpose'] == 'why'
    assert runner.kwargs['inputs'] == {'targets': ['self', 'quiet-cobalt-otter']}
    assert 'targets: list[str]' in runner.kwargs['stubs']
    assert [t['ref'] for t in events[0][1]['targets']] == ['self', 'quiet-cobalt-otter']
    rows = {d['tool']: d for k, d in events if k == 'code.call' and d['status'] == 'ok'}
    assert rows['context_meta']['kind'] == 'context' and rows['llm']['kind'] == 'llm'
    assert (rows['llm']['model'], rows['llm']['input_tokens'], rows['llm']['output_tokens']) == ('m1', 5, 2)
    finished = events[-1][1]
    assert (finished['sub_calls'], finished['sub_tokens']) == (1, 7)
    assert '1 sub-call' in text_of(result)


@pytest.mark.parametrize('mode', ['refused', 'unavailable', 'check'])
async def test_denied_target_returns_before_running(mode):
    ran = []

    async def script(f):
        ran.append(True)
        return done()

    async def authorize(key, *, parent_call_id, summary):
        return NestedAuthorization(False, 'deny', 'rule', 'ContextQuery denied by rule')

    events = []
    runner = FakeRunner(script)
    runner.context_provider = Provider(deny_check=mode == 'check')
    registered = register(runner, build_catalog([tool('read')], 'read_only'), CONTEXT_CONFIG)
    extra = {} if mode == 'unavailable' else {'authorize_nested': authorize}
    ctx = make_ctx({}, events, **extra)
    result = await registered.run({'program': 'x', 'purpose': 'p', 'targets': ['other']}, ctx)
    assert result.is_error and not ran
    assert events[-1][0] == 'code.finished' and events[-1][1]['status'] == 'denied'


async def test_description_and_spec():
    catalog = build_catalog([tool('read'), tool('write', mutates=True)], 'all')
    registered = register(FakeRunner(None), catalog, NS(**{**CONFIG.__dict__, 'expose': 'all'}))
    spec = registered.spec
    assert spec.mutates and spec.concurrency == 'exclusive'
    for needle in ('asyncio.gather', 'ToolError', 'last top-level expression', 'No `match`', 'Importable'):
        assert needle in spec.description
    assert 'async def read(' in spec.description
