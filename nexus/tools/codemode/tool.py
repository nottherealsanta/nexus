"""The code tool: a frozen catalog and transactional, auditable host callbacks.

Plan 03 sections 3.3, 3.7, 3.10 and 3.12 (contracts I1 to I4). One ``code`` call
runs one Monty program. Every host function it can reach goes through
``audited``, which emits ``code.call`` rows (running, then final) and keeps the
counts the result text and ``code.finished`` report. Nested catalog tools run
through ``ctx.invoke_tool`` so they pass the same hooks, permission engine and
dispatch as a direct call; nested ``tool.*`` events never exist. Store writes
commit as one ``code.store`` event only when the program succeeds. Context
targets are authorized up front through ``ctx.authorize_nested`` before the
program starts. Every event value is redacted and bounded.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from importlib import metadata, util
from typing import Any

from ...errors import OperationCancelled
from ...mcp.search import SearchIndex
from ...model.message import Image, Text
from ...session.context_authz import ContextDenied
from ...util import redact_secrets
from ..spec import RegisteredTool, ToolExecutionResult, ToolSpec
from .catalog import declarations, describe, render_catalog
from .runner import LIMITS, CodeToolError
from .store import Store

CHARS_PER_TOKEN = 4
MAX_RESULT_TOKENS = 25_000  # ToolSpec.max_result_tokens default
MAX_ARG_STRING = 2000
MAX_ARGS_CHARS = 16000
MAX_EVENT_RESULT_CHARS = 100_000
#: Once the events of one program carry this many result characters, later
#: results are clipped to ``LATE_RESULT_CHARS`` (announced via ``clipped``).
MAX_EVENT_TOTAL_CHARS = 2 * 1024 * 1024
LATE_RESULT_CHARS = 2000
MAX_IMAGES = 8
APPENDIX_ARGS_CHARS = 100
HELPERS = ('search_tools', 'describe_tool', 'store', 'load', 'store_delete')
IMAGE_REF = re.compile(r'\bimg-(\d+)\b')

HELPER_STUBS = '''
async def search_tools(query: str, limit: int = 8) -> list[dict[str, Any]]: ...
async def describe_tool(name: str) -> dict[str, Any] | None: ...
async def store(key: str, value: Any) -> None: ...
async def load(key: str) -> Any: ...
async def store_delete(key: str) -> None: ...
'''

INSTRUCTIONS = '''Run a short Python program in an isolated sandbox and get back one result. Use it to chain or fan out tool calls and filter their output without putting intermediate results in the conversation.
Rules of the Python subset (Monty):
- Every tool below is an async function taking keyword arguments: `await read(path="a.py")`. Use `import asyncio` and `await asyncio.gather(a(), b())` for independent calls.
- The last top-level expression is the result; `print()` output is returned separately. Top-level `await` works.
- No `match`, no generators or `yield`, no class inheritance, no filesystem, network, environment or installs. Importable: json, re, math, datetime, asyncio, collections, itertools, functools, dataclasses, typing, copy, base64, random, time. Not available: string, textwrap, hashlib, enum, decimal, operator.
- A failed, denied or invalid nested call raises ToolError with the same message a direct call would show: `try: ... except ToolError as e: ...`.
- Earlier tool calls are not undone if the program fails. store(key, value) / load(key) / store_delete(key) keep small JSON across code calls and commit only when the program succeeds.
- Nested calls follow normal permissions. Limits: nested calls, memory, time, 1 MiB printed output.
- search_tools(query) / describe_tool(name) find tools that are not listed.
Callable tools:
'''


def availability():
    try:
        version = metadata.version('pydantic-monty')
    except metadata.PackageNotFoundError:
        return {'available': False, 'reason': 'reinstall nexus-harness: pydantic-monty==1.1.0 is missing', 'version': None}
    spec = util.find_spec('pydantic_monty')
    return {'available': version == '1.1.0', 'version': version,
            'reason': '' if version == '1.1.0' else 'requires pydantic-monty==1.1.0',
            'package_path': spec.origin if spec else None}


# -- bounded, redacted event values --------------------------------------------


def _clip_value(value, depth=0):
    if depth > 6:
        return '<nested>'
    if isinstance(value, str):
        text = redact_secrets(value)
        if len(text) > MAX_ARG_STRING:
            return text[:MAX_ARG_STRING] + f'… [{len(text) - MAX_ARG_STRING} more chars]'
        return text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_clip_value(v, depth + 1) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k)[:128]: _clip_value(v, depth + 1) for k, v in list(value.items())[:50]}
    return redact_secrets(str(value))[:MAX_ARG_STRING]


def bound_args(args, kwargs):
    """JSON-safe, redacted, size-limited arguments for ``code.call``."""
    data = _clip_value(dict(kwargs))
    if args:
        data = {'args': _clip_value(list(args)), **data}
    if len(json.dumps(data, ensure_ascii=False, default=str)) > MAX_ARGS_CHARS:
        return {'_clipped': json.dumps(data, ensure_ascii=False, default=str)[:MAX_ARGS_CHARS]}
    return data


def _humanize(n):
    return f'{n / 1024:.1f} KiB' if n >= 1024 else f'{n} bytes'


def _lines(text):
    return 0 if not text else text.count('\n') + 1


def _compact_args(args):
    text = json.dumps(args, ensure_ascii=False, default=str)
    return text if len(text) <= APPENDIX_ARGS_CHARS else text[:APPENDIX_ARGS_CHARS] + '…'


def _kind(name, catalog):
    if name in catalog:
        return 'tool'
    if name.startswith('context_'):
        return 'context'
    if name == 'llm':
        return 'llm'
    if name in ('search_tools', 'describe_tool'):
        return 'search'
    return 'store'


@dataclass
class CallRecord:
    """One nested call, as shown in events and in the result appendix."""

    index: int
    name: str
    kind: str
    args: dict
    status: str = 'running'
    permission: dict | None = None
    duration_ms: int = 0
    text: str = ''
    size: int = 0
    clipped: bool = False
    extra: dict = field(default_factory=dict)

    def event(self, call_id):
        data = {'call_id': call_id, 'nested_id': f'{call_id}.{self.index}', 'index': self.index,
                'kind': self.kind, 'tool': self.name, 'args': self.args, 'status': self.status,
                'permission': self.permission, 'duration_ms': self.duration_ms}
        if self.status != 'running':
            data.update(result=self.text, bytes=self.size, clipped=self.clipped, **self.extra)
        return data


# -- nested results into the program -------------------------------------------


def _to_program(tool_name, result, images):
    """Plain JSON for the sandbox: structured data, else text; images by ref."""
    parts = []
    texts = []
    for block in result.content:
        if isinstance(block, Text):
            texts.append(block.text)
            continue
        if texts:
            parts.append(''.join(texts))
            texts = []
        if isinstance(block, Image):
            ref = f'img-{len(images) + 1}'
            images[ref] = block
            parts.append({'type': 'image', 'ref': ref})
        else:
            parts.append({'type': getattr(block, 'type', type(block).__name__.lower()), 'ref': None})
    if texts:
        parts.append(''.join(texts))
    text = ''.join(p for p in parts if isinstance(p, str))
    metrics = result.metrics or {}
    if isinstance(metrics.get('structured'), dict) and not any(isinstance(p, dict) for p in parts):
        return dict(metrics['structured'])
    if tool_name == 'bash' and isinstance(metrics.get('exit_code'), int):
        return {'exit_code': metrics['exit_code'], 'output': text}
    if not parts:
        return ''
    return parts[0] if len(parts) == 1 else parts


def _result_text(result):
    return ''.join(b.text for b in result.content if isinstance(b, Text))


def _find_refs(value, images, found):
    if isinstance(value, dict):
        if value.get('type') == 'image' and value.get('ref') in images:
            found.append(value['ref'])
        for item in value.values():
            _find_refs(item, images, found)
    elif isinstance(value, list):
        for item in value:
            _find_refs(item, images, found)
    elif isinstance(value, str) and value in images:
        found.append(value)


def register(runner, catalog, config, *, events=lambda session_id: (), context_session=None):
    # ``context_session`` is accepted for runtime compatibility; the context
    # provider is bound by session id (contract I2).
    del context_session
    stubs = declarations(catalog)
    mutates = any(tool.spec.mutates for tool in catalog.values())
    skipped = [{'tool': n, 'reason': r} for n, r in getattr(catalog, 'skipped', ())]
    max_calls = int(getattr(config, 'max_nested_calls', LIMITS['max_suspensions']))
    # The loop's per-parent ``max_nested_calls`` is the authoritative cap; Monty's
    # suspension count only needs headroom (a gather of n calls costs ~n+2).
    limits = {**LIMITS, 'max_suspensions': 2 * max_calls + 16}
    max_result_chars = MAX_RESULT_TOKENS * CHARS_PER_TOKEN
    search_index = None

    async def run(args, ctx):
        nonlocal search_index
        if ctx.invoke_tool is None:
            return ToolExecutionResult.text('code unavailable: nested dispatch is not bound', is_error=True)
        program = args['program']
        purpose = args['purpose']
        timeout = args.get('timeout_s', min(60, config.max_tool_seconds))
        if timeout < 1 or timeout > config.max_tool_seconds:
            return ToolExecutionResult.text('invalid code timeout', is_error=True)
        targets = list(dict.fromkeys(args.get('targets') or ['self']))
        started = time.monotonic()
        call_id = ctx.call_id
        records: list[CallRecord] = []
        images: dict[str, Image] = {}
        counters = {'event_chars': 0, 'sub_calls': 0, 'sub_tokens': 0}

        async def emit(kind, data):
            if ctx.emit:
                outcome = ctx.emit(kind, data)
                if hasattr(outcome, '__await__'):
                    await outcome

        provider = runner.context_provider if getattr(config, 'context', False) else None
        started_event = {
            'call_id': call_id, 'purpose': redact_secrets(purpose)[:512],
            'program_digest': hashlib.sha256(program.encode()).hexdigest(),
            'timeout_s': timeout, 'exposure': config.expose if isinstance(config.expose, str) else list(config.expose),
            'callable': list(catalog), 'targets': [], 'monty_version': availability().get('version'),
            'limits': limits, 'skipped': skipped}

        def finished(status, *, limit=None, diagnostic=None):
            ok = sum(1 for r in records if r.status == 'ok')
            denied = sum(1 for r in records if r.status == 'denied')
            return {'call_id': call_id, 'status': status, 'limit': limit, 'diagnostic': diagnostic,
                    'calls': len(records), 'ok': ok, 'errors': sum(1 for r in records if r.status in ('error', 'cancelled')),
                    'denied': denied, 'sub_calls': counters['sub_calls'], 'sub_tokens': counters['sub_tokens'],
                    'wall_ms': int((time.monotonic() - started) * 1000)}

        # -- context targets: resolve, check, ask, all before anything runs ----
        approved: dict[str, str] = {}
        infos: list[dict] = []
        denial = None
        if provider is None:
            if any(t != 'self' for t in targets):
                denial = 'context namespace unavailable: enable [code_mode] context'
        else:
            for ref in targets:
                try:
                    info = provider.resolve(ctx.session_id, ref)
                    provider.check(ctx.session_id, info['stored_id'])
                except ContextDenied as exc:
                    denial = redact_secrets(str(exc))
                    break
                infos.append(info)
                approved[ref] = info['stored_id']
                if ref == 'self':
                    continue
                if ctx.authorize_nested is None:
                    denial = f'context access to {ref} needs approval but none is available'
                    break
                verdict = await ctx.authorize_nested(
                    f'ContextQuery({info.get("name") or ref})', parent_call_id=call_id,
                    summary={'purpose': redact_secrets(purpose)[:512], 'target': ref,
                             'name': info.get('name'), 'logical_id': info.get('logical_id'),
                             'program': redact_secrets(program)[:4000]})
                if not verdict.allowed:
                    denial = verdict.message or f'context access to {ref} was denied'
                    break
        started_event['targets'] = [{'ref': i['ref'], 'name': i.get('name'), 'logical_id': i.get('logical_id')}
                                    for i in infos]
        await emit('code.started', started_event)
        if denial is not None:
            await emit('code.finished', finished('denied', diagnostic={'kind': 'ContextDenied', 'message': denial, 'line': None}))
            return ToolExecutionResult.text(f'Program not run: {denial}', is_error=True,
                                            context_note=f'code: {purpose} → denied, 0 calls')

        store = Store(events(ctx.session_id))

        # -- audited host functions -------------------------------------------
        async def tool_call(name, tool, kwargs, record):
            nested_id = f'{call_id}.{record.index}'
            outcome = await ctx.invoke_tool(tool.spec.name, kwargs, parent_call_id=call_id, nested_id=nested_id)
            record.permission = outcome.permission
            text = _result_text(outcome.result)
            if outcome.status != 'ok':
                record.status = 'denied' if outcome.status == 'denied' else (
                    'cancelled' if outcome.status == 'cancelled' else 'error')
                record.text = text
                raise CodeToolError(text or f'{name} failed')
            record.status = 'ok'
            record.text = text
            return _to_program(tool.spec.name, outcome.result, images)

        def audited(name, impl):
            kind = _kind(name, catalog)

            async def call(*pos, **kw):
                record = CallRecord(len(records) + 1, name, kind, bound_args(pos, kw))
                records.append(record)
                await emit('code.call', record.event(call_id))
                before = time.monotonic()
                try:
                    if kind == 'tool':
                        if pos:
                            raise CodeToolError(f'{name} takes keyword arguments only')
                        value = await tool_call(name, catalog[name], kw, record)
                    else:
                        value = impl(*pos, **kw)
                        if asyncio.iscoroutine(value) or hasattr(value, '__await__'):
                            value = await value
                        record.status = 'ok'
                        record.text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
                        if kind == 'llm' and isinstance(value, dict):
                            record.extra = {'model': value.get('model'),
                                            'input_tokens': value.get('input_tokens'),
                                            'output_tokens': value.get('output_tokens')}
                            counters['sub_calls'] += 1
                            counters['sub_tokens'] += int(value.get('input_tokens') or 0) + int(value.get('output_tokens') or 0)
                    return value
                except (asyncio.CancelledError, OperationCancelled):
                    record.status = 'cancelled'
                    raise
                except ContextDenied as exc:
                    record.status, record.text = 'denied', str(exc)
                    raise CodeToolError(str(exc)) from None
                except CodeToolError as exc:
                    if record.status == 'running':
                        record.status, record.text = 'error', str(exc)
                    raise
                except BaseException:
                    record.status, record.text = 'error', 'host call failed'
                    raise
                finally:
                    record.duration_ms = int((time.monotonic() - before) * 1000)
                    text = redact_secrets(record.text)
                    record.size = len(record.text.encode())
                    limit = (LATE_RESULT_CHARS if counters['event_chars'] > MAX_EVENT_TOTAL_CHARS
                             else MAX_EVENT_RESULT_CHARS)
                    record.clipped = len(text) > limit
                    if record.clipped:
                        text = text[:limit] + f'\n[clipped: showing {limit} of {record.size} bytes]'
                    record.text = text
                    counters['event_chars'] += len(text)
                    await emit('code.call', record.event(call_id))
            return call

        def search(query, limit=8):
            nonlocal search_index
            if search_index is None:
                from types import SimpleNamespace  # lightweight stand-in for a server descriptor
                tools = [SimpleNamespace(name=n, local_name=n, spec=t.spec) for n, t in catalog.items()]
                search_index = SearchIndex({'builtin': SimpleNamespace(tools=tools)})
            ranked = search_index.search(str(query), limit=max(1, min(int(limit), 10)))
            return [{'name': m.tool.local_name, 'description': m.tool.spec.description[:500]}
                    for m in ranked.matches]

        def store_write(key, value):
            try:
                store.store(key, value)
            except ValueError as exc:
                raise CodeToolError(str(exc)) from None

        def store_read(key):
            try:
                return store.load(key)
            except ValueError as exc:
                raise CodeToolError(str(exc)) from None

        def store_remove(key):
            try:
                store.delete(key)
            except ValueError as exc:
                raise CodeToolError(str(exc)) from None

        impls: dict[str, Any] = {name: None for name in catalog}
        impls.update({'describe_tool': lambda name: describe(catalog, name), 'search_tools': search,
                      'store': store_write, 'load': store_read, 'store_delete': store_remove})
        program_stubs = stubs + '\n' + HELPER_STUBS
        inputs = {}
        if provider is not None:
            impls.update(provider.bind(ctx.session_id, approved, depth=0))
            program_stubs += '\n' + provider.stubs()
            inputs['targets'] = list(approved)
        functions = {name: audited(name, impl) for name, impl in impls.items()}

        # -- run ------------------------------------------------------------------
        outcome = None
        status, limit, diagnostic = 'cancelled', None, None
        try:
            try:
                outcome = await runner.run(program, functions=functions, inputs=inputs,
                                           stubs=program_stubs, limits=limits,
                                           deadline=started + timeout, cancel=ctx.cancel_token)
            except ValueError as exc:
                outcome = {'kind': 'failed', 'value': None, 'output': '',
                           'diagnostic': {'kind': 'SyntaxError', 'message': str(exc), 'line': None, 'traceback': ''}}
            status, limit, diagnostic = _status(outcome)
            if status == 'ok' and store.dirty():
                await emit('code.store', {**store.event(call_id)})
        finally:
            # Also on CancelledError: record the end, then let it propagate.
            await emit('code.finished', finished(status, limit=limit, diagnostic=_diag_event(diagnostic)))
        if status == 'cancelled':
            raise OperationCancelled('code program cancelled')
        return _render(outcome, status, diagnostic, records, images, counters, purpose,
                       time.monotonic() - started, max_result_chars)

    description = INSTRUCTIONS + render_catalog(
        catalog, budget_tokens=int(getattr(config, 'inline_budget_tokens', 2500)),
        pinned=tuple(getattr(config, 'pinned', ()) or ()))
    description += '\n\nAlso: ' + ', '.join(f'{n}()' for n in HELPERS) + '.'
    if skipped:
        description += '\nNot callable (skipped): ' + '; '.join(f'{s["tool"]} ({s["reason"]})' for s in skipped[:10])
    return RegisteredTool(ToolSpec(
        name='code', bundle='code', description=description,
        input_schema={'type': 'object', 'properties': {
            'program': {'type': 'string', 'maxLength': 16384},
            'purpose': {'type': 'string', 'maxLength': 512},
            'timeout_s': {'type': 'integer', 'minimum': 1, 'maximum': config.max_tool_seconds},
            'targets': {'type': 'array', 'maxItems': 50, 'items': {'type': 'string'}}},
            'required': ['program', 'purpose'], 'additionalProperties': False},
        mutates=mutates, concurrency='exclusive' if mutates else 'parallel',
        permission_key=lambda args: 'code', timeout_s=config.max_tool_seconds,
    ), run)


# -- status and result text ---------------------------------------------------------


def _status(outcome):
    """(code.finished status, limit, diagnostic) for a runner outcome."""
    if outcome['kind'] == 'done':
        return 'ok', None, None
    diagnostic = outcome['diagnostic']
    kind = diagnostic['kind']
    if kind.startswith('limit:'):
        return 'limit', kind[6:], diagnostic
    if kind == 'timeout':
        return 'limit', 'time', diagnostic
    # The loop's per-program nested-call cap surfaces as a ToolError text.
    if kind == 'ToolError' and 'limit: calls (at most' in diagnostic.get('message', ''):
        return 'limit', 'calls', diagnostic
    if kind == 'cancelled':
        return 'cancelled', None, diagnostic
    if kind == 'crashed':
        return 'crashed', None, diagnostic
    return 'error', None, diagnostic


def _diag_event(diagnostic):
    if not diagnostic:
        return None
    return {'kind': diagnostic['kind'], 'message': redact_secrets(diagnostic['message'])[:2000],
            'line': diagnostic.get('line')}


def _clip(text, n, label):
    if len(text) <= n:
        return text
    return text[:max(0, n)] + f'\n[{label} clipped: showing {max(0, n)} of {len(text)} chars]'


def _appendix(records):
    lines = ['Nested calls:']
    for r in records:
        full = r.text
        lead = f'{r.index}. {r.name} {_compact_args(r.args)} → '
        if r.status == 'ok':
            size = f', {_lines(full)} lines' if full else ', empty'
            if r.size >= 1024:
                size += f' ({_humanize(r.size)})'
            if r.kind == 'llm' and r.extra:
                size += f', {r.extra.get("input_tokens")} in / {r.extra.get("output_tokens")} out tokens'
            lines.append(f'{lead}ok{size}')
        elif r.status == 'running':
            lines.append(f'{lead}unfinished')
        else:
            reason = (full.strip().splitlines() or [''])[0][:160]
            lines.append(f'{lead}{r.status}' + (f': {reason}' if reason else ''))
    return '\n'.join(lines)


def _render(outcome, status, diagnostic, records, images, counters, purpose, elapsed, max_chars):
    ok = sum(1 for r in records if r.status == 'ok')
    parts = [f'{n} {label}' for n, label in (
        (ok, 'ok'), (sum(1 for r in records if r.status == 'error'), 'error'),
        (sum(1 for r in records if r.status == 'denied'), 'denied'),
        (sum(1 for r in records if r.status == 'cancelled'), 'cancelled')) if n]
    count = f'{len(records)} nested call{"" if len(records) == 1 else "s"}'
    if parts:
        count += f' ({", ".join(parts)})'
    header = f'Program {"completed" if status == "ok" else "failed"} in {elapsed:.2f} s · {count}'
    if counters['sub_calls']:
        header += f' · {counters["sub_calls"]} sub-call{"" if counters["sub_calls"] == 1 else "s"}'
    value = outcome.get('value')
    output = outcome.get('output') or ''
    if status == 'ok':
        value_text = value if isinstance(value, str) else (
            '(no final value)' if value is None else json.dumps(value, ensure_ascii=False, indent=2))
        lead = []
    else:
        where = f' at line {diagnostic["line"]}' if diagnostic.get('line') else ''
        lead = [f'{diagnostic["kind"]}{where}: {diagnostic["message"]}']
        if diagnostic.get('traceback'):
            lead.append(diagnostic['traceback'])
        value_text = ''
    appendix = _appendix(records) if records else ''
    fixed = len(header) + len(appendix) + sum(len(x) for x in lead) + 200
    room = max(1000, max_chars - fixed)
    if len(value_text) + len(output) > room:
        half = room // 2
        v_room = max(half, room - len(output)) if len(output) < half else half
        o_room = max(half, room - len(value_text)) if len(value_text) < half else half
        value_text = _clip(value_text, v_room, 'result')
        output = _clip(output, o_room, 'printed output')
    blocks = [header] + lead
    if value_text:
        blocks.append(value_text)
    if output:
        blocks.append('Printed output:\n' + output.rstrip('\n'))
    if appendix:
        blocks.append(appendix)
    found: list[str] = []
    _find_refs(value, images, found)
    found += [f'img-{m}' for m in IMAGE_REF.findall(output) if f'img-{m}' in images]
    attached = [images[ref] for ref in dict.fromkeys(found)][:MAX_IMAGES]
    if attached:
        blocks.append(f'{len(attached)} image(s) attached.')
    body = '\n\n'.join(blocks)
    note_status = 'ok' if status == 'ok' else (diagnostic['kind'] if diagnostic else status)
    return ToolExecutionResult(
        content=[Text(text=body), *attached], is_error=status != 'ok', display=body,
        context_note=f'code: {purpose} → {note_status}, {len(records)} calls',
        metrics={'nested_calls': len(records), 'status': status})
