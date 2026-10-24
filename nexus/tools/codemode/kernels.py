"""Kernel ownership above the registry and tool selection (plan code-mode-kernel §3.2, §3.4, §3.6).

``CodeKernels`` is what the runtime owns (it outlives turns and per-iteration tool
managers): one ``KernelHost`` and ``KernelRegistry`` plus a ``KernelState`` per
``(session_id, agent_path)``. A state records what the model asked for (attached
callable names, approved context targets, the ``In [n]`` counter); it is never
authority. Every cell rebuilds its callable set from the *current* iteration's
catalog intersected with ``attached`` (``cell.py``).

``select`` turns a ``servers`` / ``tools`` / ``query`` request into attachments plus
a "Not attached" list with a reason per name. Server and query selection is capped by
the ``[code_mode] expose`` ceiling; an explicitly named tool may be mutating (the
selection is not a grant: calls still pass the current catalog and permissions).

Durability (plan code-mode-kernel §3.8): with a ``SnapshotStore`` every successful cell is
checkpointed (``after_cell``), and ``prepare`` is the one restore path run before the first
``code_run`` / ``code_start`` on a kernel that is not live: after a daemon restart (the state is
rebuilt from the session log by ``resolve``), a kernel-host crash, or an eviction. It verifies the
HMAC, Monty version and kernel id, loads the snapshot into a fresh worker, and reports the cells that
ran after it as ``missing_cells``. Cells are never re-run. Without a usable checkpoint the kernel is
reset empty (attachments kept) and ``code.kernel_lost`` says why.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from types import SimpleNamespace
from typing import Any

from ...mcp.search import SearchIndex
from ..names import canonical_tool_name, tool_rule_names
from .catalog import (BUILTIN_NAMESPACE, EXCLUDED, EXCLUDED_REASON, build_catalog, excluded_reason,
                      namespace)
from .kernel_host import MAX_SNAPSHOT_BYTES, KernelBusy, KernelHost, SnapshotInvalid
from .registry import KernelRegistry
from ...util import redact_secrets

MAX_SERVERS = 32
MAX_TOOLS = 256
MAX_QUERY = 256
MAX_TARGETS = 50
QUERY_LIMIT = 8
MAX_LISTED_NAMES = 8


@dataclass
class KernelState:
    """What one kernel was asked for. ``attached`` maps callable name to spec name."""

    kernel_id: str
    session_id: str
    agent_path: str
    attached: dict[str, str] = field(default_factory=dict)
    #: Context targets approved for this kernel: ref -> stored session id.
    approved: dict[str, str] = field(default_factory=dict)
    target_info: list[dict[str, Any]] = field(default_factory=list)
    n: int = 0
    purpose: str = ''
    started: float = field(default_factory=time.time)
    last_cell: float | None = None
    #: ``after_n`` of the stored checkpoint, or ``None`` (none, or invalidated by a reset).
    checkpoint_after: int | None = None
    #: Outcome of the latest checkpoint attempt: ``written | too_large | failed`` or ``''``.
    checkpoint_status: str = ''
    #: True when the state was rebuilt from the session log (the worker is gone).
    from_log: bool = False
    #: Context targets that were approved before a restart and need approval again.
    dropped_targets: list[str] = field(default_factory=list)


MAX_MISSING_LISTED = 200


@dataclass(frozen=True)
class Prepared:
    """Result of ``CodeKernels.prepare``: a notice to start the result with, or a lost reason."""

    notice: str = ''
    lost: str | None = None


def missing_range(after_n: int, last_n: int) -> str:
    """``In [8]–[9]`` / ``In [8]`` for the cells after a checkpoint (``last_n`` the newest)."""
    first, last = after_n + 1, last_n
    return f'In [{first}]' if first == last else f'In [{first}]–[{last}]'


def restore_notice(after_n: int, last_n: int) -> str:
    """The sentence a restored kernel's first result begins with (plan code-mode-kernel §3.8)."""
    if last_n <= after_n:
        return f'Kernel restored from after In [{after_n}]; no later cells had run.'
    return (f'Kernel restored from after In [{after_n}]. {missing_range(after_n, last_n)} ran after that '
            'snapshot; variables they set are gone.')


