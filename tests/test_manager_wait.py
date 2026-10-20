"""Manager wait lease regressions: exit paths never cancel the observed root."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from test_manager_launch import setup


@pytest.mark.parametrize("exit_kind", ["timeout", "interruption", "cancellation", "visibility_error"])
async def test_wait_releases_lease_without_cancelling_target(tmp_path, exit_kind):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    leases = []
    entered = asyncio.Event()

    @asynccontextmanager
    async def lend_slot(session_id):
        leases.append(session_id)
        entered.set()
        try:
            yield
        finally:
            leases.remove(session_id)

    host.lend_slot = lend_slot
    try:
        target = (await port.launch("keep working", "m1")).id
        if exit_kind == "timeout":
            # Advance the injected clock, not wall time, beyond the minimum bound.
            ticks = iter((0.0, 2.0))
            port._monotonic = lambda: next(ticks)
        ids = ("missing-target",) if exit_kind == "visibility_error" else (target,)
        task = asyncio.create_task(port.wait(ids, "all", 1))
        await entered.wait()
        if exit_kind == "interruption":
            proxy.queued_ids = ("new-human-message",)
        elif exit_kind == "cancellation":
            task.cancel()
        if exit_kind == "cancellation":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_kind == "visibility_error":
            from nexus.tools.manager_port import SessionNotVisible
            with pytest.raises(SessionNotVisible):
                await task
        else:
            result = await asyncio.wait_for(task, 5)
            assert result.running == (target,)
            assert result.interrupted == (exit_kind == "interruption")
        assert leases == []
        assert host.cancelled == []
        assert (await port.status((target,)))[0].outcome is None
    finally:
        root.close_all()
        agents.close_all()


async def test_real_cap_one_launch_wait_and_interrupt(tmp_path):
    """A real manager turn lends the only supervisor slot to its launched root."""
    from nexus.host import HostFacade
    from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response
    from nexus.runtime import Runtime
    from test_manager_host import config
    from manager_helpers import MANAGER_ID

    started = asyncio.Event()
    resumed = asyncio.Event()
    release = asyncio.Event()
    target = []
    results = []

    async def manage(_request):
        # The registered scripted adapter receives the pinned manager model.
        assert _request.model == 'gpt-6-luna'
        port = facade._manager_port(runtime.session(MANAGER_ID))
        port._poll_interval = 0.01
        messages = __import__('nexus.host_support.manager_port', fromlist=['scan_manager_log']).scan_manager_log(
            runtime.session(MANAGER_ID).read(force=True).records).messages
        target.append((await port.launch('keep working', messages[-1].id)).id)
        results.append(await port.wait((target[0],), 'all', 10))
        resumed.set()
        return text_response('resumed')

    def child(_request):
        started.set()
        return ()

    runtime = Runtime(tmp_path, home=tmp_path / 'home', config=config(True),
                      providers={'codex': ScriptedProvider([manage], text_response('ok')),
                                 'scripted': ScriptedProvider([child, Wait(release), *text_response('done')])})
    facade = HostFacade(runtime, max_concurrent_turns=1)
    try:
        facade.open_manager()
        # Deliberately unattended: a manager launch needs no approval, so none is requested.
        await facade.enqueue(MANAGER_ID, 'launch work')
        manager_task = runtime.session(MANAGER_ID)._turn_task
        try:
            await asyncio.wait_for(started.wait(), 5)
        except TimeoutError:
            if manager_task.done():
                outcome, = await asyncio.gather(manager_task, return_exceptions=True)
                pytest.fail(f'child did not start; manager outcome: {outcome!r}')
            raise
        assert facade.supervisor.running_for(target[0]) == 1
        assert MANAGER_ID in facade.supervisor._lent
        await facade.enqueue(MANAGER_ID, 'new human instruction')
        await asyncio.wait_for(resumed.wait(), 5)
        assert results[0].interrupted
        assert results[0].running == (target[0],)
        assert MANAGER_ID not in facade.supervisor._lent
        assert facade.supervisor.running_for(target[0]) == 1
        release.set()
        await facade.wait_idle(timeout=5)
    finally:
        release.set()
        await runtime.aclose()
