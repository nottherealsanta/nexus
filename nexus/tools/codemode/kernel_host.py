"""Internal persistent Monty boundary (kernel plan §3.3, Phase 1).

Not registered with the runtime. JSON framing stays bounded; snapshots cross as
base64 and are trusted, volatile cache bytes only. Handles and every request and
callback are scoped to a random helper generation. Authority belongs to a feed,
never to a stored session. Cancellation discards that worker, not the pool; an
unresponsive helper is fatal and invalidates all its handles.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

if __name__ == '__main__':
    # File execution deliberately avoids importing the Nexus package.
    from ipc import encode, receive, validate
    import monty_adapter
    from monty_adapter import (OUTPUT_LIMIT, classify, read_frame, send, to_json)

    # This helper initializes ToolError in a separate cell, not in user source.
    # The adapter's one-shot process retains its own prelude offset unchanged.
    monty_adapter.PRELUDE_LINES = 0
else:
    from ...util import redact_secrets
    from .ipc import encode, receive, validate
    from .runner import (CodeToolError, INPUT_NAME, LIMITS, MAX_ERROR_CHARS,
                         MAX_PROGRAM_BYTES, Runner)

    #: Hard ceilings for ``limits``. ``max_suspensions`` is higher than the one-shot
    #: runner's default so ``max_nested_calls`` per cell (plus gather overhead) fits.
    CEILINGS = {**LIMITS, 'max_suspensions': 4096}

# A snapshot travels as one base64 frame (``ipc.MAX_SNAPSHOT_FRAME``, 16 MiB, snapshot frames only):
# 8 MiB of snapshot is about 10.7 MiB encoded. Checkpoints (plan code-mode-kernel §3.8) use this cap.
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
#: Host callbacks one feed may make. Callers still tighten this through the
#: Monty suspension limit; a cell's per-cell cap (``max_nested_calls``) is
#: enforced by the loop's nested pipeline (plan code-mode-kernel §3.5).
MAX_CALLS_PER_FEED = 4096
#: Static type checking is deliberately off in the kernel: Monty's checker infers each
#: variable's type from earlier cells (``x = 1`` then ``x = 2`` is rejected) and forgets
#: imports, which a REPL cannot live with. Arguments of tool calls are checked against
#: the tools' schemas by ``argcheck.py`` before any call. ``whos`` is a
#: kernel-side function over the *top-level* ``locals()``: ``locals()`` inside a
#: function is that function's namespace, so the cell runner rewrites a bare
#: ``whos()`` into ``whos(locals())`` (plan code-mode-kernel §3.7, Phase 0).
KERNEL_PRELUDE = (
    'ToolError = RuntimeError\n'
    'def _nx_vars(_ns=None):\n'
    '    out = []\n'
    '    for k in (_ns or {}):\n'
    "        if k in ('ToolError', 'whos', '_nx_vars', 'targets'):\n"
    '            continue\n'
    '        v = _ns[k]\n'
    '        n = len(v) if isinstance(v, (list, dict, str, tuple, set, bytes)) else None\n'
    '        out.append((k, type(v).__name__, n))\n'
    '    return out\n'
    'def whos(_ns=None):\n'
    '    rows = _nx_vars(_ns)\n'
    '    if not rows:\n'
    "        return 'no variables'\n"
    "    lines = ['name  type  size']\n"
    '    for r in rows:\n'
    "        lines.append(r[0] + '  ' + r[1] + '  ' + ('' if r[2] is None else str(r[2])))\n"
    "    return '\\n'.join(lines)\n")
HIDDEN_NAMES = frozenset({'ToolError', 'whos', '_nx_vars', 'targets'})


class KernelBusy(RuntimeError):
    """Capacity cannot be satisfied without discarding live state."""


class HostLost(RuntimeError):
    """Only handles from ``generation`` are invalidated by this failure."""

    def __init__(self, message, generation=None):
        super().__init__(message)
        self.generation = generation


async def _finish(awaitable):
    """Finish owned cleanup despite repeated caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class KernelLost(RuntimeError):
    """One worker was lost; cells must never be replayed automatically."""


class SnapshotInvalid(KernelLost):
    """The supplied checkpoint cannot be decoded by Monty."""


@dataclass(frozen=True)
class KernelHandle:
    generation: str
    id: str


