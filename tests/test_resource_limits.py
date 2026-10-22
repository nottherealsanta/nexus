"""Resource admission fairness, hard bounds and cancellation."""
import asyncio

import pytest

from nexus.resources import FairPool, ResourceLimits, ResourceScope, current_scope, process_slot


async def test_project_then_session_fairness():
    pool = FairPool(1)
    held = await pool.acquire('a', 'one')
    order = []
    async def work(project, session):
        release = await pool.acquire(project, session)
        assert pool.active == 1
        order.append((project, session))
        release()
    tasks = [asyncio.create_task(work(*key)) for key in
             [('a', 'one'), ('a', 'two'), ('b', 'one'), ('b', 'two')]]
    await asyncio.sleep(0)
    held()
    await asyncio.gather(*tasks)
    assert [p for p, _ in order] == ['b', 'a', 'b', 'a']
    assert pool.active == 0


async def test_cancelled_waiters_do_not_lose_slots():
    pool = FairPool(1)
    release = await pool.acquire('a', 'one')
    cancelled = asyncio.create_task(pool.acquire('b', 'one'))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release()
    next_release = await asyncio.wait_for(pool.acquire('c', 'one'), 1)
    assert pool.active == 1
    next_release()
    next_release()  # Idempotent leases.
    assert pool.active == 0 and not pool._waiting


async def test_scoped_process_limit_and_exception_cleanup():
    limits = ResourceLimits(max_processes=1)
    token = current_scope.set(ResourceScope(limits, 'a', 'one'))
    try:
        with pytest.raises(RuntimeError):
            async with process_slot():
                assert limits.processes.active == 1
                raise RuntimeError('failed')
        assert limits.processes.active == 0
    finally:
        current_scope.reset(token)
