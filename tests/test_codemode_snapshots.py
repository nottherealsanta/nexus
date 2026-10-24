"""Signed kernel checkpoints: when they are taken, what refuses them, retention (kernel plan §3.8, §6)."""
from __future__ import annotations

import contextlib
import os
import stat

import pytest

pytest.importorskip('pydantic_monty')

from nexus.session.db import StateDatabase  # noqa: E402
from nexus.tools.codemode.cell import monty_version  # noqa: E402
from nexus.tools.codemode.snapshots import KEY_NAME, SnapshotStore, load_key, sign  # noqa: E402
from test_codemode_tool import Rig, config, ok, text_of  # noqa: E402


def make_store(tmp_path):
    db = StateDatabase(tmp_path / 'nexus.db')
    return SnapshotStore(db, load_key(tmp_path / KEY_NAME))


class Durable:
    """Rigs over one store and one event log: ``restart()`` is a new runtime over the same state."""

    def __init__(self, tmp_path, **cfg):
        self.store = make_store(tmp_path)
        self.events = []
        self.cfg = config(**cfg) if cfg else None
        self.rigs = []

    async def rig(self, *, start=True):
        r = Rig(real=True, cfg=self.cfg, store=self.store, events=self.events)
        r.outcomes.update({'read': lambda args: ok('contents of ' + args['path'])})
        self.rigs.append(r)
        if start:
            await r.start()
        return r

    async def restart(self, rig):
        """A daemon restart: the old runtime is gone, a new one starts from the log and the store."""
        await rig.aclose()
        return await self.rig(start=False)

    async def aclose(self):
        for r in self.rigs:
            await r.aclose()


@contextlib.asynccontextmanager
async def durable_scope(tmp_path):
    """``make(**config)`` -> a ``Durable``; every one is closed on exit (each test file wraps this in a fixture)."""
    made = []

    def make(**cfg):
        d = Durable(tmp_path, **cfg)
        made.append(d)
        return d

    try:
        yield make
    finally:
        for d in made:
            await d.aclose()


@pytest.fixture
async def durable(tmp_path):
    async with durable_scope(tmp_path) as make:
        yield make


def rows(store):
    with store.db.read() as conn:
        return conn.execute('SELECT session_id, kernel_id, after_n, monty_version FROM code_kernel_snapshots '
                            'ORDER BY after_n').fetchall()


def kernel_id(r):
    return r.kernels.get('s1', 'root').kernel_id


def test_the_key_is_created_once_with_mode_0600(tmp_path):
    path = tmp_path / 'home' / KEY_NAME
    key = load_key(path)
    assert len(key) == 32 and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_key(path) == key


def test_the_mac_binds_every_field_and_the_bytes():
    key = b'k' * 32
    base = sign(key, 's', 'k', 3, '1.1.0', b'data')
    for changed in (sign(key, 's2', 'k', 3, '1.1.0', b'data'), sign(key, 's', 'k2', 3, '1.1.0', b'data'),
                    sign(key, 's', 'k', 4, '1.1.0', b'data'), sign(key, 's', 'k', 3, '1.1.1', b'data'),
                    sign(key, 's', 'k', 3, '1.1.0', b'datb'), sign(b'j' * 32, 's', 'k', 3, '1.1.0', b'data')):
        assert changed != base
    # Field boundaries are length-prefixed: moving a byte between fields changes the MAC.
    assert sign(key, 'ab', 'c', 1, 'v', b'') != sign(key, 'a', 'bc', 1, 'v', b'')


