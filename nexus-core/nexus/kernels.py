"""Daemon-owned persistent Monty kernels (PLAN.md step 8).

No mounts or OS handler. Each cell binds host tools to its caller, even when
sandbox code saves a host-function reference from an earlier caller's cell.
Nested calls run as their own tasks so a timeout or interrupt stops them (and
their commands) before the cell is reported and the kernel is discarded; each
one publishes exactly one `code.call` event, in the call's session.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_monty import AsyncMonty, MontyCrashedError, MontyDisconnectError, MontyError

from .llmhub.types import ToolCall
from .store import new_id
from .tools.base import Tool, ToolContext, ToolError, clip

MAX_KERNELS = 8
MAX_CALLS = 64
MAX_CELLS = 100  # retained metadata; cell numbers keep increasing
LIMITS = {"max_feed_duration_secs": 10, "max_memory": 64 * 1024 * 1024,
          "max_recursion_depth": 200, "max_suspensions": MAX_CALLS * 4}
Approve = Callable[[ToolCall, dict[str, Any]], Awaitable[bool]]
Publish = Callable[[str, dict[str, Any]], None]


@dataclass
class Kernel:
    id: str
    created_by: str
    worker: Any
    created: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    counter: int = 0
    users: set[str] = field(default_factory=set)
    cells: list[dict[str, Any]] = field(default_factory=list)
    variables: list[dict[str, str]] = field(default_factory=list)
    names: set[str] = field(default_factory=set)
    dispatch: Any = None
    task: asyncio.Task[Any] | None = None  # the task running the current cell, if any
    closed: bool = False

    def json(self) -> dict[str, Any]:
        return {"id": self.id, "created_by": self.created_by, "created": self.created,
                "cell_count": self.counter, "busy": self.lock.locked(), "users": sorted(self.users),
                "cells": self.cells, "variables": self.variables,
                "cells_clipped": max(0, self.counter - len(self.cells))}


class Kernels:
    def __init__(self) -> None:
        self.pool: Any = None
        self.items: dict[str, Kernel] = {}
        self.main: dict[str, str] = {}
        self.creation_lock = asyncio.Lock()

    def get(self, kernel_id: str) -> Kernel:
        try:
            return self.items[kernel_id]
        except KeyError:
            raise KeyError(f"no kernel {kernel_id}") from None

    async def select(self, root: str, caller: str, kernel_id: str | None) -> Kernel:
        async with self.creation_lock:
            if kernel_id is not None:
                return self.get(kernel_id)
            if root in self.main:
                return self.get(self.main[root])
            if len(self.items) >= MAX_KERNELS:
                raise ToolError(f"live kernel limit ({MAX_KERNELS}) reached; delete a kernel first")
            if self.pool is None:
                self.pool = AsyncMonty(min_processes=0, max_processes=MAX_KERNELS, request_timeout=None)
                await self.pool.__aenter__()
            worker = self.pool.checkout(limits=LIMITS)
            await worker.__aenter__()
            kernel = Kernel(new_id("krn"), caller, worker)
            self.items[kernel.id] = kernel
            self.main[root] = kernel.id
            return kernel

    async def _retire(self, kernel: Kernel) -> None:
        self._forget(kernel)
        await self._close(kernel)

    async def _close(self, kernel: Kernel) -> None:
        if kernel.closed:
            return
        kernel.closed = True
        # Closing is cleanup; a broken worker must not mask the cell error or fail a delete.
        with contextlib.suppress(Exception):
            await kernel.worker.__aexit__(None, None, None)

    def _forget(self, kernel: Kernel) -> None:
        self.items.pop(kernel.id, None)
        self.main = {root: kid for root, kid in self.main.items() if kid != kernel.id}

    async def delete(self, kernel_id: str) -> None:
        kernel = self.get(kernel_id)
        if kernel.lock.locked():
            raise ToolError(f"kernel {kernel_id} is busy")
        # No await between checking the lock and removing the registry entry.
        await self._retire(kernel)

    async def shutdown(self) -> None:
        # Shutdown must finish and close the pool even if a cell is still running: stop the cell (its
        # workspace tools included), then close its worker.
        for kernel in list(self.items.values()):
            self._forget(kernel)
            if kernel.task is not None and not kernel.task.done():
                kernel.task.cancel()
                with contextlib.suppress(BaseException):
                    await kernel.task
            with contextlib.suppress(Exception):
                await self._close(kernel)
        self.main.clear()
        if self.pool is not None:
            pool, self.pool = self.pool, None
            with contextlib.suppress(Exception):
                await pool.__aexit__(None, None, None)

    async def run(self, args: dict[str, Any], *, root: str, caller: str, ctx: ToolContext,
                  tools: list[Tool], approve: Approve, publish: Publish) -> str:
        code = args.get("code")
        timeout = args.get("timeout_s", 300)
        kid = args.get("kernel_id")
        if not isinstance(code, str) or not code.strip():
            raise ToolError("code must be a non-empty string")
        if kid is not None and (not isinstance(kid, str) or not kid):
            raise ToolError("kernel_id must be a non-empty string")
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ToolError("timeout_s must be greater than 0 and at most 600")
        try:
            kernel = await self.select(root, caller, kid)
        except KeyError as exc:
            raise ToolError(str(exc)) from exc
        # Queueing time is bounded too; a cancelled waiter never cancels the active cell.
        try:
            async with asyncio.timeout(timeout):
                async with kernel.lock:
                    if kernel.id not in self.items:
                        raise ToolError(f"no kernel {kernel.id}")
                    try:
                        return await self._cell(kernel, code, caller, ctx, tools, approve, publish)
                    except asyncio.CancelledError:
                        await self._retire(kernel)
                        publish("kernel.deleted", {"id": kernel.id, "reason": "cell cancelled; state discarded"})
                        raise
        except TimeoutError as exc:
            state = "kernel discarded" if kernel.id not in self.items else "queued cell did not run; kernel preserved"
            raise ToolError(f"kernel {kernel.id} · cell timed out after {timeout:g} s (including queue and tool calls); {state}") from exc

    async def _cell(self, k: Kernel, code: str, caller: str, ctx: ToolContext,
                    tools: list[Tool], approve: Approve, publish: Publish) -> str:
        k.counter += 1
        k.users.add(caller)
        k.task = asyncio.current_task()
        cell: dict[str, Any] = {"number": k.counter, "session_id": caller, "call_id": ctx.call_id,
                                "code": code, "started": time.time(), "calls": []}
        k.cells.append(cell)
        k.cells = k.cells[-MAX_CELLS:]
        available = {t.spec.name: t for t in tools if t.spec.name != "code"}
        printed: list[str] = []
        printed_size = 0
        dropped = 0
        inflight: dict[int, asyncio.Task[str]] = {}
        published: set[int] = set()

        def capture(stream: str, text: str) -> None:
            nonlocal printed_size, dropped
            room = max(0, 30_000 - printed_size)
            printed.append(text[:room]) if room else None
            printed_size += min(len(text), room)
            dropped += max(0, len(text) - room)

        def close(record: dict[str, Any]) -> None:
            """Publish each nested call once, whichever path (normal, error, abort) finishes it."""
            if record["index"] in published:
                return
            published.add(record["index"])
            publish("code.call", record.copy())

        async def invoke(name: str, arguments: dict[str, Any], call: ToolCall) -> str:
            tool = available.get(name)
            if tool is None:
                raise ToolError(f"tool {name!r} is unavailable to the calling agent")
            if not await approve(call, arguments):
                raise ToolError("the user declined this tool call")
            return await tool.run(arguments, ToolContext(ctx.workspace, call.id))

        async def dispatch(name: str, arguments: dict[str, Any]) -> str:
            if len(cell["calls"]) >= MAX_CALLS:
                raise RuntimeError(f"nested call limit ({MAX_CALLS}) reached")
            index = len(cell["calls"]) + 1
            record = {"kernel_id": k.id, "cell": cell["number"], "index": index,
                      "parent_call_id": ctx.call_id, "tool": name, "arguments": arguments,
                      "output": "", "error": None}
            cell["calls"].append(record)
            call = ToolCall(f"{ctx.call_id or k.id}:{cell['number']}:{index}", name, json.dumps(arguments))
            # The tool runs in its own task so a timeout or interrupt can stop it even when Monty abandons
            # the host call while cancelling the cell.
            task = asyncio.create_task(invoke(name, arguments, call))
            inflight[index] = task
            try:
                record["output"] = await task
                return record["output"]
            except asyncio.CancelledError:
                record["error"] = record["error"] or "interrupted or timed out"
                raise
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                raise RuntimeError(record["error"]) from exc
            finally:
                inflight.pop(index, None)
                close(record)

        async def abort_calls() -> None:
            """Stop nested calls Monty left running. A cancelled cell must not keep changing the workspace."""
            waiting = [task for task in inflight.values() if not task.done()]
            for task in waiting:
                task.cancel()
            if waiting:
                await asyncio.gather(*waiting, return_exceptions=True)
            for record in cell["calls"]:
                if record["index"] in published:
                    continue
                task = inflight.get(record["index"])
                if task is not None and task.done() and not task.cancelled():
                    error = task.exception()  # None when it completed
                    if error is None:
                        record["output"] = task.result()
                    elif not record["error"]:
                        record["error"] = f"{type(error).__name__}: {error}"
                if not record["output"] and not record["error"]:
                    record["error"] = "interrupted or timed out"
                close(record)

        k.dispatch = dispatch

        def binding(name: str) -> Any:
            async def invoke(**kwargs: Any) -> str:
                if k.dispatch is None:
                    raise RuntimeError("no active cell")
                return await k.dispatch(name, kwargs)
            return invoke

        value = None
        error = None
        try:
            value = await k.worker.feed_run(code, external_lookup={n: binding(n) for n in available}, print_callback=capture)
        except asyncio.CancelledError:
            error = "interrupted or timed out"
            raise
        except (MontyError, OSError, ValueError, RuntimeError) as exc:
            error = (str(exc).strip().splitlines() or [type(exc).__name__])[-1]
            if isinstance(exc, (MontyCrashedError, MontyDisconnectError)) or any(
                marker in error for marker in ("MemoryError", "TimeoutError", "RecursionError")
            ):
                await self._retire(k)
                error += "; kernel discarded"
                publish("kernel.deleted", {"id": k.id, "reason": error})
        finally:
            k.dispatch = None
            await abort_calls()
            cell.update(duration_s=time.time() - cell["started"], error=error)
            cell["stdout"] = "".join(printed) + (f"\n[… clipped {dropped} stdout characters …]" if dropped else "")
            cell["value"] = clip(repr(value)) if value is not None else None
            publish("code.cell", {"kernel_id": k.id, **cell})
        # Monty has no globals()/dir(). Track assigned names with Python's parser,
        # then ask the sandbox for type names and bounded reprs; never evaluate on host.
        try:
            tree = ast.parse(code)
            k.names.update(n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
            variables = []
            for name in sorted(k.names)[:200] if k.id in self.items else []:
                try:
                    v = await k.worker.feed_run(f"(type({name}).__name__, repr({name})[:200])", print_callback=lambda *_: None)
                    variables.append({"name": name, "type": v[0], "repr": v[1]})
                except (MontyError, OSError, ValueError, RuntimeError):
                    continue  # deleted names, function-local names, or an unavailable worker
            k.variables = variables
        except SyntaxError:
            pass
        header = f"kernel {k.id} · In [{cell['number']}] · {cell['duration_s']:.2f} s · {len(cell['calls'])} calls"
        lines = [header, cell["stdout"]]
        if value is not None:
            lines.append(f"Out [{cell['number']}]: {cell['value']}")
        if error:
            lines.append(error)
        for record in cell["calls"]:
            lines.append(f"{record['index']}. {record['tool']} {clip(repr(record['arguments']), 300)} · "
                         f"{'error: ' + record['error'] if record['error'] else 'ok'} · {len(record['output'])} chars")
        result = clip("\n".join(line for line in lines if line))
        if error:
            raise ToolError(result)
        return result