def lost_text(reason: str, last_n: int) -> str:
    detail = {'too_large': 'its latest snapshot was over max_snapshot_bytes, so none was kept',
              'invalid': 'its snapshot failed verification',
              'version': 'its snapshot was written by a different Monty version',
              'missing': 'no snapshot of it exists'}.get(reason, reason)
    return (f'KernelLost ({reason}): the kernel worker is gone and {detail}; its variables '
            f'(through In [{last_n}]) cannot be recovered. The kernel was reset empty: In [n] restarts at 1 and '
            'attachments are kept. Cells are never re-run automatically.')


@dataclass(frozen=True)
class Selection:
    """Resolved attachments: ordered ``(heading, [callable names])`` plus skipped names."""

    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n for _, names in self.groups for n in names))


class CodeKernels:
    """Runtime-owned kernels; closing a session closes its kernels."""

    def __init__(self, config, *, context_provider=None, host=None, store=None, log_reader=None):
        max_nested = int(getattr(config, 'max_nested_calls', 64))
        self.config = config
        self.context_provider = context_provider
        #: ``SnapshotStore`` for checkpoints, or ``None`` (no durability; Phase 2 behavior).
        self.store = store
        #: ``log_reader(session_id)`` -> the session's ``code.*`` events (``Event`` objects), used to
        #: rebuild kernel metadata after a restart. ``None`` disables log reconstruction.
        self.log_reader = log_reader
        # A cell's Monty suspension budget needs headroom beyond ``max_nested_calls``
        # (a gather of n calls costs about n + 2); the loop's per-cell cap is authoritative.
        limits = {'max_suspensions': min(2 * max_nested + 16, 4096)}
        live = int(getattr(config, 'max_live_kernels', 4))
        #: ``[code_mode] max_snapshot_bytes`` clamped to the helper's frame budget (8 MiB).
        self.max_snapshot_bytes = max(1, min(int(getattr(config, 'max_snapshot_bytes', MAX_SNAPSHOT_BYTES)),
                                             MAX_SNAPSHOT_BYTES))
        self.host = host or KernelHost(max_kernels=live, limits=limits)
        self.registry = KernelRegistry(
            self.host, max_live=min(live, self.host.max_kernels),
            max_snapshot_bytes=self.max_snapshot_bytes,
            max_total_snapshot_bytes=4 * self.max_snapshot_bytes,
            idle_seconds=int(getattr(config, 'idle_evict_s', 900)))
        #: Concurrent feeds (``[code_mode] max_workers``).
        self.feeds = asyncio.Semaphore(max(1, int(getattr(config, 'max_workers', 2))))
        self._states: dict[tuple[str, str], KernelState] = {}
        self._closing: set[asyncio.Task] = set()
        #: ``await after_cell(state, n)`` runs after a successful cell and may return a
        #: ``code.kernel_checkpointed`` payload for the caller to emit. Failures are ignored;
        #: the cell result stands.
        self.after_cell = self.checkpoint if store is not None else None

    @property
    def active(self) -> bool:
        """Whether any kernel or helper process exists (nothing to close otherwise)."""
        return bool(self._states) or getattr(self.host, 'generation', None) is not None

    def get(self, session_id: str, agent_path: str) -> KernelState | None:
        return self._states.get((session_id, agent_path))

    def put(self, state: KernelState) -> None:
        self._states[(state.session_id, state.agent_path)] = state

    def drop(self, session_id: str, agent_path: str) -> None:
        self._states.pop((session_id, agent_path), None)

    def states(self, session_id: str | None = None) -> list[KernelState]:
        return [s for s in self._states.values() if session_id in (None, s.session_id)]

    async def open(self, state: KernelState) -> None:
        """Open (or restore) the worker behind ``state``; raises registry/host errors."""
        await self.registry.start(state.session_id, state.agent_path)

    async def restart(self, state: KernelState) -> None:
        """Replace a lost or cleared kernel with an empty one; attachments are kept."""
        with contextlib.suppress(Exception):
            await self.registry.close(state.session_id, state.agent_path)
        state.n = 0
        self.forget_checkpoint(state)
        with contextlib.suppress(Exception):
            await self.open(state)

    def forget_checkpoint(self, state: KernelState) -> None:
        """Delete the kernel's checkpoint (reset, stop or loss): it must never be restored again."""
        state.checkpoint_after = None
        state.checkpoint_status = ''
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.delete_kernel(state.session_id, state.kernel_id)

    async def checkpoint(self, state: KernelState, n: int) -> dict | None:
        """Dump, sign and store the kernel after cell ``n``; returns the ``code.kernel_checkpointed`` payload.

        Over ``max_snapshot_bytes`` nothing is written and the previous checkpoint stays
        (``status: too_large``, ``kept_after_n``). A failed dump or write reports ``status: failed``.
        """
        if self.store is None:
            return None
        from .cell import monty_version

        payload = {'kernel_id': state.kernel_id, 'after_n': n, 'bytes': None, 'sha256': '',
                   'status': 'failed', 'max_snapshot_bytes': self.max_snapshot_bytes,
                   'kept_after_n': state.checkpoint_after}
        try:
            data = await self.registry.dump(state.session_id, state.agent_path)
        except KernelBusy:
            payload['status'] = 'too_large'
            state.checkpoint_status = 'too_large'
            return payload
        except Exception as exc:  # noqa: BLE001 - reported in the event; the cell result stands
            payload['error'] = redact_secrets(f'{type(exc).__name__}: {exc}')[:300]
            state.checkpoint_status = 'failed'
            return payload
        payload['bytes'] = len(data)
        payload['sha256'] = hashlib.sha256(data).hexdigest()
        if len(data) > self.max_snapshot_bytes:
            payload['status'] = 'too_large'
            state.checkpoint_status = 'too_large'
            return payload
        try:
            self.store.put(state.session_id, state.kernel_id, n, monty_version(), data)
        except Exception as exc:  # noqa: BLE001
            payload['error'] = redact_secrets(f'{type(exc).__name__}: {exc}')[:300]
            state.checkpoint_status = 'failed'
            return payload
        payload['status'] = 'written'
        payload['kept_after_n'] = None
        state.checkpoint_after = n
        state.checkpoint_status = 'written'
        return payload

    async def prepare(self, state: KernelState, emit, call_id: str) -> Prepared | None:
        """Make the kernel live before a call, restoring it when it is not (the one restore path).

        ``None`` means the kernel is live (or durability is off and the caller keeps its Phase 2
        handling of a lost worker). Otherwise the caller starts its result with ``notice``, or, when
        ``lost`` names a reason, reports ``KernelLost`` (the kernel is already reset empty).
        Reasons: ``evicted`` (the registry still holds the exact in-process snapshot), ``restart``
        (no worker after a daemon restart) and ``host_crash`` (the worker or helper was lost).
        """
        from .cell import monty_version

        sid, agent = state.session_id, state.agent_path
        with contextlib.suppress(Exception):
            await self.registry.refresh(sid, agent)
        status = self.registry.status(sid, agent)
        if status == 'live':
            return None
        if status == 'evicted':
            try:
                await self.registry.start(sid, agent)
            except SnapshotInvalid:
                status = 'lost'
            else:
                await emit('code.kernel_restored', {
                    'kernel_id': state.kernel_id, 'call_id': call_id, 'from_n': state.n, 'reason': 'evicted',
                    'missing_cells': [], 'missing_count': 0, 'source': 'memory'})
                return Prepared()
        if self.store is None:
            if status is None:
                await self.open(state)
                state.from_log = False
                return None
            return None
        reason = 'restart' if status is None else 'host_crash'
        row, why = self.store.verified(sid, state.kernel_id, monty_version())
        if row is not None and row.after_n > state.n:
            row, why = None, 'invalid'
        if row is not None:
            with contextlib.suppress(Exception):
                await self.registry.close(sid, agent)
            try:
                await self.registry.start(sid, agent, row.data)
            except SnapshotInvalid:
                row, why = None, 'invalid'
            except BaseException:
                with contextlib.suppress(Exception):
                    await self.registry.close(sid, agent)
                raise
        if row is not None:
            missing = list(range(row.after_n + 1, state.n + 1))
            state.checkpoint_after, state.from_log = row.after_n, False
            await emit('code.kernel_restored', {
                'kernel_id': state.kernel_id, 'call_id': call_id, 'from_n': row.after_n, 'reason': reason,
                'missing_cells': missing[:MAX_MISSING_LISTED], 'missing_count': len(missing),
                'bytes': len(row.data), 'sha256': row.sha256})
            notice = restore_notice(row.after_n, state.n)
            if state.dropped_targets:
                notice += (' Context targets need approval again (code_attach targets): '
                           + ', '.join(state.dropped_targets) + '.')
                state.dropped_targets = []
            return Prepared(notice)
        if state.n == 0:
            # Nothing ran since the start or the last reset: there is nothing to lose.
            await self.restart(state)
            state.from_log = False
            return Prepared()
        if why == 'missing' and state.checkpoint_status == 'too_large':
            why = 'too_large'
        last_n = state.n
        await emit('code.kernel_lost', {'kernel_id': state.kernel_id, 'call_id': call_id, 'reason': why,
                                        'last_n': last_n})
        await self.restart(state)
        state.from_log = False
        return Prepared(lost_text(why, last_n), lost=why)

    # -- the session log: kernel metadata after a restart ---------------------------

    def reconstruct(self, session_id: str, agent_path: str) -> KernelState | None:
        """Rebuild ``(session, agent)``'s kernel metadata from its ``code.*`` events, or ``None``.

        Folds ``kernel_started`` / ``attached`` / ``cell_started`` / ``reset`` / ``lost`` /
        ``checkpointed``. A stopped kernel is gone. Context approvals are not restored from the log
        (they were an interactive grant): only the ``self`` target is re-approved; others are listed
        in ``dropped_targets``.
        """
        if self.log_reader is None:
            return None
        try:
            events = list(self.log_reader(session_id))
        except Exception:  # noqa: BLE001 - an unreadable log means no kernel
            return None
        state: KernelState | None = None
        targets: list[dict] = []
        for event in events:
            kind, data = getattr(event, 'type', ''), getattr(event, 'data', None) or {}
            kid = data.get('kernel_id')
            if kind == 'code.kernel_started':
                if data.get('agent_path') == agent_path and isinstance(kid, str):
                    attached = data.get('attached') or {}
                    state = KernelState(
                        kernel_id=kid, session_id=session_id, agent_path=agent_path,
                        attached={str(t): str(t) for t in attached.get('tools') or []},
                        purpose=str(data.get('purpose') or ''), started=float(getattr(event, 'ts', 0) or 0),
                        from_log=True)
                    targets = [t for t in attached.get('targets') or [] if isinstance(t, dict)]
                continue
            if state is None or kid != state.kernel_id:
                continue
            if kind == 'code.kernel_attached':
                attached = data.get('attached') or {}
                state.attached.update({str(t): str(t) for t in attached.get('tools') or []})
                targets.extend(t for t in attached.get('targets') or [] if isinstance(t, dict))
            elif kind == 'code.cell_started':
                n = data.get('n')
                if isinstance(n, int):
                    state.n = max(state.n, n)
                    state.last_cell = float(getattr(event, 'ts', 0) or 0)
            elif kind == 'code.kernel_reset':
                if data.get('stopped'):
                    state = None
                    continue
                state.n, state.checkpoint_status = 0, ''
            elif kind == 'code.kernel_lost':
                state.n, state.checkpoint_status = 0, ''
            elif kind == 'code.kernel_checkpointed':
                state.checkpoint_status = str(data.get('status') or 'written')
        if state is None:
            return None
        if self.store is not None:
            info = self.store.info(session_id, state.kernel_id)
            state.checkpoint_after = info[0] if info else None
        provider = self.context_provider
        for target in targets:
            ref = str(target.get('ref') or '')
            if ref == 'self' and provider is not None:
                with contextlib.suppress(Exception):
                    found = provider.resolve(session_id, 'self')
                    provider.check(session_id, found['stored_id'])
                    state.approved['self'] = found['stored_id']
                    state.target_info.append({'ref': found['ref'], 'name': found.get('name'),
                                              'logical_id': found.get('logical_id')})
            elif ref and ref not in state.dropped_targets:
                state.dropped_targets.append(ref)
        return state

    async def resolve(self, session_id: str, agent_path: str, catalog=None) -> KernelState | None:
        """The kernel for ``(session, agent)``: in memory, else rebuilt from the session log.

        ``catalog`` (the iteration's full callable catalog) maps attached callable names back to
        their spec names. A rebuilt state has no worker; ``prepare`` restores it.
        """
        state = self.get(session_id, agent_path)
        if state is not None:
            return state
        state = await asyncio.to_thread(self.reconstruct, session_id, agent_path)
        if state is None:
            return None
        if catalog is not None:
            state.attached = {n: (catalog[n].spec.name if n in catalog else spec) for n, spec in state.attached.items()}
        self.put(state)
        return state

    async def maintain(self) -> None:
        """Release idle workers (the registry has no background task)."""
        with contextlib.suppress(Exception):
            await self.registry.evict_idle()

    async def close_session(self, session_id: str) -> None:
        """Close the session's kernels and delete their checkpoints (a closed or deleted session)."""
        for state in self.states(session_id):
            self.drop(state.session_id, state.agent_path)
            with contextlib.suppress(Exception):
                await self.registry.close(state.session_id, state.agent_path)
        self.delete_checkpoints(session_id)

    def release_session(self, session_id: str) -> None:
        """Synchronous variant for session deletion: delete checkpoints now, close workers soon."""
        states = self.states(session_id)
        for state in states:
            self.drop(state.session_id, state.agent_path)
        self.delete_checkpoints(session_id)
        if not states:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for state in states:
            task = loop.create_task(self.registry.close(state.session_id, state.agent_path))
            self._closing.add(task)
            task.add_done_callback(lambda t: (self._closing.discard(t), t.exception() if not t.cancelled() else None))

    def delete_checkpoints(self, session_id: str) -> None:
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.delete_session(session_id)

    async def aclose(self) -> None:
        self._states.clear()
        with contextlib.suppress(Exception):
            await self.registry.shutdown()