async def test_a_checkpoint_follows_every_successful_cell_and_only_those(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    assert [row['after_n'] for row in rows(d.store)] == [1]
    failed = await r.cell('x = 2\n1 / 0')
    assert failed.is_error
    assert [row['after_n'] for row in rows(d.store)] == [1]      # a failed cell takes none
    await r.cell('x = 3')
    # Only the newest row per kernel is kept.
    assert [row['after_n'] for row in rows(d.store)] == [3]
    written = [e for e in r.of('code.kernel_checkpointed')]
    assert [(e['after_n'], e['status']) for e in written] == [(1, 'written'), (3, 'written')]
    assert all(e['bytes'] > 0 and len(e['sha256']) == 64 and 'snapshot' not in e for e in written)
    # Order in the log: cell_finished precedes its checkpoint.
    kinds = [k for k, _ in r.events if k in ('code.cell_finished', 'code.kernel_checkpointed')]
    assert kinds == ['code.cell_finished', 'code.kernel_checkpointed', 'code.cell_finished',
                     'code.cell_finished', 'code.kernel_checkpointed']


async def test_an_oversized_snapshot_writes_nothing_and_the_previous_checkpoint_stays(durable):
    d = durable(max_snapshot_bytes=64 * 1024)
    r = await d.rig()
    await r.cell('x = 1')
    await r.cell("big = 'y' * 200000")
    events = r.of('code.kernel_checkpointed')
    assert events[1]['status'] == 'too_large' and events[1]['kept_after_n'] == 1
    assert events[1]['max_snapshot_bytes'] == 64 * 1024 and events[1]['bytes'] > 64 * 1024
    assert [row['after_n'] for row in rows(d.store)] == [1]
    r2 = await d.restart(r)
    body = text_of(await r2.cell('x'))
    assert body.startswith('Kernel restored from after In [1]. In [2] ran after that snapshot')


@pytest.mark.parametrize('tamper, reason', [
    ("UPDATE code_kernel_snapshots SET bytes = x'00ff'", 'invalid'),
    ('UPDATE code_kernel_snapshots SET after_n = after_n - 1', 'invalid'),
    ("UPDATE code_kernel_snapshots SET monty_version = '0.0.1'", 'invalid'),
    ("UPDATE code_kernel_snapshots SET mac = x'00'", 'invalid'),
])
async def test_a_tampered_snapshot_is_refused_as_kernel_lost(durable, tamper, reason):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    await r.cell('x = 2')
    with d.store.db.transaction() as conn:
        conn.execute(tamper)
    r2 = await d.restart(r)
    result = await r2.cell('x')
    body = text_of(result)
    assert result.is_error and body.startswith(f'KernelLost ({reason})') and 'was not run' in body
    lost = r2.of('code.kernel_lost')[-1]
    assert lost['reason'] == reason and lost['last_n'] == 2
    assert not r2.of('code.kernel_restored')
    # Reset empty, attachments kept, numbering restarts, the unusable row is gone.
    state = r2.kernels.get('s1', 'root')
    assert state.n == 0 and 'read' in state.attached and rows(d.store) == []
    assert text_of(await r2.cell('y = 5\ny')).startswith('In [1]')


async def test_a_validly_signed_snapshot_of_another_monty_version_is_refused(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    row = d.store.get('s1', kernel_id(r))
    d.store.put('s1', row.kernel_id, row.after_n, '0.0.1-other', row.data)   # signed, but a different Monty
    assert monty_version() != '0.0.1-other'
    r2 = await d.restart(r)
    assert text_of(await r2.cell('x')).startswith('KernelLost (version)')
    assert r2.of('code.kernel_lost')[-1]['reason'] == 'version'


async def test_a_snapshot_under_the_wrong_kernel_id_is_refused(durable):
    d = durable()
    r = await d.rig()
    await r.call('code_start', {}, agent_id='child-1')
    await r.cell('mine = 1')
    await r.cell('theirs = 2', agent_id='child-1')
    root, child = r.kernels.get('s1', 'root').kernel_id, r.kernels.get('s1', 'child-1').kernel_id
    with d.store.db.transaction() as conn:   # the child's signed row, relabelled as the root's
        conn.execute('DELETE FROM code_kernel_snapshots WHERE kernel_id=?', (root,))
        conn.execute('UPDATE code_kernel_snapshots SET kernel_id=? WHERE kernel_id=?', (root, child))
    r2 = await d.restart(r)
    assert text_of(await r2.cell('mine')).startswith('KernelLost (invalid)')


async def test_no_checkpoint_at_all_is_missing_and_too_large_is_reported_as_such(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    d.store.delete_kernel('s1', kernel_id(r))
    r2 = await d.restart(r)
    assert text_of(await r2.cell('x')).startswith('KernelLost (missing)')
    # The first checkpoint was already too large: nothing was ever stored.
    d2 = durable(max_snapshot_bytes=64 * 1024)
    r3 = await d2.rig()
    await r3.cell("big = 'y' * 200000")
    r4 = await d2.restart(r3)
    assert text_of(await r4.cell('1')).startswith('KernelLost (too_large)')
    assert r4.of('code.kernel_lost')[-1]['reason'] == 'too_large'


async def test_restore_lists_the_cells_that_ran_after_the_snapshot(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    await r.cell('x = 2\n1 / 0')
    await r.cell('x = 3\n1 / 0')
    r2 = await d.restart(r)
    body = text_of(await r2.cell('x'))
    assert body.startswith('Kernel restored from after In [1]. In [2]–[3] ran after that snapshot; '
                           'variables they set are gone.')
    assert 'In [4]  ✓' in body and 'Out [4]: 1' in body       # numbering continues; cells are never re-run
    restored = r2.of('code.kernel_restored')[0]
    assert (restored['from_n'], restored['reason'], restored['missing_cells']) == (1, 'restart', [2, 3])


async def test_reset_and_stop_delete_the_checkpoint(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    assert len(rows(d.store)) == 1
    await r.call('code_reset', {})
    assert rows(d.store) == []
    r2 = await d.restart(r)
    body = text_of(await r2.cell('x'))
    assert 'NameError' in body and not r2.of('code.kernel_restored') and not r2.of('code.kernel_lost')
    await r2.cell('z = 1')
    await r2.call('code_reset', {'stop': True})
    assert rows(d.store) == []


async def test_closing_or_deleting_a_session_deletes_its_snapshots(durable):
    d = durable()
    r = await d.rig()
    await r.cell('x = 1')
    d.store.put('other', 'k-other', 1, monty_version(), b'z')
    await r.kernels.close_session('s1')
    assert [row['session_id'] for row in rows(d.store)] == ['other']
    r2 = await d.rig(start=False)
    await r2.call('code_start', {})
    await r2.cell('y = 1')
    assert len(rows(d.store)) == 2
    r2.kernels.release_session('s1')            # session deletion (sync entry point)
    assert [row['session_id'] for row in rows(d.store)] == ['other']


async def test_a_large_snapshot_crosses_the_helper_frame_budget(durable):
    d = durable()
    r = await d.rig()
    await r.cell("data = 'x' * 3000000")
    [row] = rows(d.store)
    assert d.store.get('s1', row['kernel_id']).data.__len__() > 2 * 1024 * 1024
    r2 = await d.restart(r)
    assert 'Out [2]: 3000000' in text_of(await r2.cell('len(data)'))