class KernelHost:
    """One lazily spawned, scrubbed helper; independent feeds multiplex IPC.

    Limits are fixed on checkout, including a VM feed-time ceiling. A shorter
    per-feed deadline additionally aborts the worker. No stubs/attachments are
    retained: this minimal API deliberately disables static type checking;
    callbacks are supplied and authorized afresh for every feed.
    """

    def __init__(self, *, max_kernels=4, python=None, limits=None):
        if type(max_kernels) is not int or not 1 <= max_kernels <= 32:
            raise ValueError('max_kernels must be 1..32')
        self.max_kernels = max_kernels
        self.python = python or sys.executable
        self.limits = {**LIMITS, **(limits or {})}
        # Callers may tighten but cannot relax the existing safety ceilings.
        if set(self.limits) != set(LIMITS) or any(
                type(v) not in (int, float) or not 0 <= v <= CEILINGS[k]
                for k, v in self.limits.items()):
            raise ValueError('limits may only tighten existing ceilings')
        self.generation = None
        self._proc = None
        self._reader = None
        self._cwd = None
        self._start_lock = asyncio.Lock()
        self._open_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending = {}
        self._feeds = {}
        self._call_counts = {}
        self._call_ids = {}
        self._callbacks = set()
        self._busy = set()
        self._closed = False

    async def _start(self):
        async with self._start_lock:
            if self._closed:
                raise HostLost('host is closed', self.generation)
            if self._proc is not None:
                return
            self._cwd = tempfile.TemporaryDirectory(prefix='nexus-kernel-')
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    self.python, '-E', '-s', str(Path(__file__).resolve()),
                    cwd=self._cwd.name, env={'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'},
                    start_new_session=True, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            except BaseException:
                self._cwd.cleanup()
                self._cwd = None
                raise
            self.generation = uuid.uuid4().hex
            self._proc._kernel_generation = self.generation
            if self._closed:
                await _finish(self._break(self._proc))
                raise HostLost('host is closed', self.generation)
            self._reader = asyncio.create_task(self._read(self._proc))

    async def _write(self, message, *, proc=None):
        proc = proc or self._proc
        data = encode(message)
        async with self._write_lock:
            if self._proc is None or self._proc is not proc:
                raise HostLost('helper is unavailable', getattr(proc, '_kernel_generation', None))
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _read(self, proc):
        try:
            while True:
                message = await receive(proc.stdout)
                if not isinstance(message, dict):
                    raise ValueError('invalid helper frame')
                request = message.get('request')
                if not isinstance(request, str):
                    raise ValueError('invalid request ID')
                # Requests are random, never reused, and authority is removed on
                # abandonment. Late callbacks/replies cannot acquire authority.
                if request not in self._pending:
                    continue
                if message.get('kind') == 'call':
                    if request not in self._feeds:
                        raise ValueError('callback outside a feed')
                    call = message.get('call')
                    if (not isinstance(call, str) or len(call) > 128
                            or call in self._call_ids[request]
                            or not isinstance(message.get('name'), str)
                            or type(message.get('args')) is not list
                            or type(message.get('kwargs')) is not dict):
                        raise ValueError('invalid callback')
                    self._call_counts[request] += 1
                    if self._call_counts[request] > MAX_CALLS_PER_FEED:
                        raise ValueError('callback limit exceeded')
                    self._call_ids[request].add(call)
                    task = asyncio.create_task(self._serve(message, self._feeds[request]))
                    task._kernel_request = request
                    self._callbacks.add(task)
                    task.add_done_callback(self._callbacks.discard)
                elif message.get('kind') == 'reply':
                    future = self._pending[request]
                    if not future.done():
                        future.set_result(message)
                else:
                    raise ValueError('invalid helper reply')
        except (Exception, asyncio.CancelledError):
            await self._break(proc)

    async def _break(self, proc):
        if self._proc is not proc:
            return
        generation = self.generation
        self._proc = None
        cwd, self._cwd = self._cwd, None
        Runner._kill(proc)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HostLost('sandbox helper lost', generation))
        callbacks = tuple(self._callbacks)
        for task in callbacks:
            task.cancel()
        if callbacks:
            await asyncio.gather(*callbacks, return_exceptions=True)
        await proc.wait()
        if cwd:
            cwd.cleanup()

    async def _serve(self, message, functions):
        proc = self._proc
        try:
            name = message['name']
            if name not in functions:
                raise CodeToolError('function is not callable now')
            value = functions[name](*message['args'], **message['kwargs'])
            if inspect.isawaitable(value):
                value = await value
            response = {'value': validate(value)}
            encode({'verb': 'return', 'request': message['request'],
                    'call': message['call'], **response})
        except asyncio.CancelledError:
            return
        except CodeToolError as exc:
            response = {'error': {'tool': True, 'message': redact_secrets(str(exc))[:MAX_ERROR_CHARS]}}
        except Exception:
            response = {'error': {'tool': False, 'message': 'host call failed'}}
        if self._proc is not proc or message['request'] not in self._feeds:
            return
        try:
            await self._write({'verb': 'return', 'request': message['request'],
                               'call': message['call'], **response}, proc=proc)
        except (Exception, asyncio.CancelledError):
            pass

    async def _request(self, verb, *, handle=None, timeout=10, functions=None, **payload):
        if handle is not None and (self._proc is None or handle.generation != self.generation):
            raise KernelLost('handle belongs to a lost helper generation')
        await self._start()
        generation = self.generation
        if handle is not None and handle.generation != generation:
            raise KernelLost('handle belongs to a lost helper generation')
        request = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        proc = self._proc
        self._pending[request] = future
        kernel = handle.id if handle else uuid.uuid4().hex
        if functions is not None:
            self._feeds[request] = dict(functions)
            self._call_counts[request] = 0
            self._call_ids[request] = set()
        try:
            try:
                # Includes the write lock, pipe backpressure and response wait.
                async with asyncio.timeout(timeout):
                    await self._write({'verb': verb, 'request': request,
                                       'kernel': kernel, **payload}, proc=proc)
                    reply = await asyncio.shield(future)
            except (TimeoutError, asyncio.CancelledError):
                self._feeds.pop(request, None)
                self._pending.pop(request, None)
                future.cancel()
                # Even a successful racing reply must not orphan an open or a
                # mutated session. Abort names the host-owned kernel as well.
                await _finish(self._abort(proc, request, kernel))
                raise
            if reply.get('error'):
                error = str(reply['error'])[:MAX_ERROR_CHARS]
                if reply.get('error_kind') == 'capacity':
                    raise KernelBusy(error)
                if reply.get('error_kind') == 'snapshot_invalid':
                    raise SnapshotInvalid(error)
                raise KernelLost(error)
            if verb == 'open':
                return KernelHandle(generation, kernel)
            return reply.get('value')
        except (ConnectionError, BrokenPipeError) as exc:
            await self._break(proc)
            raise HostLost('helper connection lost', generation) from exc
        finally:
            self._pending.pop(request, None)
            self._feeds.pop(request, None)
            self._call_counts.pop(request, None)
            self._call_ids.pop(request, None)
            # Callback tasks must not outlive the authority that created them.
            # Each task is tagged by its feed via the task-local coroutine args.
            callbacks = [task for task in self._callbacks
                         if getattr(task, '_kernel_request', None) == request]
            for task in callbacks:
                task.cancel()
            if callbacks:
                await _finish(asyncio.gather(*callbacks, return_exceptions=True))
            if future.done() and not future.cancelled():
                future.exception()

    async def _abort(self, proc, request, kernel):
        try:
            async with asyncio.timeout(1):
                await self._write({'verb': 'abort', 'request': request,
                                   'kernel': kernel}, proc=proc)
                cleanup = uuid.uuid4().hex
                done = asyncio.get_running_loop().create_future()
                self._pending[cleanup] = done
                try:
                    await self._write({'verb': 'close', 'request': cleanup,
                                       'kernel': kernel}, proc=proc)
                    await asyncio.shield(done)
                finally:
                    self._pending.pop(cleanup, None)
                    if done.done() and not done.cancelled():
                        done.exception()
        except Exception:
            await self._break(proc)

    def alive(self, generation) -> bool:
        """Whether the helper process of ``generation`` is still running (a crash check)."""
        return self._proc is not None and self.generation == generation

    async def open(self):
        async with self._open_lock:
            return await self._request('open', limits=self.limits, cap=self.max_kernels)

    async def feed(self, handle, program, *, functions, inputs=None, deadline=None):
        if not isinstance(program, str) or len(program.encode()) > MAX_PROGRAM_BYTES:
            raise ValueError('program exceeds 16 KiB')
        if re.search(r'^\s*#\s*///\s*script\b', program, re.M):
            raise ValueError('inline dependency scripts are forbidden')
        inputs = dict(inputs or {})
        if any(not INPUT_NAME.fullmatch(n) or n in functions for n in inputs):
            raise ValueError('invalid sandbox input name')
        if any(not INPUT_NAME.fullmatch(n) for n in functions):
            raise ValueError('invalid callback name')
        validate(inputs)
        if handle in self._busy:
            raise ValueError('concurrent operation on one kernel')
        self._busy.add(handle)
        try:
            timeout = max(0, (deadline or time.monotonic() + 60) - time.monotonic())
            result = await self._request('feed', handle=handle, timeout=timeout,
                                         functions=functions, names=list(functions),
                                         program=program, inputs=inputs)
            return Runner._final(result)
        finally:
            self._busy.discard(handle)

    async def _control(self, verb, handle, **payload):
        if handle in self._busy:
            raise ValueError('concurrent operation on one kernel')
        self._busy.add(handle)
        try:
            return await self._request(verb, handle=handle, **payload)
        finally:
            self._busy.discard(handle)

    async def dump(self, handle):
        data = await self._control('dump', handle)
        snapshot = base64.b64decode(data, validate=True)
        if len(snapshot) > MAX_SNAPSHOT_BYTES:
            raise KernelBusy('snapshot exceeds cap; kernel remains live')
        return snapshot

    async def load(self, handle, snapshot):
        if type(snapshot) is not bytes or len(snapshot) > MAX_SNAPSHOT_BYTES:
            raise ValueError('snapshot exceeds internal frame budget')
        await self._control('load', handle, limits=self.limits,
                            snapshot=base64.b64encode(snapshot).decode('ascii'))

    async def close(self, handle):
        await self._control('close', handle)

    async def shutdown(self):
        self._closed = True
        await _finish(self._shutdown())

    async def _shutdown(self):
        # Startup owns subprocess creation until the process is published.
        async with self._start_lock:
            if self._proc:
                await self._break(self._proc)
            if self._reader:
                self._reader.cancel()
                await asyncio.gather(self._reader, return_exceptions=True)
            await asyncio.gather(*self._callbacks, return_exceptions=True)


