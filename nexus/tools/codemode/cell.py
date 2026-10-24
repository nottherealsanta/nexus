"""Running one cell in a kernel (plan code-mode-kernel §3.5, §3.6, §3.7; plan 03 §3.5, §3.10).

``run_cell`` is the whole life of a ``code_run`` call: size check, ``In [n]``,
``code.cell_started``, the per-cell callable set (the *current* iteration's catalog
intersected with the kernel's attachments, so a tool disabled since ``code_start``
is simply absent and its name maps to ``NameError: name 'x' is not callable now
(reason)``), the feed through the registry with a deadline, ``code.cell_finished``,
and the rendered result. Every host function a cell can reach goes through
``audited``, which emits ``code.call`` rows (running, then final; ``parent_call_id``
is the cell's call id) and keeps the counts the result reports. Nested catalog tools
run through ``ctx.invoke_tool`` so they pass the same hooks, permission engine and
dispatch as a direct call; nested ``tool.*`` events never exist. Context namespace
functions are bound per cell from the targets approved at start/attach. Every event
value is redacted and bounded. Timeouts, cancels and lost workers discard the kernel's
worker (the host cannot keep a half-run cell): the result says so and the kernel is
reset empty with its attachments kept, unless a checkpoint exists: the next call restores it
(plan code-mode-kernel §3.8). Before the cell, ``kernels.prepare`` restores a kernel that is not
live and the result begins with its notice; after a successful cell ``kernels.after_cell``
checkpoints and ``code.kernel_checkpointed`` follows ``code.cell_finished``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import pprint
import re
import time
from dataclasses import dataclass, field
from importlib import metadata

from ...errors import OperationCancelled
from ...model.message import Image, Text
from ...session.context_authz import ContextDenied
from ...util import redact_secrets
from ..spec import ToolExecutionResult
from .argcheck import check_cell
from .argcheck import describe as describe_problems
from .catalog import describe
from .kernel_host import HIDDEN_NAMES, HostLost, KernelBusy, KernelLost
from .kernels import KernelState, missing_reason
from .runner import MAX_PROGRAM_BYTES, CodeToolError

MAX_ARG_STRING = 2000
MAX_ARGS_CHARS = 16000
MAX_EVENT_RESULT_CHARS = 100_000
#: Once the events of one cell carry this many result characters, later results are
#: clipped to ``LATE_RESULT_CHARS`` (announced via ``clipped``).
MAX_EVENT_TOTAL_CHARS = 2 * 1024 * 1024
LATE_RESULT_CHARS = 2000
MAX_IMAGES = 8
APPENDIX_ARGS_CHARS = 100
MAX_CALL_LINES = 50
MAX_SOURCE_EVENT_CHARS = 16384
IMAGE_REF = re.compile(r'\bimg-(\d+)\b')
WHOS_CALL = re.compile(r'(?<![\w.])whos\(\s*\)')
NAME_ERROR = re.compile(r"(?:name '(\w+)' is not defined|Name `(\w+)` used when not defined)")
BACKGROUND_REFUSAL = ('background shell jobs are not available inside code: the cell ends with the call. '
                      'Run the command in the foreground, or start it with a direct shell call with background=true.')


def monty_version():
    try:
        return metadata.version('pydantic-monty')
    except metadata.PackageNotFoundError:
        return None


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


def _lines(text):
    return 0 if not text else text.count('\n') + 1


def _size(n):
    if n >= 1024 * 1024:
        return f'{n / 1024 / 1024:.1f} MB'
    return f'{n / 1024:.0f} KB' if n >= 1024 else f'{n} B'


def _compact_args(args):
    text = ', '.join(f'{k}={v!r}' for k, v in args.items()) if args else ''
    return text if len(text) <= APPENDIX_ARGS_CHARS else text[:APPENDIX_ARGS_CHARS] + '…'


@dataclass
class CallRecord:
    """One nested call, as shown in events and in the result's call list."""

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
        data = {'call_id': call_id, 'parent_call_id': call_id, 'nested_id': f'{call_id}.{self.index}',
                'index': self.index, 'kind': self.kind, 'tool': self.name, 'args': self.args,
                'status': self.status, 'permission': self.permission, 'duration_ms': self.duration_ms}
        if self.status != 'running':
            data.update(result=self.text, bytes=self.size, clipped=self.clipped, **self.extra)
        return data


