"""Volatile kernel ownership and eviction (kernel plan §3.4, Phase 1).

No events, disk snapshots, replay or runtime registration. Keys are exactly
(session_id, agent_path). Active and queued operations pin entries; admission
fails rather than evicting them. The global lock covers bookkeeping/checkout
only, never a cell, so independent entries execute concurrently.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time

from .kernel_host import (HostLost, KernelBusy, KernelLost, MAX_SNAPSHOT_BYTES,
                          SnapshotInvalid, _finish)


@dataclass
class _Entry:
    handle: object = None
    snapshot: bytes | None = None
    lost: bool = False
    touched: float = 0
    pins: int = 0
    closing: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle: asyncio.Event = field(default_factory=asyncio.Event)


class KernelRegistry:
    """Minimal internal API: start, feed, evict_idle, close, shutdown.

    A lost kernel stays lost until explicitly closed and started again. Recovery
    of a crashed generation is intentionally not transparent: even an available
    checkpoint can omit side effects or failed-cell mutations. Eviction restores
    only a just-dumped snapshot. No authority/callback object is kept on entries.
    The caller must periodically invoke evict_idle; there is no background task.
    """

    def __init__(self, host, *, max_live=4, max_entries=128,
                 max_snapshot_bytes=MAX_SNAPSHOT_BYTES,
                 max_total_snapshot_bytes=16 * MAX_SNAPSHOT_BYTES,
                 idle_seconds=900, clock=time.monotonic):
        if (type(max_live) is not int or type(max_entries) is not int
                or not 1 <= max_live <= host.max_kernels or not max_live <= max_entries <= 10000):
            raise ValueError('invalid entry capacities')
        if (type(max_snapshot_bytes) is not int
                or not 1 <= max_snapshot_bytes <= MAX_SNAPSHOT_BYTES):
            raise ValueError('invalid snapshot capacity')
        if (type(max_total_snapshot_bytes) is not int
                or max_total_snapshot_bytes < max_snapshot_bytes
                or not 0 < idle_seconds < float("inf")):
            raise ValueError('invalid cache bounds')
        self.host = host
        self.max_live = max_live
        self.max_entries = max_entries
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_total_snapshot_bytes = max_total_snapshot_bytes
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._entries = {}
        self._guard = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _key(session_id, agent_path):
        if (not isinstance(session_id, str) or not isinstance(agent_path, str)
                or not session_id or not agent_path
                or len(session_id) > 512 or len(agent_path) > 512):
            raise ValueError('bounded nonempty session_id and agent_path required')
        return session_id, agent_path

    def _check(self):
        if self._closed:
            raise KernelLost('registry is closed')

    def _pin(self, key, *, create=False):
        # Event-loop-local bookkeeping must not suspend behind eviction I/O.
        self._check()
        entry = self._entries.get(key)
        if entry is None:
            if not create:
                raise KernelLost('kernel not started')
            if len(self._entries) >= self.max_entries:
                raise KernelBusy('entry cap reached; close an existing kernel')
            entry = self._entries[key] = _Entry(touched=self.clock())
        if entry.closing:
            raise KernelBusy('kernel is closing')
        entry.idle.clear()
        entry.pins += 1
        return entry

    def _unpin(self, entry):
        entry.pins -= 1
        if not entry.pins:
            entry.idle.set()
        entry.touched = self.clock()

    async def _invalidate(self, exc, entry):
        targets = ([e for e in self._entries.values()
                    if e.handle is not None
                    and getattr(e.handle, 'generation', None) == exc.generation]
                   if isinstance(exc, HostLost) else (entry,))
        handles = []
        for target in targets:
            if target.handle is not None or target is entry:
                if target.handle is not None:
                    handles.append(target.handle)
                target.handle = None
                target.snapshot = None
                target.lost = True
        for handle in handles:
            try:
                await _finish(self.host.close(handle))
            except Exception:
                pass

    async def _evict(self, entry):
        # Caller holds guard and only chooses unpinned entries. Never close on
        # dump failure or cache overflow: doing so would silently lose state.
        entry.closing = True
        try:
            cancellations = asyncio.current_task().cancelling()
            snapshot = await _finish(self.host.dump(entry.handle))
            # Dump is read-only: cancellation waits for it, then leaves the
            # original worker live rather than aborting it or committing eviction.
            if asyncio.current_task().cancelling() > cancellations:
                raise asyncio.CancelledError
            if type(snapshot) is not bytes or len(snapshot) > self.max_snapshot_bytes:
                raise KernelBusy('snapshot too large; kernel remains live')
            total = sum(len(e.snapshot or b'') for e in self._entries.values()
                        if e is not entry)
            if total + len(snapshot) > self.max_total_snapshot_bytes:
                raise KernelBusy('snapshot cache full; kernel remains live')
            # Once dumped, finish release even if the caller is cancelled.
            # A worker-local close failure still leaves a restorable checkpoint;
            # a generation failure deliberately invalidates all its state.
            handle = entry.handle
            cancellations = asyncio.current_task().cancelling()
            try:
                await _finish(self.host.close(handle))
            except HostLost as exc:
                await self._invalidate(exc, entry)
                raise
            except (KernelLost, TimeoutError):
                pass
            entry.handle = None
            entry.snapshot = snapshot
            if asyncio.current_task().cancelling() > cancellations:
                raise asyncio.CancelledError
        except (HostLost, KernelLost, TimeoutError) as exc:
            if entry.handle is not None:
                await self._invalidate(exc, entry)
            raise
        finally:
            entry.closing = False

    async def _ensure(self, entry):
        self._check()
        if entry.lost:
            raise KernelLost('kernel lost; explicitly close and start again')
        if entry.handle is not None:
            return
        async with self._guard:
            self._check()
            if entry.lost:
                raise KernelLost('kernel lost; explicitly close and start again')
            if entry.handle is not None:
                return
            live = [e for e in self._entries.values() if e.handle is not None]
            if len(live) >= self.max_live:
                candidates = [e for e in live if not e.pins]
                if not candidates:
                    raise KernelBusy('all live kernels are active')
                for candidate in sorted(candidates, key=lambda e: e.touched):
                    # Earlier eviction I/O may let a later candidate acquire pins.
                    if candidate.handle is None or candidate.pins:
                        continue
                    try:
                        await self._evict(candidate)
                        break
                    except (KernelBusy, KernelLost, TimeoutError):
                        continue
                    except HostLost:
                        # The candidate, not the incoming entry, owned failure.
                        # Its generation has already been invalidated.
                        continue
                else:
                    if sum(e.handle is not None for e in self._entries.values()) >= self.max_live:
                        raise KernelBusy('no live kernel can be snapshotted')
            handle = None
            try:
                handle = await self.host.open()
                if entry.snapshot is not None:
                    await self.host.load(handle, entry.snapshot)
                entry.handle = handle
                entry.snapshot = None
            except BaseException as exc:
                if isinstance(exc, HostLost):
                    await self._invalidate(exc, entry)
                if handle is not None:
                    try:
                        await _finish(self.host.close(handle))
                    except HostLost as close_exc:
                        await self._invalidate(close_exc, entry)
                    except Exception:
                        pass
                # A fresh worker is disposable; a valid checkpoint is not.
                # Only an explicitly rejected snapshot makes this entry lost.
                if isinstance(exc, SnapshotInvalid):
                    entry.snapshot = None
                    entry.lost = True
                raise

    async def start(self, session_id, agent_path, snapshot=None):
        """Open the worker; with ``snapshot`` (checkpoint bytes the caller verified) restore into it.

        A rejected snapshot (``SnapshotInvalid``) marks the entry lost; the caller closes it.
        """
        key = self._key(session_id, agent_path)
        entry = self._pin(key, create=True)
        try:
            async with entry.lock:
                if snapshot is not None and entry.handle is None and not entry.lost:
                    if type(snapshot) is not bytes or len(snapshot) > self.max_snapshot_bytes:
                        raise ValueError('snapshot exceeds the configured cap')
                    entry.snapshot = snapshot
                await self._ensure(entry)
        finally:
            self._unpin(entry)
            if (not entry.pins and entry.handle is None
                    and entry.snapshot is None and not entry.lost):
                self._entries.pop(key, None)

    async def feed(self, session_id, agent_path, program, *, functions, inputs=None,
                   deadline=None):
        entry = self._pin(self._key(session_id, agent_path))
        try:
            async with entry.lock:
                await self._ensure(entry)
                try:
                    return await self.host.feed(entry.handle, program,
                                                functions=functions, inputs=inputs,
                                                deadline=deadline)
                except (HostLost, KernelLost, asyncio.CancelledError, TimeoutError) as exc:
                    await self._invalidate(exc, entry)
                    raise
        finally:
            self._unpin(entry)

    async def dump(self, session_id, agent_path):
        """The kernel's current snapshot bytes (a checkpoint source; plan code-mode-kernel §3.8).

        ``KernelBusy`` means the snapshot exceeds the frame budget; the kernel stays live.
        """
        entry = self._pin(self._key(session_id, agent_path))
        try:
            async with entry.lock:
                await self._ensure(entry)
                try:
                    return await _finish(self.host.dump(entry.handle))
                except (HostLost, KernelLost, asyncio.CancelledError, TimeoutError) as exc:
                    await self._invalidate(exc, entry)
                    raise
        finally:
            self._unpin(entry)

    async def refresh(self, session_id, agent_path):
        """Mark a live kernel lost when its helper process has died (found before the next feed)."""
        entry = self._entries.get(self._key(session_id, agent_path))
        alive = getattr(self.host, 'alive', None)
        handle = None if entry is None else entry.handle
        if handle is None or alive is None or entry.pins or alive(getattr(handle, 'generation', None)):
            return
        await self._invalidate(HostLost('sandbox helper exited', getattr(handle, 'generation', None)), entry)

    def status(self, session_id, agent_path):
        """``live`` | ``evicted`` | ``lost`` for a known kernel, else ``None``."""
        entry = self._entries.get(self._key(session_id, agent_path))
        if entry is None:
            return None
        if entry.lost:
            return 'lost'
        return 'live' if entry.handle is not None else 'evicted'

    async def evict_idle(self):
        async with self._guard:
            self._check()
            now = self.clock()
            blocked = False
            for entry in sorted(self._entries.values(), key=lambda e: e.touched):
                if (entry.handle is not None and not entry.pins
                        and now - entry.touched >= self.idle_seconds):
                    try:
                        await self._evict(entry)
                    except KernelBusy:
                        blocked = True
            if blocked:
                raise KernelBusy('some idle kernels could not be snapshotted')

    async def close(self, session_id, agent_path):
        key = self._key(session_id, agent_path)
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                return
            if entry.pins:
                raise KernelBusy('cannot close an active or queued kernel')
            entry.closing = True
            cancellations = asyncio.current_task().cancelling()
            try:
                if entry.handle is not None:
                    try:
                        await _finish(self.host.close(entry.handle))
                    except HostLost as exc:
                        await self._invalidate(exc, entry)
                        del self._entries[key]
                        raise
                    except (KernelLost, TimeoutError):
                        # Host close aborts the worker before reporting these.
                        del self._entries[key]
                        raise
                del self._entries[key]
                if asyncio.current_task().cancelling() > cancellations:
                    raise asyncio.CancelledError
            finally:
                entry.closing = False

    async def shutdown(self):
        # Host shutdown terminates in-flight operations; they unwind their own
        # pins. No active eviction or dump occurs during terminal shutdown.
        self._closed = True
        entries = list(self._entries.values())
        try:
            await self.host.shutdown()
        finally:
            async with self._guard:
                self._entries.clear()
            # Every operation owns a pin through its final cleanup.
            await asyncio.gather(*(entry.idle.wait() for entry in entries if entry.pins))
