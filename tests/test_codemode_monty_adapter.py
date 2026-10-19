"""Real Monty sandbox through the isolated helper (plan 03 section 7 test 4).

Skipped when the optional ``pydantic-monty`` extra is missing.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import time

import pytest

from nexus.tools.codemode.runner import CodeToolError, Runner

pytestmark = pytest.mark.skipif(importlib.util.find_spec('pydantic_monty') is None,
                                reason='optional codemode extra')

STUBS = '''from typing import Any
async def echo(x: Any) -> Any: ...
async def slow(x: Any) -> Any: ...
async def deny() -> Any: ...
async def bad() -> Any: ...
async def hang() -> Any: ...
'''


async def echo(x):
    return x


async def slow(x):
    await asyncio.sleep(0.2)
    return x


async def deny():
    raise CodeToolError('File not found: sk-ant-api03-' + 'a' * 40)


async def bad():
    raise KeyError('secret path /home/me')


@pytest.fixture
async def runner():
    r = Runner()
    yield r
    await r.close()


FUNCS = {'echo': echo, 'slow': slow, 'deny': deny, 'bad': bad}


async def run(runner, program, **kw):
    kw.setdefault('functions', FUNCS)
    kw.setdefault('stubs', STUBS)
    return await runner.run(program, **kw)


def kind(result):
    return result['diagnostic']['kind']


async def test_roundtrip_inputs_and_tuple_coercion(runner):
    result = await run(runner, '(await echo("a"), targets)', inputs={'targets': ['self']},
                       stubs=STUBS + 'targets: list[str]\n')
    assert result == {'kind': 'done', 'value': ['a', ['self']], 'output': '', 'diagnostic': None}


async def test_syntax_type_and_name_errors(runner):
    result = await run(runner, 'x = (')
    assert kind(result) == 'SyntaxError' and result['diagnostic']['line'] == 1
    result = await run(runner, 'y = 1\nawait nope()')
    assert kind(result) == 'NameError' and result['diagnostic']['line'] == 2
    result = await run(runner, 'x: int = "s"\nx')
    assert kind(result) == 'TypeError' and result['diagnostic']['line'] == 1
    result = await run(runner, 'def g():\n    yield 1\n1')
    assert kind(result) == 'SyntaxError'
    result = await run(runner, 'match 1:\n    case 1:\n        pass')
    assert kind(result) == 'SyntaxError'


async def test_runtime_error_has_line_and_bounded_traceback(runner):
    result = await run(runner, 'def f(n):\n    return f(n + 1)\nf(0)')
    assert kind(result) == 'RecursionError'
    trace = result['diagnostic']['traceback']
    assert 'frames omitted' in trace and len(trace) <= 8000
    result = await run(runner, 'a = 1\nb = 0\na / b')
    assert kind(result) == 'ZeroDivisionError' and result['diagnostic']['line'] == 3


@pytest.mark.parametrize('program,limits,expected', [
    ('x = [0] * 10**8\n1', {'max_memory': 8 << 20}, 'limit:memory'),
    ('while True:\n    pass', {'max_feed_duration_secs': 1}, 'limit:time'),
    ('for i in range(10):\n    await echo(i)\n1', {'max_suspensions': 3}, 'limit:calls'),
])
async def test_each_limit_has_its_kind(runner, program, limits, expected):
    result = await run(runner, program, limits=limits)
    assert result['kind'] == 'failed' and kind(result) == expected
    # the next call is unaffected: a limit never poisons later programs
    assert (await run(runner, '1 + 1'))['value'] == 2


async def test_output_limit_clips_and_keeps_partial_output(runner):
    result = await run(runner, "print('start')\nfor i in range(200000):\n    print('y' * 100)")
    assert kind(result) == 'limit:output'
    assert result['output'].startswith('start\n') and len(result['output']) <= 1024 * 1024 + 200


async def test_open_os_subprocess_fail(runner):
    for program in ('open("/etc/passwd").read()', 'import subprocess', 'import os\nos.environ["HOME"]',
                    'import os\nos.listdir("/")'):
        result = await run(runner, program)
        assert result['kind'] == 'failed', program


async def test_pep723_and_oversize_rejected(runner):
    with pytest.raises(ValueError):
        await run(runner, '# /// script\n# dependencies = ["x"]\n# ///\n1')
    with pytest.raises(ValueError):
        await run(runner, 'x' * 16385)


async def test_tool_error_catchable_with_redacted_message(runner):
    program = ('try:\n    await deny()\nexcept ToolError as e:\n    r = str(e)\nr')
    result = await run(runner, program)
    assert result['kind'] == 'done'
    assert result['value'].startswith('File not found: ') and 'sk-ant' not in result['value']
    uncaught = await run(runner, 'await deny()')
    assert kind(uncaught) == 'ToolError' and uncaught['diagnostic']['line'] == 1


async def test_other_host_exceptions_are_generic(runner):
    result = await run(runner, 'try:\n    await bad()\nexcept ToolError as e:\n    r = str(e)\nr')
    assert result['value'] == 'host call failed'
    assert 'secret' not in str(result)


async def test_concurrent_host_calls_overlap(runner):
    started = time.monotonic()
    result = await run(runner, 'import asyncio\nawait asyncio.gather(slow(1), slow(2), slow(3))')
    assert result['value'] == [1, 2, 3]
    assert time.monotonic() - started < 0.5 + 1.5  # three 0.2 s calls overlap, plus helper start
    inside = await run(runner, 'import asyncio\nawait asyncio.gather(slow(1), slow(2), slow(3))\n1',
                       functions={**FUNCS, 'slow': _timed})
    assert inside['kind'] == 'done'
    assert max(_overlap) == 3


_overlap: list[int] = []
_active = 0


async def _timed(x):
    global _active
    _active += 1
    _overlap.append(_active)
    await asyncio.sleep(0.2)
    _active -= 1
    return x


async def test_deadline_reports_timeout_with_partial_output(runner):
    result = await run(runner, "print('partial')\nwhile True:\n    pass",
                       deadline=time.monotonic() + 2, limits={'max_feed_duration_secs': 60})
    assert kind(result) == 'timeout' and result['output'] == 'partial\n'


async def test_cancel_stops_the_worker(runner):
    cancel = asyncio.Event()
    task = asyncio.create_task(run(runner, 'while True:\n    pass', cancel=cancel,
                                   limits={'max_feed_duration_secs': 60}, deadline=time.monotonic() + 60))
    await asyncio.sleep(1.5)
    assert runner._procs
    pids = [p.pid for p in runner._procs]
    cancel.set()
    result = await asyncio.wait_for(task, 5)
    assert kind(result) == 'cancelled'
    assert not runner._procs
    for pid in pids:
        ps = subprocess.run(['ps', '-axo', 'pgid='], capture_output=True, text=True).stdout.split()
        assert str(pid) not in ps


async def test_task_cancellation_kills_worker(runner):
    task = asyncio.create_task(run(runner, 'while True:\n    pass', limits={'max_feed_duration_secs': 60},
                                   deadline=time.monotonic() + 60))
    await asyncio.sleep(1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runner._procs


async def test_worker_crash_is_reported_and_runner_recovers(runner):
    async def killer():
        # Kill every process in the helper's group except the helper: the Monty worker dies.
        for proc in list(runner._procs):
            out = subprocess.run(['ps', '-axo', 'pid=,pgid='], capture_output=True, text=True).stdout
            for line in out.splitlines():
                pid, pgid = map(int, line.split())
                if pgid == proc.pid and pid != proc.pid:
                    subprocess.run(['kill', '-9', str(pid)])
        await asyncio.sleep(0.5)
        return 1

    result = await run(runner, 'await hang()\n1', functions={**FUNCS, 'hang': killer})
    assert result['kind'] == 'failed' and kind(result) in ('crashed', 'RuntimeError', 'timeout')
    assert (await run(runner, '1 + 1'))['value'] == 2
