"""LRU and idle eviction restore transparently through the same path as a restart (kernel plan §3.4, §6)."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip('pydantic_monty')

from test_codemode_snapshots import durable_scope  # noqa: E402
from test_codemode_tool import Rig, config, text_of  # noqa: E402


@pytest.fixture
async def durable(tmp_path):
    async with durable_scope(tmp_path) as make:
        yield make


async def test_lru_eviction_restores_the_evicted_kernel_with_its_variables(durable):
    d = durable(max_live_kernels=1)
    r = await d.rig()
    await r.cell('mine = 11')
    await r.call('code_start', {}, agent_id='child-1')        # needs the only slot: root is evicted
    registry = r.kernels.registry
    assert registry.status('s1', 'root') == 'evicted' and registry.status('s1', 'child-1') == 'live'
    await r.cell('theirs = 22', agent_id='child-1')
    result = await r.cell('mine', agent_id='root')
    body = text_of(result)
    assert not result.is_error and 'Out [2]: 11' in body
    assert not body.startswith('Kernel restored')              # the exact in-process state came back
    restored = r.of('code.kernel_restored')[-1]
    assert restored['reason'] == 'evicted' and restored['missing_cells'] == [] and restored['from_n'] == 1
    assert 'Out [2]: 22' in text_of(await r.cell('theirs', agent_id='child-1'))   # and the other was evicted/restored


async def test_idle_eviction_restores_transparently(durable):
    d = durable()
    r = await d.rig()
    await r.cell("seen = ['a', 'b']")
    registry = r.kernels.registry
    registry.idle_seconds = 0.05
    await asyncio.sleep(0.15)
    await r.kernels.maintain()
    assert registry.status('s1', 'root') == 'evicted'
    body = text_of(await r.cell('seen'))
    assert "Out [2]: ['a', 'b']" in body and not body.startswith('Kernel restored')
    assert r.of('code.kernel_restored')[-1]['reason'] == 'evicted'


async def test_an_evicted_kernel_survives_a_restart_through_its_checkpoint(durable):
    d = durable(max_live_kernels=1)
    r = await d.rig()
    await r.cell('kept = 3')
    await r.call('code_start', {}, agent_id='child-1')
    assert r.kernels.registry.status('s1', 'root') == 'evicted'
    r2 = await d.restart(r)                                    # the in-memory snapshot died with the process
    body = text_of(await r2.cell('kept'))
    assert body.startswith('Kernel restored from after In [1]') and 'Out [2]: 3' in body
    assert r2.of('code.kernel_restored')[-1]['reason'] == 'restart'


async def test_eviction_without_durability_still_restores_from_memory():
    r = Rig(real=True, cfg=config(max_live_kernels=1))
    try:
        await r.start()
        await r.cell('v = 9')
        await r.call('code_start', {}, agent_id='child-1')
        assert 'Out [2]: 9' in text_of(await r.cell('v'))
    finally:
        await r.aclose()