# -- nested results into the cell -----------------------------------------------


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
    if tool_name in ('shell', 'bash') and isinstance(metrics.get('exit_code'), int):
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


# -- status and diagnostics ---------------------------------------------------------


def _status(outcome):
    """(code.cell_finished status, limit, diagnostic) for a host outcome."""
    if outcome['kind'] == 'done':
        return 'ok', None, None
    diagnostic = outcome['diagnostic']
    kind = diagnostic['kind']
    if kind.startswith('limit:'):
        return 'limit', kind[6:], diagnostic
    if kind == 'timeout':
        return 'limit', 'time', diagnostic
    # The loop's per-cell nested-call cap surfaces as a ToolError text.
    if kind == 'ToolError' and 'limit: calls (at most' in diagnostic.get('message', ''):
        return 'limit', 'calls', diagnostic
    if kind == 'cancelled':
        return 'cancelled', None, diagnostic
    if kind in ('crashed', 'KernelLost'):
        return 'crashed', None, diagnostic
    return 'error', None, diagnostic


def _diag_event(diagnostic):
    if not diagnostic:
        return None
    return {'kind': diagnostic['kind'], 'message': redact_secrets(diagnostic['message'])[:2000],
            'line': diagnostic.get('line')}


def failure(kind, message, output=''):
    return {'kind': 'failed', 'value': None, 'output': output,
            'diagnostic': {'kind': kind, 'message': message, 'line': None, 'traceback': ''}}


def map_name_error(diagnostic, reasons):
    """``NameError: name 'x' is not callable now (reason)`` for a known tool name."""
    if not diagnostic or diagnostic.get('kind') != 'NameError':
        return diagnostic
    match = NAME_ERROR.search(diagnostic.get('message', ''))
    name = (match.group(1) or match.group(2)) if match else None
    reason = reasons().get(name) if name else None
    if not reason:
        return diagnostic
    message = f"name '{name}' is not callable now ({reason})"
    return {**diagnostic, 'message': message, 'traceback': re.sub(
        r"NameError: .*$", f'NameError: {message}', diagnostic.get('traceback', ''), flags=re.M)}


# -- value rendering ---------------------------------------------------------------