async def _helper():
    """Version-specific pool lives exclusively in the credential-free helper."""
    import threading
    from pydantic_monty import AsyncMonty, MontyCrashedError

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=128)
    sessions, tasks, pending = {}, {}, {}

    def reader():
        while True:
            try:
                frame = read_frame()
            except Exception:
                loop.call_soon_threadsafe(lambda: __import__('os')._exit(1))
                return
            # Backpressure, including malicious callback floods.
            asyncio.run_coroutine_threadsafe(queue.put(frame), loop).result()

    threading.Thread(target=reader, daemon=True).start()

    async with AsyncMonty(min_processes=0, max_processes=32,
                          checkout_timeout=5, request_timeout=15,
                          # Never reuse a released session worker, including
                          # releases following failure or cancellation.
                          max_checkouts_per_worker=1) as pool:
        # Same fail-closed pure-interpreter check as the disposable adapter.
        async with pool.checkout() as probe:
            try:
                await probe.feed_run('import subprocess')
            except Exception:
                pass
            else:
                raise RuntimeError('unsafe interpreter')

        async def release(kernel):
            item = sessions.pop(kernel, None)
            if item:
                await item[0].__aexit__(None, None, None)

        async def operation(frame):
            request, kernel, verb = frame['request'], frame['kernel'], frame['verb']
            state = {'over': False, 'tool_errors': set(), 'size': 0, 'calls': 0}
            chunks = []
            value = None
            error = None
            error_kind = None
            reply_snapshot = False

            def on_print(stream, text):
                state['size'] += len(json.dumps(text, ensure_ascii=True).encode())
                if state['size'] > OUTPUT_LIMIT:
                    state['over'] = True
                    raise RuntimeError('output limit')
                chunks.append(text)

            def callback(name):
                async def invoke(*args, **kwargs):
                    state['calls'] += 1
                    if state['calls'] > MAX_CALLS_PER_FEED:
                        raise RuntimeError('callback call limit exceeded')
                    call = uuid.uuid4().hex
                    future = loop.create_future()
                    pending[(request, call)] = future
                    try:
                        frame = {'kind': 'call', 'request': request, 'call': call,
                                 'name': name, 'args': list(args), 'kwargs': kwargs}
                        encode(frame)
                        send(frame)
                        reply = await future
                        if 'error' in reply:
                            text = reply['error']['message']
                            if reply['error'].get('tool'):
                                state['tool_errors'].add(text)
                            raise RuntimeError(text)
                        return reply.get('value')
                    finally:
                        pending.pop((request, call), None)
                return invoke

            try:
                if verb == 'open':
                    if len(sessions) >= frame['cap']:
                        raise RuntimeError('live kernel cap reached')
                    cm = pool.checkout(limits=frame['limits'], type_check=False)
                    session = await cm.__aenter__()
                    # Publish ownership before setup so failed/cancelled setup
                    # releases the checkout too. This runs once per new worker,
                    # before either user feeds or a restored snapshot is loaded.
                    sessions[kernel] = (cm, session)
                    await session.feed_run(KERNEL_PRELUDE)
                    value = kernel
                else:
                    session = sessions.get(kernel, (None, None))[1]
                    if verb == 'feed':
                        if session is None:
                            raise KernelLost('kernel not found')
                        try:
                            result = await session.feed_run(
                                frame['program'], inputs=frame['inputs'] or None,
                                external_lookup={n: callback(n) for n in frame['names']},
                                print_callback=on_print)
                            if state['over']:
                                raise RuntimeError('output limit exceeded')
                            value = {'kind': 'done', 'value': to_json(result), 'output': ''.join(chunks)}
                            # Oversized values fail boundedly, not by breaking IPC.
                            encode({'kind': 'reply', 'request': request, 'value': value})
                        except MontyCrashedError:
                            await release(kernel)
                            error = 'worker lost'
                        except Exception as exc:
                            value = {'kind': 'failed', 'output': ''.join(chunks),
                                     'diagnostic': classify(exc, state)}
                    elif verb == 'dump':
                        snapshot = await session.dump()
                        if len(snapshot) > MAX_SNAPSHOT_BYTES:
                            raise KernelBusy('snapshot exceeds internal frame budget')
                        value = base64.b64encode(snapshot).decode('ascii')
                        reply_snapshot = True
                    elif verb == 'load':
                        snapshot = base64.b64decode(frame['snapshot'], validate=True)
                        if len(snapshot) > MAX_SNAPSHOT_BYTES:
                            raise KernelBusy('snapshot exceeds internal frame budget')
                        # Monty requires a fresh (unfed) restore target.
                        await release(kernel)
                        cm = pool.checkout(limits=frame['limits'], type_check=False)
                        session = await cm.__aenter__()
                        sessions[kernel] = (cm, session)
                        from pydantic_monty import MontyRuntimeError
                        try:
                            await session.load_session(snapshot)
                        except MontyRuntimeError as exc:
                            # Monty 1.1 exposes decode failure as a generic
                            # runtime error, not a dedicated exception type.
                            # Adapt only its explicit decode diagnostic; other
                            # restore failures remain retryable host failures.
                            if 'failed to load session:' in str(exc):
                                raise SnapshotInvalid('invalid kernel snapshot') from exc
                            raise
                        await session.feed_run(KERNEL_PRELUDE)
                    elif verb == 'close':
                        await release(kernel)
                    else:
                        raise ValueError('unknown verb')
            except KernelBusy:
                error = 'snapshot exceeds cap; kernel remains live'
                error_kind = 'capacity'
            except SnapshotInvalid:
                await release(kernel)
                error = 'invalid kernel snapshot'
                error_kind = 'snapshot_invalid'
            except KernelLost:
                error = 'kernel not found'
            except asyncio.CancelledError:
                await release(kernel)
                error = 'worker discarded after cancellation'
            except Exception:
                if verb in ('open', 'load'):
                    await release(kernel)
                error = 'kernel operation failed'
            finally:
                tasks.pop(request, None)
            reply = {'kind': 'reply', 'request': request, 'value': value, 'error': error, 'error_kind': error_kind}
            if reply_snapshot and error is None:
                reply['snapshot'] = True
            try:
                encode(reply)
            except (ValueError, TypeError, OverflowError, RecursionError):
                if verb == 'dump':
                    send({'kind': 'reply', 'request': request, 'error_kind': 'capacity',
                          'error': 'snapshot frame exceeds cap; kernel remains live'})
                    return
                reply = {'kind': 'reply', 'request': request,
                         'value': {'kind': 'failed', 'output': '', 'diagnostic':
                                   {'kind': 'output_limit', 'message': 'serialized response exceeds limit'}}}
            send(reply)

        while True:
            frame = await queue.get()
            verb, request = frame['verb'], frame['request']
            if verb == 'return':
                future = pending.get((request, frame['call']))
                if future and not future.done():
                    future.set_result(frame)
            elif verb == 'abort':
                task = tasks.get(request)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await release(frame['kernel'])
            else:
                tasks[request] = asyncio.create_task(operation(frame))


if __name__ == '__main__':
    asyncio.run(_helper())
