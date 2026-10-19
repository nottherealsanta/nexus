"""Credential-free process-group boundary around the optional interpreter.

``Runner.run`` executes one program in a disposable helper process (plan 03
sections 3.6 and 3.8; contract I3). Host functions may run concurrently: the
helper tags each call with an id and the runner answers by id, so
``asyncio.gather`` in a program overlaps real work. The call budget has a single
authority, Monty's ``max_suspensions``; hitting it is a ``limit:calls``
diagnostic, not an exception.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time

from ...util import redact_secrets
from .ipc import encode, receive, validate

LIMITS = {
    'max_feed_duration_secs': 10, 'max_turn_duration_secs': 5,
    'max_memory': 64 * 1024 * 1024, 'max_recursion_depth': 200,
    'max_suspensions': 64, 'max_total_sleep_secs': 0,
}
MAX_PROGRAM_BYTES = 16384
MAX_STUB_BYTES = 262144
MAX_ERROR_CHARS = 8000
ABORT_GRACE_S = 1.0
INPUT_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class CodeToolError(Exception):
    """Raised by a host function; the message reaches the program as ToolError.

    The message is redacted and bounded by the runner. Any other exception
    type surfaces in the sandbox only as a generic "host call failed".
    """


class ContextNamespaceProvider:
    """Structural marker; the real provider lives in ``context_ns.py``."""


def failed(kind, message, output='', line=None):
    return {'kind': 'failed', 'value': None, 'output': output,
            'diagnostic': {'kind': kind, 'message': message, 'line': line, 'traceback': ''}}


class Runner:
    """Admission is daemon-local; every call has a disposable helper and heap.

    ``context_provider`` is consulted by the code tool, which authorizes every
    target up front; the runner itself never grants access.
    """

    def __init__(self, max_workers=2, *, python=None, context_provider=None):
        self._slots = asyncio.Semaphore(max_workers)
        self.python = python or sys.executable
        self.context_provider = context_provider
        self._tasks = set()
        self._procs = set()

    async def close(self):
        for proc in tuple(self._procs):
            self._kill(proc)
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _kill(proc):
        # Includes Rust grandchildren, even when the helper exited first.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    async def run(self, program, *, functions, inputs=None, stubs='', limits=None,
                  deadline=None, cancel=None):
        if not isinstance(program, str) or len(program.encode()) > MAX_PROGRAM_BYTES:
            raise ValueError('program exceeds 16 KiB')
        if re.search(r'^\s*#\s*///\s*script\b', program, re.M):
            raise ValueError('inline dependency scripts are forbidden')
        if len(stubs.encode()) > MAX_STUB_BYTES:
            raise ValueError('stub catalog exceeds limit')
        inputs = dict(inputs or {})
        for name in inputs:
            if not INPUT_NAME.match(name) or name in functions:
                raise ValueError(f'invalid sandbox input name: {name!r}')
        validate(inputs)
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            try:
                await asyncio.wait_for(self._slots.acquire(), 5)
            except TimeoutError:
                return failed('RuntimeError', 'code mode busy, retry')
            try:
                return await self._run(program, functions, inputs, stubs,
                                       {**LIMITS, **(limits or {})}, deadline, cancel)
            finally:
                self._slots.release()
        finally:
            self._tasks.discard(task)

    async def _run(self, program, functions, inputs, stubs, limits, deadline, cancel):
        helper = Path(__file__).with_name('monty_adapter.py').resolve()
        deadline = deadline or time.monotonic() + 60
        with tempfile.TemporaryDirectory(prefix='nexus-code-') as cwd:
            # No HOME, PATH, PYTHONPATH, proxy, telemetry, or provider variables.
            env = {'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
            proc = await asyncio.create_subprocess_exec(
                self.python, '-E', '-s', str(helper), cwd=cwd, env=env,
                start_new_session=True, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=4 * 1024 * 1024,
            )
            self._procs.add(proc)
            calls = set()
            write_lock = asyncio.Lock()

            async def write(frame):
                async with write_lock:
                    proc.stdin.write(encode(frame))
                    await proc.stdin.drain()

            async def serve(message):
                call_id = message['id']
                try:
                    name = message['name']
                    if name not in functions:
                        raise CodeToolError(f'unknown function {name}')
                    value = functions[name](*message['args'], **message['kwargs'])
                    if inspect.isawaitable(value):
                        value = await value
                    try:
                        response = {'kind': 'return', 'id': call_id, 'value': validate(value)}
                        encode(response)
                    except ValueError as exc:
                        raise CodeToolError(f'result cannot be returned to the program: {exc}') from None
                except asyncio.CancelledError:
                    raise
                except CodeToolError as exc:
                    text = redact_secrets(str(exc))[:MAX_ERROR_CHARS]
                    response = {'kind': 'return', 'id': call_id, 'error': {'tool': True, 'message': text}}
                except Exception:
                    # Host exception strings may include credentials and paths.
                    response = {'kind': 'return', 'id': call_id,
                                'error': {'tool': False, 'message': 'host call failed'}}
                try:
                    await write(response)
                except (ConnectionError, ValueError):
                    pass

            async def watch_cancel():
                await cancel.wait()

            watcher = asyncio.create_task(watch_cancel()) if cancel is not None else None
            try:
                await write({'program': program, 'functions': list(functions), 'inputs': inputs,
                             'stubs': stubs, 'limits': limits,
                             'timeout': max(1, deadline - time.monotonic())})
                reason = None
                reader = None
                while reason is None:
                    if reader is None:
                        reader = asyncio.ensure_future(receive(proc.stdout))
                    waiting = {reader} | ({watcher} if watcher else set())
                    done, _ = await asyncio.wait(
                        waiting, timeout=max(0, deadline - time.monotonic()),
                        return_when=asyncio.FIRST_COMPLETED)
                    if reader not in done:
                        reason = 'cancelled' if watcher in done else 'timeout'
                        break
                    try:
                        message = reader.result()
                        reader = None
                    except (asyncio.IncompleteReadError, ValueError):
                        return failed('crashed', 'sandbox helper exited unexpectedly')
                    kind = message.get('kind')
                    if kind in ('done', 'failed'):
                        return self._final(message)
                    if kind != 'call':
                        return failed('crashed', 'invalid helper message')
                    task = asyncio.create_task(serve(message))
                    calls.add(task)
                    task.add_done_callback(calls.discard)
                # Ask the helper to stop so partial output survives; then kill.
                output = ''
                try:
                    await write({'kind': 'abort', 'reason': reason})
                    async with asyncio.timeout(ABORT_GRACE_S):
                        while True:
                            if reader is None:
                                reader = asyncio.ensure_future(receive(proc.stdout))
                            message = await asyncio.shield(reader)
                            reader = None
                            if message.get('kind') in ('done', 'failed'):
                                output = str(message.get('output', ''))
                                break
                except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
                    pass
                text = ('code execution deadline exceeded' if reason == 'timeout'
                        else 'program cancelled')
                return failed(reason, text, output)
            finally:
                if reader is not None:
                    reader.cancel()
                if watcher:
                    watcher.cancel()
                for task in tuple(calls):
                    task.cancel()
                self._kill(proc)
                await proc.wait()
                self._procs.discard(proc)
                await asyncio.gather(*calls, *([watcher] if watcher else []), return_exceptions=True)

    @staticmethod
    def _final(message):
        output = str(message.get('output', ''))
        if message['kind'] == 'done':
            return {'kind': 'done', 'value': message.get('value'), 'output': output, 'diagnostic': None}
        diag = dict(message.get('diagnostic') or {})
        diagnostic = {'kind': str(diag.get('kind', 'RuntimeError')),
                      'message': redact_secrets(str(diag.get('message', '')))[:MAX_ERROR_CHARS],
                      'line': diag.get('line'),
                      'traceback': redact_secrets(str(diag.get('traceback', '')))[:MAX_ERROR_CHARS]}
        return {'kind': 'failed', 'value': None, 'output': output, 'diagnostic': diagnostic}