def describe_value(value):
    """``(description, text, raw)`` for a cell value; ``None`` means no ``Out`` line.

    The value arrives as JSON (a tuple or set was already a list). ``raw`` means the
    text is a multi-line string shown verbatim under the ``Out`` header, not its repr.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if '\n' in value:
            return f'(str, {len(value)} chars, {_lines(value)} lines)', value, True
        return (f'(str, {len(value)} chars)' if len(value) > 80 else ''), repr(value), False
    if isinstance(value, (bool, int, float)):
        return '', repr(value), False
    text = pprint.pformat(value, width=100, sort_dicts=False, compact=True)
    if isinstance(value, dict):
        desc = f'(dict, {len(value)} key{"" if len(value) == 1 else "s"})'
    elif isinstance(value, list):
        desc = f'(list, {len(value)} item{"" if len(value) == 1 else "s"})'
    else:
        desc = f'({type(value).__name__})'
    return desc, text, False


def _hint(source):
    last = next((line.strip() for line in reversed(source.splitlines()) if line.strip()), '')
    if re.fullmatch(r'[A-Za-z_]\w*', last):
        return f'try {last}[:5] or len({last})'
    return 'assign it to a variable, then run a cell with a slice such as x[:5] or len(x)'


def _clip(text, n, label, hint=''):
    if len(text) <= n:
        return text, False
    note = f'[{label} clipped: showing {max(0, n)} of {len(text)} chars' + (f'; {hint}' if hint else '') + ']'
    return text[:max(0, n)] + '\n' + note, True


def _call_lines(records):
    lines = []
    for r in records[:MAX_CALL_LINES]:
        full = r.text
        lead = f'  {r.index} {r.name}({_compact_args(r.args)}) '
        if r.status == 'ok':
            detail = _size(r.size) if r.size else 'empty'
            if r.kind == 'llm' and r.extra:
                detail += f', {r.extra.get("input_tokens")} in / {r.extra.get("output_tokens")} out tokens'
            lines.append(f'{lead}✓ {detail}')
        elif r.status == 'running':
            lines.append(f'{lead}… unfinished')
        else:
            glyph = '⊘' if r.status == 'denied' else ('–' if r.status == 'cancelled' else '✗')
            reason = (full.strip().splitlines() or [''])[0][:160]
            lines.append(f'{lead}{glyph} {r.status}' + (f': {reason}' if reason else ''))
    if len(records) > MAX_CALL_LINES:
        lines.append(f'  … {len(records) - MAX_CALL_LINES} more calls (all are in the session log)')
    return lines


def render_cell(*, n, outcome, status, diagnostic, records, images, counters, purpose, elapsed,
                max_chars, source, notice='', restored=''):
    ok = sum(1 for r in records if r.status == 'ok')
    glyph = '✓' if status == 'ok' else '✗'
    label = '' if status == 'ok' else f'{diagnostic["kind"] if diagnostic else status} '
    count = f'{len(records)} call{"" if len(records) == 1 else "s"}'
    head = f'In [{n}]  {glyph} {label}{elapsed:.2f} s · {count}'
    if counters['sub_calls']:
        head += f' · {counters["sub_calls"]} sub-call{"" if counters["sub_calls"] == 1 else "s"}'
    head += f' · purpose: {purpose}' if purpose else ''
    value = outcome.get('value')
    output = outcome.get('output') or ''
    lead = []
    out_label = out_text = out_desc = ''
    raw = False
    if status == 'ok':
        described = describe_value(value)
        if described is not None:
            out_desc, out_text, raw = described
    else:
        where = f' at line {diagnostic["line"]}' if diagnostic.get('line') else ''
        lead = [f'{diagnostic["kind"]}{where}: {diagnostic["message"]}']
        if diagnostic.get('traceback') and diagnostic['traceback'] not in lead[0]:
            lead.append(diagnostic['traceback'])
    calls = _call_lines(records)
    fixed = len(head) + len(notice) + len(restored) + sum(len(x) for x in lead) + sum(len(x) + 1 for x in calls) + 200
    room = max(1000, max_chars - fixed)
    stdout_text = output.rstrip('\n')
    clipped = False
    if len(out_text) + len(stdout_text) > room:
        half = room // 2
        o_room = max(half, room - len(stdout_text)) if len(stdout_text) < half else half
        s_room = max(half, room - len(out_text)) if len(out_text) < half else half
        out_text, c1 = _clip(out_text, o_room, f'Out [{n}]', _hint(source))
        stdout_text, c2 = _clip(stdout_text, s_room, 'stdout')
        clipped = c1 or c2
    blocks = [restored, head] if restored else [head]
    if notice:
        blocks.append(notice)
    blocks.extend(lead)
    if stdout_text:
        blocks.append('stdout:\n' + '\n'.join('  ' + line for line in stdout_text.split('\n')))
    if out_text:
        out_label = f'Out [{n}]'
        if raw or '\n' in out_text:
            blocks.append(f'{out_label}: {out_desc}\n{out_text}' if out_desc else f'{out_label}:\n{out_text}')
        else:
            blocks.append(f'{out_label}: {out_text}' + (f'    {out_desc}' if out_desc else ''))
    if calls:
        blocks.append('calls:\n' + '\n'.join(calls))
    found: list[str] = []
    _find_refs(value, images, found)
    found += [f'img-{m}' for m in IMAGE_REF.findall(output) if f'img-{m}' in images]
    attached = [images[ref] for ref in dict.fromkeys(found)][:MAX_IMAGES]
    if attached:
        blocks.append(f'{len(attached)} image(s) attached.')
    body = '\n\n'.join(blocks)
    note_status = 'ok' if status == 'ok' else (diagnostic['kind'] if diagnostic else status)
    summary = (out_desc or out_text[:200]) if out_text else ''
    result = ToolExecutionResult(
        content=[Text(text=body), *attached], is_error=status != 'ok', display=body,
        context_note=f'code_run In [{n}]: {purpose} → {note_status}, {len(records)} calls',
        metrics={'nested_calls': len(records), 'status': status, 'cell': n})
    return result, {'stdout_bytes': len(output.encode()), 'out_summary': redact_secrets(summary)[:300],
                    'clipped': clipped, 'ok': ok}


# -- the cell ----------------------------------------------------------------------


def _text_result(text, *, purpose='', note=None):
    return ToolExecutionResult.text(text, is_error=True, context_note=note or (
        f'code_run: {purpose} → not run' if purpose else 'code_run: not run'))


async def run_cell(*, kernels, state: KernelState, full, tools, config, ctx, source: str, purpose: str,
                   timeout: float, search) -> ToolExecutionResult:
    """Run ``source`` as the next cell of ``state``'s kernel and return the rendered result."""
    if ctx.invoke_tool is None:
        return _text_result('code_run unavailable: nested dispatch is not bound')
    if len(source.encode()) > MAX_PROGRAM_BYTES:
        return _text_result(f'Cell not run: it is {len(source.encode())} bytes; the limit is {MAX_PROGRAM_BYTES} '
                            '(16 KiB). Split it into smaller cells and keep state in variables.', purpose=purpose)
    if re.search(r'^\s*#\s*///\s*script\b', source, re.M):
        return _text_result('Cell not run: inline dependency scripts are forbidden.', purpose=purpose)
    max_cells = int(getattr(config, 'max_cells', 500))
    if state.n >= max_cells:
        return _text_result(f'Cell not run: this kernel has run its {max_cells} cells (max_cells). '
                            'Call code_reset to start a fresh numbering; variables are cleared.', purpose=purpose)
    call_id = ctx.call_id
    sid = state.session_id
    registry = kernels.registry

    async def emit(kind, data):
        if ctx.emit:
            outcome = ctx.emit(kind, data)
            if hasattr(outcome, '__await__'):
                await outcome

    prepared = await kernels.prepare(state, emit, call_id)
    if prepared is not None and prepared.lost:
        return _text_result(prepared.notice + ' This cell was not run; rebuild the state you need and run it '
                            'again.', purpose=purpose, note='code_run: KernelLost, 0 calls')
    restored = prepared.notice if prepared is not None else ''
    if prepared is None and registry.status(sid, state.agent_path) == 'lost':
        await kernels.restart(state)
        await emit('code.kernel_reset', {'kernel_id': state.kernel_id, 'call_id': call_id,
                                         'stopped': False, 'reason': 'lost'})
        return _text_result(
            'KernelLost: the kernel worker was lost earlier (timeout, cancel or crash); its variables are gone. '
            'The kernel was reset empty: In [n] restarts at 1 and attachments are kept. This cell was not run; '
            'rebuild the state you need and run it again.', purpose=purpose,
            note='code_run: KernelLost, 0 calls')

    state.n += 1
    n = state.n
    started = time.monotonic()
    records: list[CallRecord] = []
    images: dict[str, Image] = {}
    counters = {'event_chars': 0, 'sub_calls': 0, 'sub_tokens': 0}
    provider = kernels.context_provider if getattr(config, 'context', False) else None
    callable_now = {name: full[name] for name in state.attached if name in full}
    await emit('code.cell_started', {
        'kernel_id': state.kernel_id, 'call_id': call_id, 'n': n,
        'source': redact_secrets(source)[:MAX_SOURCE_EVENT_CHARS], 'purpose': redact_secrets(purpose)[:512],
        'source_digest': _digest(source), 'timeout_s': timeout, 'callable': list(callable_now),
        'monty_version': monty_version()})

    def finished(status, *, limit=None, diagnostic=None, extra=None):
        errors = sum(1 for r in records if r.status in ('error', 'cancelled'))
        return {'kernel_id': state.kernel_id, 'call_id': call_id, 'n': n, 'status': status, 'limit': limit,
                'diagnostic': diagnostic, 'calls': len(records),
                'ok': sum(1 for r in records if r.status == 'ok'), 'errors': errors,
                'denied': sum(1 for r in records if r.status == 'denied'),
                'sub_calls': counters['sub_calls'], 'sub_tokens': counters['sub_tokens'],
                'wall_ms': int((time.monotonic() - started) * 1000),
                **{'stdout_bytes': 0, 'out_summary': '', 'clipped': False, **(extra or {})}}

    # -- audited host functions -----------------------------------------------
    async def tool_call(name, tool, kwargs, record):
        if tool.spec.name in ('shell', 'bash') and (kwargs.get('background') or kwargs.get('run_in_background')):
            # A cell ends with its call; a background job's exit would reach the
            # model with no cell left to use it.
            raise CodeToolError(BACKGROUND_REFUSAL)
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

    def audited(name, impl, kind):
        async def call(*pos, **kw):
            record = CallRecord(len(records) + 1, name, kind, bound_args(pos, kw))
            records.append(record)
            await emit('code.call', record.event(call_id))
            before = time.monotonic()
            try:
                if kind == 'tool':
                    if pos:
                        raise CodeToolError(f'{name} takes keyword arguments only')
                    value = await tool_call(name, callable_now[name], kw, record)
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

    def search_impl(query, limit=8):
        names = search.search(str(query), limit)
        return [{'name': n, 'description': full[n].spec.description[:500], 'attached': n in state.attached}
                for n in names if n in full]

    def describe_impl(name):
        found = describe(full, name)
        return None if found is None else {**found, 'attached': name in state.attached}

    functions = {name: audited(name, None, 'tool') for name in callable_now}
    functions['search_tools'] = audited('search_tools', search_impl, 'search')
    functions['describe_tool'] = audited('describe_tool', describe_impl, 'search')
    inputs = {}
    if provider is not None and state.approved:
        for name, impl in provider.bind(sid, state.approved, depth=0).items():
            functions[name] = audited(name, impl, 'llm' if name == 'llm' else 'context')
        inputs['targets'] = list(state.approved)

    def reasons():
        found = {}
        for name, spec_name in state.attached.items():
            if name not in full:
                found[name] = missing_reason(spec_name, tools, full, attached=True)
        for name in full:
            if name not in state.attached:
                found[name] = f'not attached; attach it with code_attach(tools=["{name}"])'
        return found

    program = WHOS_CALL.sub('whos(locals())', source)
    problems = check_cell(source, {name: tool.spec.input_schema for name, tool in callable_now.items()})

    # -- run --------------------------------------------------------------------
    outcome = None
    status, limit, diagnostic = 'cancelled', None, None
    flags = {'lost': False, 'cancelled': False}
    result = extra = None

    async def fed():
        """One feed raced against the cancel token; failures become outcomes."""
        feed = asyncio.ensure_future(registry.feed(
            sid, state.agent_path, program, functions=functions, inputs=inputs or None,
            deadline=started + timeout))
        watcher = (asyncio.ensure_future(ctx.cancel_token.wait())
                   if ctx.cancel_token is not None else None)
        try:
            await asyncio.wait({feed, *([watcher] if watcher else [])}, return_when=asyncio.FIRST_COMPLETED)
            if not feed.done():
                flags['cancelled'] = True
                feed.cancel()
                await asyncio.gather(feed, return_exceptions=True)
                return None
            try:
                return feed.result()
            except ValueError as exc:
                return failure('SyntaxError', str(exc))
            except TimeoutError:
                flags['lost'] = True
                return failure('timeout', f'cell exceeded its deadline of {timeout} s; the kernel worker '
                                          'was discarded and its variables are gone')
            except (KernelLost, HostLost) as exc:
                flags['lost'] = True
                return failure('KernelLost', redact_secrets(str(exc))[:500] +
                               '; the kernel worker was lost and its variables are gone')
            except KernelBusy as exc:
                return failure('KernelBusy', redact_secrets(str(exc))[:500])
        finally:
            if watcher:
                watcher.cancel()
            if not feed.done():
                feed.cancel()
                await asyncio.gather(feed, return_exceptions=True)

    try:
        if problems:
            # Certain mistakes in a tool call are reported before any host call is made.
            outcome = {'kind': 'failed', 'value': None, 'output': '', 'diagnostic': describe_problems(problems)}
        else:
            try:
                await asyncio.wait_for(kernels.feeds.acquire(), 5)
            except TimeoutError:
                outcome = failure('RuntimeError', 'code mode busy, retry')
        if outcome is None:
            try:
                outcome = await fed()
            finally:
                kernels.feeds.release()
        cancelled, lost = flags['cancelled'], flags['lost']
        if cancelled:
            outcome = failure('cancelled', 'cell cancelled')
        diagnostic = outcome.get('diagnostic')
        if diagnostic and diagnostic['kind'] == 'output_limit':
            diagnostic = {**diagnostic, 'kind': 'limit:output'}
            outcome = {**outcome, 'diagnostic': diagnostic}
        diagnostic = map_name_error(diagnostic, reasons)
        if diagnostic is not outcome.get('diagnostic'):
            outcome = {**outcome, 'diagnostic': diagnostic}
        status, limit, diagnostic = _status(outcome)
        if status != 'cancelled':
            notice = ''
            if lost and state.checkpoint_after is not None and kernels.store is not None:
                # The next call restores from the checkpoint (the lost cell is in missing_cells).
                notice = (f'Kernel worker discarded. The next call restores the kernel from its checkpoint '
                          f'after In [{state.checkpoint_after}]; variables set since are gone.')
            elif lost:
                await kernels.restart(state)
                await emit('code.kernel_reset', {'kernel_id': state.kernel_id, 'call_id': call_id,
                                                 'stopped': False, 'reason': 'lost'})
                notice = ('Kernel reset: its worker was discarded, so all variables are gone and In [n] '
                          'restarts at 1. Attachments are kept.')
            result, extra = render_cell(
                n=n, outcome=outcome, status=status, diagnostic=diagnostic, records=records, images=images,
                counters=counters, purpose=purpose, elapsed=time.monotonic() - started,
                max_chars=int(getattr(config, 'max_output_chars', 8000)), source=source, notice=notice, restored=restored)
    finally:
        # Also on CancelledError: record the end, then let it propagate.
        await emit('code.cell_finished', finished(status, limit=limit, diagnostic=_diag_event(diagnostic),
                                                  extra=extra))
    if status == 'ok' and kernels.after_cell is not None:
        with contextlib.suppress(Exception):
            payload = await kernels.after_cell(state, n)
            if isinstance(payload, dict):
                await emit('code.kernel_checkpointed', {**payload, 'call_id': call_id})
    state.last_cell = time.time()
    if cancelled or status == 'cancelled':
        raise OperationCancelled('code cell cancelled')
    return result


def _digest(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def variables(rows):
    """``whos`` rows without the kernel's own names."""
    return [tuple(r) for r in rows if r and r[0] not in HIDDEN_NAMES]


async def read_variables(kernels, state: KernelState):
    """The kernel's ``whos`` table (names, types, sizes; no values), or ``None`` when unavailable."""
    try:
        outcome = await kernels.registry.feed(
            state.session_id, state.agent_path, '_nx_vars(locals())', functions={})
    except Exception:  # noqa: BLE001 - a missing table must not fail a re-describe
        return None
    if outcome.get('kind') != 'done' or not isinstance(outcome.get('value'), list):
        return None
    return variables(outcome['value'])
