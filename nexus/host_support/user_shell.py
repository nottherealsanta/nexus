"""Composer ``!`` shell mode: run a user-typed command, then add it to context.

Contract (docs/tools.md "Shell mode"):

- ``SessionShell`` runs the command with ``/bin/bash -c`` in the workspace
  through the runtime's ``JobRegistry`` (same process-group lifecycle, capture
  cap and environment as the ``bash`` tool). The user typed it, so no
  permission rule is consulted. It is bounded by ``tools.bash_max_s``.
- ``shell.started`` and ``shell.completed`` are durable session events; the
  reducer draws each run as a ``kind="shell"`` turn holding one ``bash`` row.
- The output is limited exactly like the ``bash`` tool (2,000 lines or 50 KiB;
  the rest is saved to a temp file whose path is named) and added to model
  context as a user message through ``Session.add_context``. It never starts a
  turn: idle sessions just record it, a running turn sees it at its next safe
  boundary.
- ``cancel(session)`` (the client's Stop) kills running commands of that
  session; the cancelled run is still recorded with what it printed.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any

from ..tools.builtin import _jobs
from ..tools.builtin._output_limit import saved_path
from ..util import new_id

__all__ = ["MAX_COMMAND_CHARS", "MAX_RUNNING_PER_SESSION", "UserShells", "context_text"]

#: Longest accepted ``!`` command.
MAX_COMMAND_CHARS = 16_000
#: Commands one session may run at the same time.
MAX_RUNNING_PER_SESSION = 4
_SHELL = "/bin/bash"


def context_text(command: str, output: str) -> str:
    """The user message the model sees for one ``!`` run."""
    return (
        "The user ran a shell command in the workspace (composer `!` mode). "
        "Command and output:\n"
        f"$ {command}\n{output}"
    )


class UserShells:
    """Runs ``!`` commands for one runtime; owns their asyncio tasks."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._own_registry: _jobs.JobRegistry | None = None

    def _registry(self) -> _jobs.JobRegistry:
        registry = getattr(self._runtime, "job_registry", None)
        if registry is not None:
            return registry
        if self._own_registry is None:
            self._own_registry = _jobs.JobRegistry()
        return self._own_registry

    def _max_s(self) -> float:
        loader = getattr(self._runtime, "_load_config", None)
        try:
            config = loader() if callable(loader) else None
        except Exception:  # noqa: BLE001 - fall back to the default limit
            config = None
        return _jobs.max_runtime(config)

    def running(self, session_id: str) -> int:
        return len(self._tasks.get(session_id, ()))

    def start(self, handle: Any, command: str) -> str:
        """Validate, record ``shell.started`` and run in the background."""
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Shell command is empty")
        if len(command) > MAX_COMMAND_CHARS:
            raise ValueError(f"Shell command is longer than {MAX_COMMAND_CHARS} characters")
        if "\x00" in command:
            raise ValueError("Shell command must not contain NUL")
        session_id = handle.id
        if self.running(session_id) >= MAX_RUNNING_PER_SESSION:
            raise ValueError(
                f"{MAX_RUNNING_PER_SESSION} shell commands are already running in this session"
            )
        workspace = Path(self._runtime.workspace)
        shell_id = new_id()
        handle.emit_session_event(
            "shell.started",
            # ``pid`` lets a later open tell a crashed run from a live one.
            {"shell_id": shell_id, "command": command, "shell": _SHELL, "cwd": str(workspace),
             "pid": os.getpid()},
        )
        task = asyncio.create_task(self._run(handle, shell_id, command, workspace))
        tasks = self._tasks.setdefault(session_id, set())
        tasks.add(task)

        def _done(finished: asyncio.Task[None]) -> None:
            tasks.discard(finished)
            if not tasks:
                self._tasks.pop(session_id, None)
            if finished.cancelled():
                # Stopped before ``_run`` began: still close the run, so the
                # view never keeps a row that looks like it is running.
                with contextlib.suppress(Exception):
                    handle.emit_session_event("shell.completed", {
                        "shell_id": shell_id, "command": command, "status": "cancelled",
                        "exit_code": None, "duration_ms": 0,
                        "output": "[stopped by the user]\n(not started)", "context": "deferred",
                    })

        task.add_done_callback(_done)
        return shell_id

    async def _run(self, handle: Any, shell_id: str, command: str, workspace: Path) -> None:
        started = time.monotonic()
        limit = self._max_s()
        job = None
        status = "completed"
        output = ""
        try:
            job = await self._registry().spawn(
                command, session_id=handle.id, cwd=workspace, shell=_SHELL
            )
            outcome = await _jobs.await_job(job, timeout=limit)
            if outcome == "timeout" or job.status is _jobs.JobStatus.TIMED_OUT:
                status = "timed_out"
            output = _jobs.format_job_output(job)
        except asyncio.CancelledError:
            status = "cancelled"
            output = _jobs.format_job_output(job) if job is not None else "(not started)"
        except Exception as exc:  # noqa: BLE001 - recorded as a failed run
            status = "failed"
            output = f"failed to start: {exc}"
        if status == "timed_out":
            output = f"[timed out after {limit:g}s; process group terminated]\n{output}"
        elif status == "cancelled":
            output = f"[stopped by the user]\n{output}"
        exit_code = getattr(job, "exit_code", None)
        added = False
        with contextlib.suppress(Exception):
            added = handle.add_context(context_text(command, output))
        data: dict[str, Any] = {
            "shell_id": shell_id,
            "command": command,
            "status": status,
            "exit_code": exit_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": output,
            "context": "added" if added else "deferred",
        }
        if job is not None:
            data["job_id"] = job.job_id
        saved = saved_path(output)
        if saved:
            data["output_path"] = saved
        with contextlib.suppress(Exception):
            handle.emit_session_event("shell.completed", data)

    def cancel(self, session_id: str) -> int:
        """Stop every running ``!`` command of a session; returns how many."""
        tasks = list(self._tasks.get(session_id, ()))
        for task in tasks:
            task.cancel()
        return len(tasks)

    async def aclose(self) -> None:
        tasks = [task for group in self._tasks.values() for task in group]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._own_registry is not None:
            await self._own_registry.aclose()