# -- selection --------------------------------------------------------------------


def _by_spec(catalog) -> dict[str, str]:
    return {tool.spec.name: name for name, tool in catalog.items()}


def _matches(pattern: str, name: str, tool) -> bool:
    spec_name = tool.spec.name
    return (fnmatchcase(name, pattern) or fnmatchcase(spec_name, pattern)
            or any(fnmatchcase(alias, pattern) for alias in tool_rule_names(spec_name)))


def missing_reason(name: str, tools, full, *, attached=False) -> str:
    """Why ``name`` is not callable from a cell now (disabled, excluded, unsafe, unknown)."""
    canonical = canonical_tool_name(name)
    if name in EXCLUDED or canonical in EXCLUDED:
        return EXCLUDED_REASON
    spec_names = {t.spec.name: t for t in tools}
    tool = spec_names.get(name) or spec_names.get(canonical)
    if tool is not None:
        why = excluded_reason(tool)
        if why:
            return why
        skipped = dict(getattr(full, 'skipped', ()))
        if tool.spec.name in skipped:
            return skipped[tool.spec.name]
    if attached:
        return 'disabled for this session or not available this turn'
    return 'unknown tool, or disabled for this session'


class SearchCache:
    """A lazily built ``SearchIndex`` over one iteration's callable catalog."""

    def __init__(self, catalog):
        self.catalog = catalog
        self._index = None

    def search(self, query: str, limit: int = QUERY_LIMIT) -> list[str]:
        if self._index is None:
            tools = [SimpleNamespace(name=n, local_name=n, spec=t.spec) for n, t in self.catalog.items()]
            self._index = SearchIndex({'builtin': SimpleNamespace(tools=tools)})
        ranked = self._index.search(str(query), limit=max(1, min(int(limit), 10)))
        return [m.tool.local_name for m in ranked.matches]


