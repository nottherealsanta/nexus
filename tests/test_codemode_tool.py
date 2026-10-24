"""Kernel tools and cells: result format, call list, events, images, cancellation (kernel plan §3.1, §3.5, §3.7).

A scripted host drives the cell's host functions directly, so these tests need
neither Monty nor a subprocess. ``Rig`` is reused by the other code-mode tests.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import msgspec
import pytest

from nexus.config.schema import CodeModeSection
from nexus.errors import OperationCancelled
from nexus.model.message import Image, Text
from nexus.tools.codemode.kernel_host import KernelLost
from nexus.tools.codemode.kernels import CodeKernels
from nexus.tools.codemode.runner import CodeToolError
from nexus.tools.codemode.tool import register
from nexus.tools.spec import (NestedAuthorization, NestedOutcome, RegisteredTool,
                              ToolExecutionResult, ToolSpec)


async def _noop(args, ctx):
    return None


def tool(name, mutates=False, bundle='fs'):
    spec = ToolSpec(name=name, description=f'{name} tool', bundle=bundle, mutates=mutates,
                    input_schema={'type': 'object', 'properties': {'path': {'type': 'string'}}})
    return RegisteredTool(spec, _noop)


CONFIG = CodeModeSection()


def config(**changes):
    return msgspec.structs.replace(CONFIG, **changes)


def done(value=None, output=''):
    return {'kind': 'done', 'value': value, 'output': output, 'diagnostic': None}


def failed(kind, message, line=None, output='', traceback=''):
    return {'kind': 'failed', 'value': None, 'output': output,
            'diagnostic': {'kind': kind, 'message': message, 'line': line, 'traceback': traceback}}


def ok(text, **kw):
    return NestedOutcome(ToolExecutionResult.text(text, **kw), 'ok', {'decision': 'allow', 'rule': None})


def text_of(result):
    return ''.join(b.text for b in result.content if isinstance(b, Text))


class ScriptHost:
    """A ``KernelHost`` stand-in: ``feed`` runs ``script(functions)`` for the outcome."""

    max_kernels = 32

    def __init__(self):
        self.script = None
        self.fed = []
        self.opened = []
        self.fail = None
        self._next = 0

    async def open(self):
        self._next += 1
        self.opened.append(self._next)
        return self._next

    async def feed(self, handle, program, *, functions, inputs=None, deadline=None):
        self.fed.append({'program': program, 'functions': functions, 'inputs': inputs, 'deadline': deadline})
        if self.fail is not None:
            raise self.fail
        return await self.script(functions)

    async def dump(self, handle):
        return b'snapshot'

    async def load(self, handle, snapshot):
        return None

    async def close(self, handle):
        return None

    async def shutdown(self):
        return None


class Rig:
    """Four kernel tools over a scripted host, with a recording nested pipeline."""

    def __init__(self, tools=None, *, cfg=None, provider=None, outcomes=None, real=False, store=None,
                 events=None):
        self.config = cfg or CONFIG
        # ``real=True`` runs cells in the real Monty kernel host (no scripted outcomes).
        self.host = None if real else ScriptHost()
        #: ``store`` turns durability on; ``events`` may be shared with an earlier Rig (a "restart").
        self.events = events if events is not None else []
        self.kernels = CodeKernels(self.config, context_provider=provider, host=self.host, store=store,
                                   log_reader=self._code_events if store is not None else None)
        self.tools = tools if tools is not None else [tool('read'), tool('grep')]
        self.outcomes = outcomes or {}
        self.registered = {t.name: t for t in register(self.kernels, self.tools, self.config)}
        self.n = 0

    def _code_events(self, session_id):
        from nexus.events import Event

        return [Event(type=k, data=d, session=session_id) for k, d in list(self.events) if k.startswith('code.')]

    def ctx(self, **extra):
        async def invoke(name, args, *, parent_call_id, nested_id):
            outcome = self.outcomes[name]
            return outcome(args) if callable(outcome) else outcome

        self.n += 1
        return NS(invoke_tool=invoke, authorize_nested=extra.pop('authorize_nested', None),
                  emit=lambda kind, data: self.events.append((kind, data)),
                  call_id=extra.pop('call_id', f'c{self.n}'), session_id='s1',
                  agent_id=extra.pop('agent_id', 'root'),
                  cancel_token=extra.pop('cancel_token', None), **extra)

    async def call(self, name, args, **extra):
        args = {'purpose': 'test', **args}
        return await self.registered[name].run(args, self.ctx(**extra))

    async def start(self, **args):
        return await self.call('code_start', args)

    async def run(self, script, cell='1', *, purpose='test', **extra):
        self.host.script = script
        return await self.call('code_run', {'cell': cell, 'purpose': purpose}, **extra)

    async def cell(self, cell, *, purpose='test', **extra):
        """Run a cell in the real kernel (``real=True``)."""
        args = {'cell': cell, 'purpose': purpose}
        if 'timeout_s' in extra:
            args['timeout_s'] = extra.pop('timeout_s')
        return await self.call('code_run', args, **extra)

    async def aclose(self):
        await self.kernels.aclose()

    def of(self, kind):
        return [d for k, d in self.events if k == kind]


@pytest.fixture
async def rig():
    made = []

    async def make(*args, **kw):
        r = Rig(*args, **kw)
        made.append(r)
        await r.start()
        return r

    yield make
    for r in made:
        await r.aclose()


async def test_result_format_and_call_list_lists_every_call(rig):
    r = await rig(outcomes={})

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
    r.outcomes.update({'read': lambda args: ok('x' * 3000) if args['path'] == 'a.py' else denied,
                       'grep': ok('l1\nl2')})
    result = await r.run(script)
    body = text_of(result)
    assert not result.is_error
    assert body.startswith('In [1]  ✓ ')
    assert '· 3 calls · purpose: test' in body
    assert "Out [1]: {'n': 3000}    (dict, 1 key)" in body
    assert 'stdout:\n  hello' in body
    assert "  1 read(path='a.py') ✓ 3 KB" in body
    assert "  2 read(path='/etc/hosts') ⊘ denied: Error: outside read roots" in body
    assert "  3 grep(path='x') ✓ 5 B" in body
    assert result.context_note == 'code_run In [1]: test → ok, 3 calls'


async def test_assignment_only_cell_has_no_out_line(rig):
    r = await rig()

    async def script(f):
        return done(None)

    body = text_of(await r.run(script, 'x = 1'))
    assert 'Out [' not in body and body.startswith('In [1]  ✓')


async def test_error_text_preserved_and_failure_format(rig):
    r = await rig()

    async def script(f):
        with pytest.raises(CodeToolError) as info:
            await f['read'](path='nope')
        assert str(info.value) == 'File not found: nope'
        return failed('ToolError', 'File not found: nope', line=4, output='partial\n',
                      traceback='Traceback (most recent call last):\n  line 4, in <module>')

    r.outcomes['read'] = NestedOutcome(ToolExecutionResult.text('File not found: nope', is_error=True), 'error')
    result = await r.run(script)
    body = text_of(result)
    assert result.is_error
    assert body.startswith('In [1]  ✗ ToolError ')
    assert 'ToolError at line 4: File not found: nope' in body
    assert 'Traceback (most recent call last)' in body
    assert 'stdout:\n  partial' in body
    assert "  1 read(path='nope') ✗ error: File not found: nope" in body
    finished = r.of('code.cell_finished')[0]
    assert finished['status'] == 'error' and finished['errors'] == 1
    assert finished['diagnostic'] == {'kind': 'ToolError', 'message': 'File not found: nope', 'line': 4}


@pytest.mark.parametrize('kind,status,limit', [
    ('limit:memory', 'limit', 'memory'), ('limit:calls', 'limit', 'calls'),
    ('timeout', 'limit', 'time'), ('crashed', 'crashed', None)])
async def test_finished_statuses(rig, kind, status, limit):
    r = await rig()

    async def script(f):
        return failed(kind, 'boom')

    result = await r.run(script)
    finished = r.of('code.cell_finished')[0]
    assert (finished['status'], finished['limit']) == (status, limit)
    assert result.is_error


async def test_event_shape_redaction_and_clipping(rig):
    secret = 'sk-ant-api03-' + 'a' * 40
    r = await rig()

    async def script(f):
        await f['read'](path=secret)
        await f['grep'](path='y')
        return done()

    r.outcomes.update({'read': ok('ab ' * 50_000), 'grep': ok('fine')})
    await r.run(script, cell=f'x = "{secret}"')
    cell_events = [(k, d) for k, d in r.events if k in ('code.cell_started', 'code.call', 'code.cell_finished')]
    kinds = [k for k, _ in cell_events]
    assert kinds[0] == 'code.cell_started' and kinds[-1] == 'code.cell_finished'
    started = cell_events[0][1]
    assert started['call_id'] == 'c2' and started['n'] == 1 and started['callable'] == ['read', 'grep']
    assert {'kernel_id', 'source', 'purpose', 'timeout_s', 'monty_version'} <= set(started)
    assert secret not in started['source']
    calls = [d for k, d in cell_events if k == 'code.call']
    assert [(c['nested_id'], c['status']) for c in calls] == [
        ('c2.1', 'running'), ('c2.1', 'ok'), ('c2.2', 'running'), ('c2.2', 'ok')]
    assert all(c['parent_call_id'] == 'c2' and c['call_id'] == 'c2' for c in calls)
    first = calls[1]
    assert first['kind'] == 'tool' and first['index'] == 1 and first['permission'] == {'decision': 'allow', 'rule': None}
    assert secret not in str(calls[0]['args'])
    assert first['bytes'] == 150_000 and first['clipped'] and len(first['result']) < 101_000
    assert calls[3]['clipped'] is False
    assert [d['ok'] for k, d in cell_events if k == 'code.cell_finished'] == [2]
    assert {'stdout_bytes', 'out_summary', 'clipped', 'calls'} <= set(r.of('code.cell_finished')[0])


async def test_image_ref_attached_when_returned(rig):
    r = await rig()
    image = Image(media_type='image/png', data=b'\x89PNG')
    shot = NestedOutcome(ToolExecutionResult(content=[Text(text='caption'), image]), 'ok')
    r.outcomes['read'] = shot
    got = {}

    async def script(f):
        got['v'] = await f['read'](path='p.png')
        await f['read'](path='q.png')
        return done(got['v'][1])

    result = await r.run(script)
    assert got['v'] == ['caption', {'type': 'image', 'ref': 'img-1'}]
    assert [b for b in result.content if isinstance(b, Image)] == [image]

    async def printed(f):
        await f['read'](path='p.png')
        return done('x', 'see img-1\n')

    result = await r.run(printed)
    assert len([b for b in result.content if isinstance(b, Image)]) == 1

    async def unreferenced(f):
        await f['read'](path='p.png')
        return done('nothing')

    result = await r.run(unreferenced)
    assert not [b for b in result.content if isinstance(b, Image)]


async def test_bash_exit_code_and_structured_metrics():
    r = Rig([tool('bash')], cfg=config(expose='all'))
    await r.start()
    got = []

    async def script(f):
        got.append(await f['bash'](path='x'))
        return done()

    r.outcomes['bash'] = NestedOutcome(ToolExecutionResult.text('out', metrics={'exit_code': 3}), 'ok')
    await r.run(script)
    assert got == [{'exit_code': 3, 'output': 'out'}]
    await r.aclose()


@pytest.mark.parametrize('name,kwargs', [('shell', {'command': 'sleep 9', 'background': True}),
                                         ('bash', {'command': 'sleep 9', 'run_in_background': True})])
async def test_background_shell_jobs_are_refused_without_dispatch(name, kwargs):
    r = Rig([tool(name, mutates=True)], cfg=config(expose='all'))
    await r.start()
    dispatched = []
    raised = []

    async def script(f):
        try:
            await f[name](**kwargs)
        except CodeToolError as exc:
            raised.append(str(exc))
        return done()

    def invoke(args):
        dispatched.append(name)
        return ok('x')

    r.outcomes[name] = invoke
    await r.run(script)
    assert dispatched == []
    assert raised and 'background shell jobs are not available inside code' in raised[0]
    await r.aclose()


async def test_result_clipping_is_announced_with_a_hint(rig):
    r = await rig()

    async def script(f):
        return done('v' * 300_000)

    body = text_of(await r.run(script, cell='big = "v" * 300000\nbig'))
    assert len(body) < 9_000
    assert '[Out [1] clipped: showing' in body and 'of 300002 chars' in body
    assert 'try big[:5] or len(big)' in body
    assert r.of('code.cell_finished')[0]['clipped'] is True


async def test_stdout_and_out_share_the_output_budget(rig):
    r = await rig(cfg=config(max_output_chars=1000))

    async def script(f):
        return done('o' * 5000, 's' * 5000)

    body = text_of(await r.run(script))
    assert len(body) < 2_500
    assert '[stdout clipped:' in body and '[Out [1] clipped:' in body


async def test_cancellation_emits_finished_and_reraises(rig):
    r = await rig()

    async def script(f):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await r.run(script)
    assert r.events[-1][0] == 'code.cell_finished' and r.events[-1][1]['status'] == 'cancelled'

    r2 = await rig()

    async def cancelled(f):
        return failed('cancelled', 'cell cancelled')

    with pytest.raises(OperationCancelled):
        await r2.run(cancelled)
    assert r2.events[-1][1]['status'] == 'cancelled'


async def test_kernel_not_started_and_unknown_state_errors():
    r = Rig()
    result = await r.call('code_run', {'cell': '1', 'purpose': 'p'})
    assert result.is_error and text_of(result).startswith('KernelNotStarted: call code_start first')
    assert r.host.fed == [] and r.host.opened == []
    result = await r.call('code_attach', {'tools': ['read']})
    assert result.is_error and 'KernelNotStarted' in text_of(result)
    result = await r.call('code_reset', {})
    assert not result.is_error and 'nothing to reset' in text_of(result)
    await r.aclose()


async def test_cell_size_and_cell_cap_are_enforced_before_any_feed(rig):
    r = await rig(cfg=config(max_cells=10))
    result = await r.run(None, cell='x' * 16385)
    assert result.is_error and 'limit is 16384' in text_of(result)
    assert r.host.fed == [] and r.kernels.get('s1', 'root').n == 0

    async def script(f):
        return done(1)

    for _ in range(10):
        assert not (await r.run(script)).is_error
    result = await r.run(script)
    assert result.is_error and 'max_cells' in text_of(result) and 'code_reset' in text_of(result)
    assert len(r.host.fed) == 10


async def test_whos_call_is_rewritten_to_top_level_locals(rig):
    r = await rig()

    async def script(f):
        return done('table')

    await r.run(script, cell='a = 1\nwhos()')
    assert r.host.fed[-1]['program'] == 'a = 1\nwhos(locals())'


async def test_lost_worker_resets_empty_and_keeps_attachments(rig):
    r = await rig()
    r.host.fail = KernelLost('worker lost')

    async def script(f):
        return done(1)

    result = await r.run(script)
    body = text_of(result)
    assert result.is_error and body.startswith('In [1]  ✗ KernelLost ')
    assert 'Kernel reset' in body and 'In [n] restarts at 1' in body
    assert r.of('code.kernel_reset')[-1]['reason'] == 'lost'
    state = r.kernels.get('s1', 'root')
    assert state.n == 0 and 'read' in state.attached
    r.host.fail = None
    assert not (await r.run(script)).is_error
    assert r.kernels.get('s1', 'root').n == 1


async def test_deadline_timeout_discards_the_worker_and_says_so(rig):
    r = await rig()
    r.host.fail = TimeoutError()

    async def script(f):
        return done(1)

    result = await r.run(script)
    body = text_of(result)
    assert result.is_error and 'worker was discarded' in body and 'Kernel reset' in body
    finished = r.of('code.cell_finished')[0]
    assert (finished['status'], finished['limit']) == ('limit', 'time')


async def test_after_cell_hook_runs_only_after_success(rig):
    r = await rig()
    seen = []

    async def hook(state, n):
        seen.append(n)

    r.kernels.after_cell = hook

    async def good(f):
        return done(1)

    async def bad(f):
        return failed('NameError', 'x')

    await r.run(good)
    await r.run(bad)
    await r.run(good)
    assert seen == [1, 3]


async def test_nested_dispatch_must_be_bound(rig):
    r = await rig()
    ctx = r.ctx()
    ctx.invoke_tool = None
    result = await r.registered['code_run'].run({'cell': '1', 'purpose': 'p'}, ctx)
    assert result.is_error and 'nested dispatch is not bound' in text_of(result)


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
        return 'targets: list[str]\nasync def context_meta(target: str = "self") -> dict: ...\n'


async def test_context_targets_authorized_at_start_and_bound_per_cell():
    asked = []

    async def authorize(key, *, parent_call_id, summary):
        asked.append((key, parent_call_id, summary))
        return NestedAuthorization(True, 'ask:allow')

    provider = Provider()
    r = Rig(cfg=config(context=True), provider=provider)
    started = await r.call('code_start', {'targets': ['quiet-cobalt-otter'], 'purpose': 'why'},
                           authorize_nested=authorize)
    assert [a[0] for a in asked] == ['ContextQuery(quiet-cobalt-otter)']
    assert asked[0][2]['target'] == 'quiet-cobalt-otter' and asked[0][2]['purpose'] == 'why'
    assert 'Context targets' in text_of(started) and 'quiet-cobalt-otter' in text_of(started)
    event = r.of('code.kernel_started')[0]
    assert [t['ref'] for t in event['attached']['targets']] == ['self', 'quiet-cobalt-otter']

    async def script(f):
        await f['context_meta']('quiet-cobalt-otter')
        await f['llm']('p', 't')
        return done()

    result = await r.run(script)
    assert not result.is_error
    assert provider.bound == {'self': 'id-self', 'quiet-cobalt-otter': 'id-quiet-cobalt-otter'}
    assert r.host.fed[-1]['inputs'] == {'targets': ['self', 'quiet-cobalt-otter']}
    rows = {d['tool']: d for d in r.of('code.call') if d['status'] == 'ok'}
    assert rows['context_meta']['kind'] == 'context' and rows['llm']['kind'] == 'llm'
    assert (rows['llm']['model'], rows['llm']['input_tokens'], rows['llm']['output_tokens']) == ('m1', 5, 2)
    finished = r.of('code.cell_finished')[0]
    assert (finished['sub_calls'], finished['sub_tokens']) == (1, 7)
    assert '1 sub-call' in text_of(result)
    await r.aclose()


@pytest.mark.parametrize('mode', ['refused', 'unavailable', 'check', 'disabled'])
async def test_denied_target_is_listed_not_attached_with_its_reason(mode):
    async def authorize(key, *, parent_call_id, summary):
        return NestedAuthorization(False, 'deny', 'rule', 'ContextQuery denied by rule')

    provider = None if mode == 'disabled' else Provider(deny_check=mode == 'check')
    r = Rig(cfg=config(context=mode != 'disabled'), provider=provider)
    extra = {} if mode == 'unavailable' else {'authorize_nested': authorize}
    result = await r.call('code_start', {'targets': ['other'], 'purpose': 'p'}, **extra)
    text = text_of(result)
    assert not result.is_error
    not_attached = text.split('5. Not attached')[1]
    assert 'target other:' in not_attached
    reason = {'refused': 'ContextQuery denied by rule', 'unavailable': 'needs approval but none is available',
              'check': 'not your session', 'disabled': 'enable [code_mode] context'}[mode]
    assert reason in not_attached
    assert 'other' not in (r.kernels.get('s1', 'root').approved)
    await r.aclose()


async def test_tool_descriptions_specs_and_permission_key():
    r = Rig([tool('read'), tool('write', mutates=True)], cfg=config(expose='all'))
    assert list(r.registered) == ['code_start', 'code_attach', 'code_run', 'code_reset']
    for registered in r.registered.values():
        spec = registered.spec
        assert len(spec.description) <= 600 and spec.bundle == 'code'
        assert spec.input_schema['additionalProperties'] is False
    assert 'preferred' in r.registered['code_start'].spec.description
    assert 'multi-step or fan-out' in r.registered['code_start'].spec.description
    run = r.registered['code_run'].spec
    assert run.permission_key({'cell': 'x', 'purpose': 'p'}) == 'code'
    assert run.input_schema['properties']['cell']['maxLength'] == 16384
    assert not run.mutates and run.concurrency == 'parallel'
    # Mutating tools attached anywhere make the next registration exclusive.
    await r.start(tools=['write'])
    again = {t.name: t for t in register(r.kernels, r.tools, r.config)}
    assert again['code_run'].spec.mutates and again['code_run'].spec.concurrency == 'exclusive'
    await r.aclose()
