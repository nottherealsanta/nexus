"""Isolated helper entry point. This alone imports the optional Monty dependency.

The daemon executes this file in a new process group with a minimal environment
and private empty cwd. No host OS provider or mount is ever installed (plan 03
sections 3.6 and 3.8).

Protocol (JSON frames, see ``ipc.py``): the host sends one request frame, then
``return`` frames answering the helper's ``call`` frames (correlated by ``id``;
calls overlap) and optionally one ``abort`` frame. The helper ends with exactly
one ``done`` or ``failed`` frame carrying the printed output, and exits.
Everything version specific about Monty lives here.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import struct
import sys
import threading

# Executed as a file, not through nexus package initialization.
from ipc import MAX_FRAME, encode, validate

#: ``except ToolError`` works because Monty has no class inheritance (verified
#: against 1.1.0) and a host-raised exception arrives as its builtin base class.
#: ToolError is therefore an alias of RuntimeError, bound by this prelude line
#: (an ``inputs`` value of a class becomes an opaque object that cannot be
#: caught). Reported line numbers subtract PRELUDE_LINES.
PRELUDE = 'ToolError = RuntimeError\n'
PRELUDE_LINES = 1
STUB_PRELUDE = 'ToolError = RuntimeError\n'
OUTPUT_LIMIT = 1024 * 1024
TRACE_FRAMES = 14
TRACE_CHARS = 4000
IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class OutputLimit(Exception):
    pass


def read_frame():
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        raise EOFError('helper input closed')
    size = struct.unpack('!I', header)[0]
    if size > MAX_FRAME:
        raise ValueError('IPC frame exceeds limit')
    data = sys.stdin.buffer.read(size)
    if len(data) != size:
        raise EOFError('truncated frame')
    return validate(json.loads(data))


def send(value):
    sys.stdout.buffer.write(encode(value))
    sys.stdout.buffer.flush()


def to_json(value, depth=0):
    """Coerce a sandbox result to the JSON subset instead of failing the run."""
    if depth > 32:
        return '<nested too deeply>'
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        return value if value == value and value not in (float('inf'), float('-inf')) else str(value)
    if isinstance(value, (list, tuple)):
        return [to_json(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return [to_json(item, depth + 1) for item in sorted(value, key=repr)]
    if isinstance(value, dict):
        return {str(k): to_json(v, depth + 1) for k, v in value.items()}
    if isinstance(value, bytes):
        return f'<{len(value)} bytes>'
    return str(value)[:2000]


def traceback_text(frames, kind, message):
    """Bounded Python-style traceback with prelude-adjusted line numbers."""
    rows = []
    for frame in frames:
        line = max(1, frame.line - PRELUDE_LINES)
        where = frame.function_name or '<module>'
        row = f'  line {line}, in {where}'
        if frame.source_line and frame.function_name is not None or frame.source_line:
            row += f'\n    {frame.source_line.strip()}'
        rows.append(row)
    if len(rows) > TRACE_FRAMES:
        omitted = len(rows) - TRACE_FRAMES
        rows = rows[:3] + [f'  ... {omitted} frames omitted ...'] + rows[-(TRACE_FRAMES - 3):]
    head = 'Traceback (most recent call last):\n' if rows else ''
    return (head + '\n'.join(rows) + ('\n' if rows else '') + f'{kind}: {message}')[:TRACE_CHARS]


def classify(exc, state):
    """Map a Monty/host exception to the diagnostic dict of contract I3."""
    from pydantic_monty import (MontyCrashedError, MontyRuntimeError,
                                MontySyntaxError, MontyTypingError)
    if state['over']:
        return {'kind': 'limit:output', 'message': f'printed output exceeded {OUTPUT_LIMIT} bytes',
                'line': None, 'traceback': ''}
    if isinstance(exc, MontyTypingError):
        text = str(exc)
        match = re.search(r'main\.py:(\d+)', text)
        text = re.sub(r'main\.py:(\d+)', lambda m: f'line {max(1, int(m.group(1)) - PRELUDE_LINES)}', text)
        # An undefined name is caught by the checker first; report it as the
        # NameError the program would have raised.
        kind = ('NameError' if 'unresolved-reference' in text
                else 'SyntaxError' if 'invalid-syntax' in text
                else 'TypeError')
        return {'kind': kind, 'message': text[:TRACE_CHARS],
                'line': max(1, int(match.group(1)) - PRELUDE_LINES) if match else None,
                'traceback': ''}
    if isinstance(exc, MontyCrashedError):
        if exc.timed_out:
            return {'kind': 'timeout', 'message': 'sandbox worker exceeded its request timeout',
                    'line': None, 'traceback': ''}
        return {'kind': 'crashed', 'message': 'sandbox worker crashed; nothing was persisted',
                'line': None, 'traceback': ''}
    if isinstance(exc, (MontyRuntimeError, MontySyntaxError)):
        inner = exc.exception()
        name = type(inner).__name__
        message = str(inner)
        frames = exc.traceback()
        line = max(1, frames[-1].line - PRELUDE_LINES) if frames else None
        if isinstance(exc, MontySyntaxError):
            kind = 'SyntaxError'
        elif name == 'MemoryError':
            kind = 'limit:memory'
        elif name == 'TimeoutError':
            kind = 'limit:time'
        elif name == 'RuntimeError' and message.startswith('suspension limit'):
            kind = 'limit:calls'
        elif name == 'RuntimeError' and message in state['tool_errors']:
            kind = 'ToolError'
        elif name in ('NotImplementedError',) and 'does not yet support' in message:
            kind = 'SyntaxError'
        else:
            kind = name
        return {'kind': kind, 'message': message[:TRACE_CHARS], 'line': line,
                'traceback': traceback_text(frames, name, message)}
    return {'kind': 'RuntimeError', 'message': f'{type(exc).__name__}: {exc}'[:TRACE_CHARS],
            'line': None, 'traceback': ''}


async def main(request):
    from pydantic_monty import AsyncMonty

    loop = asyncio.get_running_loop()
    pending = {}
    abort = asyncio.Event()
    abort_reason = ['cancelled']
    ids = itertools.count(1)
    chunks = []
    state = {'over': False, 'tool_errors': set(), 'size': 0}

    def dispatch(frame):
        kind = frame.get('kind')
        if kind == 'return':
            future = pending.pop(frame.get('id'), None)
            if future is not None and not future.done():
                future.set_result(frame)
        elif kind == 'abort':
            abort_reason[0] = frame.get('reason', 'cancelled')
            abort.set()

    def reader():
        while True:
            try:
                frame = read_frame()
            except Exception:
                # Host gone or protocol broken: stop rather than run unattended.
                loop.call_soon_threadsafe(dispatch, {'kind': 'abort', 'reason': 'cancelled'})
                return
            loop.call_soon_threadsafe(dispatch, frame)

    threading.Thread(target=reader, daemon=True).start()

    def on_print(stream, text):
        state['size'] += len(text.encode())
        if state['size'] > OUTPUT_LIMIT:
            state['over'] = True
            raise OutputLimit('output limit')
        chunks.append(text)

    def callback(name):
        async def invoke(*args, **kwargs):
            call_id = next(ids)
            future = loop.create_future()
            pending[call_id] = future
            try:
                send({'kind': 'call', 'id': call_id, 'name': name,
                      'args': list(args), 'kwargs': kwargs})
            except ValueError as exc:
                pending.pop(call_id, None)
                raise TypeError(f'arguments to {name} must be plain JSON values: {exc}') from None
            response = await future
            error = response.get('error')
            if error is not None:
                message = str(error.get('message', 'host call failed'))
                if error.get('tool'):
                    state['tool_errors'].add(message)
                raise RuntimeError(message)
            return response.get('value')
        return invoke

    inputs = {k: v for k, v in (request.get('inputs') or {}).items() if IDENT.match(k)}
    stubs = request['stubs']
    if 'ToolError' not in stubs:
        stubs = stubs + '\n' + STUB_PRELUDE
    try:
        async with AsyncMonty(min_processes=0, max_processes=1,
                              checkout_timeout=5, request_timeout=request['timeout'] + 2,
                              max_checkouts_per_worker=50) as pool:
            # Verify the selected binary is the pure interpreter, not CPython.
            async with pool.checkout() as probe:
                try:
                    await probe.feed_run('import subprocess')
                except Exception:
                    pass
                else:
                    raise RuntimeError('unsafe interpreter: subprocess import succeeded')
            async with pool.checkout(limits=request['limits'], type_check=True,
                                     type_check_stubs=stubs,
                                     type_check_format='concise') as session:
                feed = asyncio.ensure_future(session.feed_run(
                    PRELUDE + request['program'],
                    inputs=inputs or None,
                    external_lookup={name: callback(name) for name in request['functions']},
                    print_callback=on_print,
                ))
                waiter = asyncio.create_task(abort.wait())
                try:
                    await asyncio.wait((feed, waiter), return_when=asyncio.FIRST_COMPLETED)
                    if not feed.done():
                        feed.cancel()
                        await asyncio.wait((feed,), timeout=0.5)
                        reason = abort_reason[0]
                        send({'kind': 'failed', 'output': ''.join(chunks),
                              'diagnostic': {'kind': reason, 'message': f'program stopped: {reason}',
                                             'line': None, 'traceback': ''}})
                        return
                    value = feed.result()
                finally:
                    waiter.cancel()
                send({'kind': 'done', 'value': to_json(value), 'output': ''.join(chunks)})
    except BaseException as exc:  # noqa: BLE001 - the helper must always report
        try:
            send({'kind': 'failed', 'output': ''.join(chunks), 'diagnostic': classify(exc, state)})
        except Exception:
            pass


def run():
    request = read_frame()
    asyncio.run(main(request))
    sys.stdout.flush()
    # Skip pool shutdown: the host kills the whole process group anyway.
    os._exit(0)


if __name__ == '__main__':
    run()