def _listing(names) -> str:
    names = list(names)
    shown = ', '.join(names[:MAX_LISTED_NAMES])
    return shown + (f' and {len(names) - MAX_LISTED_NAMES} more' if len(names) > MAX_LISTED_NAMES else '')


def select(*, tools, full, ceiling, search: SearchCache, config, servers=(), names=(),
           query: str | None = None, defaults: bool = False, already=()) -> Selection:
    """Resolve a start/attach request against this iteration's catalogs.

    ``full`` (``expose = all``) is where named tools come from; ``ceiling`` (capped by
    ``[code_mode] expose``) bounds implicit attachment by server, query and defaults.
    """
    have = set(already)
    groups: list[tuple[str, tuple[str, ...]]] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set(have)

    def take(heading, candidates):
        fresh = tuple(n for n in dict.fromkeys(candidates) if n not in seen)
        seen.update(fresh)
        if fresh:
            groups.append((heading, fresh))

    if defaults:
        builtin = [n for n, t in ceiling.items() if namespace(t.spec.name) == BUILTIN_NAMESPACE]
        pinned = [canonical_tool_name(p).replace('-', '_') for p in getattr(config, 'pinned', ()) or ()]
        pinned = [p for p in dict.fromkeys(pinned) if p in ceiling]
        take('Built-in tools', [*(p for p in pinned if p in builtin),
                                *(n for n in builtin if n not in pinned)])
        take('Pinned tools', [p for p in pinned if p not in builtin])
    mcp_servers = {namespace(t.spec.name)[4:] for t in full.values() if namespace(t.spec.name) != BUILTIN_NAMESPACE}
    for server in dict.fromkeys(servers):
        if server not in mcp_servers:
            skipped.append((f'server {server}', 'not an enabled MCP server in this session'))
            continue
        everything = [n for n, t in full.items() if namespace(t.spec.name) == f'mcp:{server}']
        allowed = [n for n in everything if n in ceiling]
        refused = [n for n in everything if n not in ceiling]
        take(f'Server {server}', allowed)
        if refused:
            skipped.append((f'server {server}: {len(refused)} of {len(everything)} tools',
                            'refused by [code_mode] expose (mutating or not exposed); name them in '
                            f'`tools` to attach: {_listing(refused)}'))
    named: list[str] = []
    for pattern in dict.fromkeys(names):
        hits = [n for n, t in full.items() if _matches(pattern, n, t)]
        if hits:
            named.extend(hits)
        elif any(ch in pattern for ch in '*?['):
            skipped.append((pattern, 'matches no callable tool'))
        else:
            skipped.append((pattern, missing_reason(pattern, tools, full)))
    take('Named tools', named)
    if query:
        ranked = search.search(query)
        take(f'Search matches for "{query[:60]}"', [n for n in ranked if n in ceiling])
        for n in ranked:
            if n not in ceiling:
                skipped.append((n, 'refused by [code_mode] expose (mutating or not exposed); name it in `tools`'))
    if defaults:
        skipped.extend(getattr(full, 'skipped', ()))
    return Selection(tuple(groups), tuple(dict.fromkeys(skipped)))


def new_kernel_id() -> str:
    return uuid.uuid4().hex[:16]


def catalogs(tools, config):
    """(full, ceiling) catalogs of one iteration's selected tools."""
    return build_catalog(tools, 'all'), build_catalog(tools, config.expose)


__all__ = ['CodeKernels', 'KernelState', 'Selection', 'SearchCache', 'catalogs',
           'missing_reason', 'new_kernel_id', 'select']

