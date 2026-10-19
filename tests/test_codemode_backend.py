import importlib.util
import math
import time

import pytest

from nexus.tools.codemode.ipc import encode, validate
from nexus.tools.codemode.runner import Runner
from nexus.tools.codemode.store import Store
from nexus.tools.codemode.stubs import stub


def test_json_boundary():
    for bad in (object(), b'bytes', math.inf, {1: 'x'}):
        with pytest.raises(ValueError):
            validate(bad)
    with pytest.raises(ValueError):
        encode('x' * (2 * 1024 * 1024))


def test_store_transaction_replay():
    store = Store()
    store.store('cursor', [1, 2])
    assert not store.values
    assert store.load('cursor') == [1, 2]
    restored = Store([{'type': 'code.store', 'data': store.event('outer')}])
    assert restored.load('cursor') == [1, 2]
    with pytest.raises(ValueError):
        store.store('huge', 'x' * 65536)


def test_stub_injection():
    text = stub('read', {'properties': {'evil\nprint(123)': {'type': 'string'}}})
    assert text == 'async def read(**kwargs: Any) -> Any: ...'


@pytest.mark.asyncio
async def test_source_gating():
    runner = Runner()
    for program in ('x' * 16385, '# /// script\n1'):
        with pytest.raises(ValueError):
            await runner.run(program, functions={})


@pytest.mark.asyncio
@pytest.mark.skipif(importlib.util.find_spec('pydantic_monty') is None, reason='optional codemode extra')
async def test_real_sandbox_roundtrip_and_security():
    runner = Runner()
    result = await runner.run('await echo(x="hello")', functions={'echo': lambda x: x},
                              stubs='async def echo(*, x: str) -> str: ...')
    assert result['kind'] == 'done' and result['value'] == 'hello'
    for program in ('import subprocess', 'open("/etc/passwd").read()', 'import os\nos.environ'):
        result = await runner.run(program, functions={})
        assert result['kind'] == 'failed'
    result = await runner.run('while True:\n    pass', functions={}, deadline=time.monotonic() + 1)
    assert result["diagnostic"]["kind"] == "timeout"
    await runner.close()
