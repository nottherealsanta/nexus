"""Kernel blocks for the compaction input (plan code-mode-kernel §3.9).

``kernel_blocks`` gathers, for one session, a ``KernelBlock`` per code-mode kernel: the
in-memory kernels plus any restorable kernel the session log records (after a restart). It
never restores or starts a worker. Only a **live** kernel gets its ``whos`` table, read by a
bounded feed (``read_variables``, time-limited); an evicted, lost or not-running kernel is
listed from metadata with the reason its variables are not listed.
"""
from __future__ import annotations

import asyncio

from ...context.kernel_block import KernelBlock
from .catalog import BUILTIN_NAMESPACE, namespace
from .cell import read_variables

#: Upper bound on the whos feed while compacting: a busy or slow kernel is listed without variables.
VARIABLES_TIMEOUT_S = 5.0


def _logged_agent_paths(kernels, session_id: str) -> set[str]:
    """Agent paths of the ``code.kernel_started`` events in the session log (empty when unreadable)."""
    reader = getattr(kernels, "log_reader", None)
    if reader is None:
        return set()
    try:
        events = list(reader(session_id))
    except Exception:  # noqa: BLE001 - an unreadable log only hides restorable kernels
        return set()
    paths = set()
    for event in events:
        if getattr(event, "type", "") == "code.kernel_started":
            agent = (getattr(event, "data", None) or {}).get("agent_path")
            if isinstance(agent, str) and agent:
                paths.add(agent)
    return paths


def _servers(attached: dict[str, str]) -> tuple[str, ...]:
    names = set()
    for spec_name in attached.values():
        space = namespace(spec_name)
        if space != BUILTIN_NAMESPACE and space.startswith("mcp:"):
            names.add(space[4:])
    return tuple(sorted(names))


async def _block(kernels, state) -> KernelBlock:
    sid, agent = state.session_id, state.agent_path
    status = kernels.registry.status(sid, agent) or "not running"
    variables = None
    note = ""
    if status == "live":
        try:
            async with asyncio.timeout(VARIABLES_TIMEOUT_S):
                rows = await read_variables(kernels, state)
        except TimeoutError:
            rows = None
        if rows is None:
            note = "kernel busy or unavailable"
        else:
            variables = tuple((str(n), str(k), s) for n, k, s in rows)
    elif status == "evicted":
        note = "kernel evicted"
    elif status == "lost":
        note = "kernel lost"
    else:
        note = "kernel not running; restorable from its checkpoint if one exists"
    targets = [str(i.get("name") or i.get("ref") or "") for i in state.target_info]
    targets += [f"{ref} (needs approval)" for ref in state.dropped_targets]
    return KernelBlock(
        kernel_id=state.kernel_id,
        agent_path=agent,
        status=status,
        cells=int(state.n),
        attached=tuple(state.attached),
        servers=_servers(state.attached),
        targets=tuple(t for t in targets if t),
        purpose=state.purpose or "",
        variables=variables,
        variables_note=note,
    )


async def kernel_blocks(kernels, session_id: str) -> tuple[KernelBlock, ...]:
    """One block per kernel of ``session_id`` (in memory or restorable), in agent-path order."""
    if kernels is None or not session_id:
        return ()
    agents = {state.agent_path for state in kernels.states(session_id)}
    agents |= await asyncio.to_thread(_logged_agent_paths, kernels, session_id)
    blocks = []
    for agent in sorted(agents):
        # ``resolve`` returns the in-memory state or rebuilds metadata from the log; it starts no worker.
        state = await kernels.resolve(session_id, agent)
        if state is None:
            continue
        blocks.append(await _block(kernels, state))
    return tuple(blocks)


__all__ = ["VARIABLES_TIMEOUT_S", "kernel_blocks"]
