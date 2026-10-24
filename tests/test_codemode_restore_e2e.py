"""Restart and crash recovery end to end on real Monty (kernel plan §3.8, §6).

Run cells, lose the runtime (or only the kernel host), and the next ``code_run`` sees the restored
variables and begins with the notice. Kernel metadata comes back from the session log.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip('pydantic_monty')

from test_codemode_snapshots import durable_scope  # noqa: E402
from test_codemode_tool import text_of  # noqa: E402


@pytest.fixture
async def durable(tmp_path):
    async with durable_scope(tmp_path) as make:
        yield make


async def test_restart_restores_variables_attachments_and_numbering(durable):
    d = durable()
    r = await d.rig()
    await r.cell('import json\nx = 41\ndef bump(v):\n    return v + 1\n')
    await r.cell('y = bump(x)')
    started = r.of('code.kernel_started')[0]
    old_id = started['kernel_id']
    attached = set(r.kernels.get('s1', 'root').attached)

    r2 = await d.restart(r)                       # the daemon restarted: no workers, no states
    assert r2.kernels.get('s1', 'root') is None
    result = await r2.cell("json.dumps({'y': y, 'x': bump(x)})")
    body = text_of(result)
    assert not result.is_error
    assert body.startswith('Kernel restored from after In [2]; no later cells had run.')
    assert """Out [3]: '{"y": 42, "x": 42}'""" in body       # variables, a function and an import came back
    state = r2.kernels.get('s1', 'root')
    assert state.kernel_id == old_id and set(state.attached) == attached and state.n == 3
    assert [e['reason'] for e in r2.of('code.kernel_restored')] == ['restart']
    # The notice is for the first call only.
    assert not text_of(await r2.cell('x')).startswith('Kernel restored')
    assert len(r2.of('code.kernel_restored')) == 1


async def test_a_second_restart_keeps_working_and_chained_checkpoints_stay_consistent(durable):
    d = durable()
    r = await d.rig()
    await r.cell('a = 1')
    r = await d.restart(r)
    await r.cell('b = a + 1')
    r = await d.restart(r)
    body = text_of(await r.cell('a + b'))
    assert 'Out [3]: 3' in body and body.startswith('Kernel restored from after In [2]')


async def test_code_start_after_a_restart_describes_the_restored_kernel_instead_of_starting_a_new_one(durable):
    d = durable()
    r = await d.rig()
    await r.cell('items = [1, 2, 3]')
    old_id = r.kernels.get('s1', 'root').kernel_id
    r2 = await d.restart(r)
    text = text_of(await r2.call('code_start', {}))
    assert f'Kernel {old_id} is already running' in text
    assert 'Kernel restored from after In [1]' in text and 'items' in text
    assert not r2.of('code.kernel_started')[1:]
    assert len(r2.of('code.kernel_started')) == 1


async def test_a_killed_kernel_host_restores_on_the_next_call(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 7')
    await r.cell("x = 8\n1 / 0")
    host = r.kernels.host
    host._proc.kill()
    for _ in range(100):
        if host._proc is None:
            break
        await asyncio.sleep(0.05)
    body = text_of(await r.cell('x'))
    assert body.startswith('Kernel restored from after In [1]. In [2] ran after that snapshot')
    assert 'Out [3]: 7' in body
    assert r.of('code.kernel_restored')[-1]['reason'] == 'host_crash'
    assert 'Out [4]: 7' in text_of(await r.cell('x'))


async def test_a_timeout_with_a_checkpoint_restores_on_the_next_call(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 5')
    result = await r.cell('x = 6\nwhile True:\n    pass', timeout_s=1)
    assert result.is_error and 'next call restores' in text_of(result) and 'after In [1]' in text_of(result)
    assert not r.of('code.kernel_reset')
    body = text_of(await r.cell('x'))
    assert body.startswith('Kernel restored from after In [1]. In [2] ran after') and 'Out [3]: 5' in body


async def test_no_checkpoint_and_nothing_to_lose_restarts_silently(durable):
    d = durable()
    r = await d.rig()           # started, no cell ran
    r2 = await d.restart(r)
    assert text_of(await r2.cell('1 + 1')).startswith('In [1]  ✓')
    assert not r2.of('code.kernel_lost') and not r2.of('code.kernel_restored')


async def test_a_stopped_kernel_is_not_resurrected_by_a_restart(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    await r.call('code_reset', {'stop': True})
    r2 = await d.restart(r)
    assert text_of(await r2.cell('x')).startswith('KernelNotStarted')


async def test_context_targets_other_than_self_need_approval_again_after_a_restart(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    # A target approved before the restart is a grant of the old process, never read back from the log.
    r.events.append(('code.kernel_attached', {'kernel_id': r.kernels.get('s1', 'root').kernel_id,
                                              'attached': {'tools': [], 'targets': [{'ref': 'notes'}]}}))
    r2 = await d.restart(r)
    body = text_of(await r2.cell('x'))
    assert 'Context targets need approval again' in body and 'notes' in body
    assert r2.kernels.get('s1', 'root').approved == {}


# -- a real Runtime: the store, the key and the log reader are wired by the runtime itself ----------


async def test_a_new_runtime_restores_a_session_from_the_shared_database(tmp_path):
    from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
    from nexus.runtime import Runtime
    from test_codemode_e2e import drain, make_config, of, result_text

    def runtime(*steps):
        return Runtime(tmp_path, config=make_config(), providers={"scripted": ScriptedProvider(*steps)})

    first = runtime(tool_response(("s1", "code_start", {"purpose": "t"})),
                    tool_response(("k1", "code_run", {"cell": "total = 40 + 2", "purpose": "t"})),
                    text_response("done"))
    session = first.session("durable")
    await drain(session, "go")
    db = first._state_db
    assert db is not None
    key = db.path.parent / "code-kernel.key"
    assert key.exists() and (key.stat().st_mode & 0o777) == 0o600
    with db.read() as conn:
        assert conn.execute("SELECT after_n FROM code_kernel_snapshots").fetchall()[0][0] == 1
    await first.aclose()                                    # the daemon goes away; its workers die with it

    second = runtime(tool_response(("k2", "code_run", {"cell": "total", "purpose": "t"})), text_response("done"))
    try:
        session = second.session("durable")
        events = await drain(session, "again")
        text = result_text(session)
        assert text.startswith("Kernel restored from after In [1]; no later cells had run."), text
        assert "Out [2]: 42" in text
        assert [e.data["reason"] for e in of(events, "code.kernel_restored")] == ["restart"]
        # Deleting the session removes the checkpoint.
        second.release_code_session("durable")
        with second._state_db.read() as conn:
            assert conn.execute("SELECT count(*) FROM code_kernel_snapshots").fetchone()[0] == 0
    finally:
        await second.aclose()
