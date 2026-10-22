"""``BashOutput``: read buffered output and status for a registry-owned job.

Only opaque job ids returned by ``shell`` are accepted. The tool cannot be used
to inspect an arbitrary process or PID. Offsets are indexes into the job's
captured stdout/stderr, so callers can read the full buffer or only what has
arrived since a previous call.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from . import _jobs

__all__ = ["SPEC", "run"]

_MAX_WAIT_S = 30.0

_JOB_ID = {
    "type": "string",
    "description": "A job_id previously returned by Bash.",
}
_STDOUT_OFFSET = {
    "type": "integer",
    "description": (
        "Byte offset into captured stdout. Use the stdout_offset returned by "
        "the previous call to read only new output; omit for the full buffer."
    ),
}
_STDERR_OFFSET = {
    "type": "integer",
    "description": (
        "Byte offset into captured stderr. Use the stderr_offset returned by "
        "the previous call to read only new output; omit for the full buffer."
    ),
}
_WAIT = {
    "type": "number",
    "maximum": _MAX_WAIT_S,
    "description": (
        "Optional seconds to wait for new output or completion before "
        f"returning. Must be no greater than {_MAX_WAIT_S:g} seconds."
    ),
}

SPEC = ToolSpec(
    name="BashOutput",
    group="bash",
    description=(
        "Read stdout, stderr, and status for a background shell job started "
        "by Bash. Pass the returned offsets back to poll incrementally."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "job_id": _JOB_ID,
            "stdout_offset": _STDOUT_OFFSET,
            "stderr_offset": _STDERR_OFFSET,
            "wait_s": _WAIT,
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
    bundle="legacy_shell",
    mutates=False,
    concurrency="parallel",
    permission_key=lambda data: str(data.get("job_id", "")),
)


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult.text(message, is_error=True)


def _offset(args: dict[str, Any], key: str) -> int:
    value = args.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


async def _read_job_output(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    tool_name: str = "BashOutput",
    max_wait_s: float | None = None,
    unified: bool = False,
) -> ToolExecutionResult:
    """Return buffered output from a registry-owned job.

    The legacy ``BashOutput`` entry point waits only for new output (bounded by
    ``_MAX_WAIT_S``) and reads from offset 0 unless offsets are given. The
    unified ``shell`` tool (``unified=True``) defaults to ``until="exit"`` and a
    per-job read cursor, so ``status``/``wait`` without offsets return only
    output not returned before. ``max_wait_s`` caps ``until="output"`` waits.
    """
    job_id = args.get("job_id") if isinstance(args, dict) else None
    if not isinstance(job_id, str) or not job_id:
        return _error(f"{tool_name}: 'job_id' must be a non-empty string")

    registry = _jobs.registry_for(ctx)
    try:
        session_id = _jobs.require_session_id(ctx.session_id)
    except _jobs.JobRegistryError as exc:
        return _error(f"{tool_name}: {exc}")
    job = registry.job(job_id, session_id=session_id)
    if job is None:
        return _error(
            f"{tool_name}: unknown job_id {job_id!r}; only jobs started by "
            "the shell tool in this session are valid"
        )

    try:
        explicit_out = "stdout_offset" in args
        explicit_err = "stderr_offset" in args
        stdout_offset = _offset(args, "stdout_offset")
        stderr_offset = _offset(args, "stderr_offset")
        wait_s = args.get("wait_s")
    except ValueError as exc:
        return _error(f"{tool_name}: {exc}")
    if unified:
        if not explicit_out:
            stdout_offset = job.read_stdout
        if not explicit_err:
            stderr_offset = job.read_stderr
    until = args.get("until", "exit" if unified else "output")
    if until not in (("exit", "output", "match") if unified else ("exit", "output")):
        return _error(
            f"{tool_name}: until must be 'exit', 'output'"
            + (" or 'match'" if unified else "")
        )
    matcher: _jobs.LineMatcher | None = None
    if until == "match":
        try:
            regex = _jobs.compile_match_pattern(args.get("pattern"))
        except ValueError as exc:
            return _error(f"{tool_name}: {exc}")
        matcher = _jobs.LineMatcher(
            regex, 0 if explicit_out or explicit_err else job.read_combined
        )

    if ctx.cancel_token is not None:
        ctx.cancel_token.raise_if_cancelled()

    if wait_s is not None and (
        isinstance(wait_s, bool)
        or not isinstance(wait_s, (int, float))
        or (isinstance(wait_s, float) and not math.isfinite(wait_s))
        or wait_s <= 0
    ):
        return _error(f"{tool_name}: wait_s must be a positive finite number")

    wait_limit = max_wait_s if max_wait_s is not None else _MAX_WAIT_S
    if until == "exit":
        wait_limit = _jobs.max_runtime(ctx.config)
        if job.persistent:
            wait_limit = min(wait_limit, _jobs.PERSISTENT_WAIT_MAX_S)
    elif until == "match":
        wait_limit = _jobs.MATCH_WAIT_MAX_S
    if wait_s is not None and wait_s > wait_limit:
        return _error(f"{tool_name}: wait_s must not exceed {wait_limit:g} seconds")

    waiting = args.get("action") == "wait" if unified else wait_s is not None
    header_extra: tuple[str, ...] = ()
    if not waiting:
        pass
    elif matcher is not None:
        try:
            header_extra = await _wait_for_match(
                job,
                matcher,
                float(wait_s) if wait_s is not None else wait_limit,
                ctx,
            )
        except ValueError as exc:
            return _error(f"{tool_name}: {exc}")
    elif until == "exit":
        if not job.done:
            await _wait_for_exit(
                job, float(wait_s) if wait_s is not None else wait_limit, ctx
            )
    else:
        await _wait_for_output(
            job,
            stdout_offset,
            stderr_offset,
            float(wait_s) if wait_s is not None else wait_limit,
            ctx,
        )

    text = _jobs.format_job_output(
        job,
        stdout_offset=stdout_offset,
        stderr_offset=stderr_offset,
        show_offsets=True,
        combined_offset=(
            job.read_combined if unified and not (explicit_out or explicit_err) else 0
        ),
        header_extra=header_extra,
    )
    # The terminal state is in this result: it is the model-visible completion.
    job.claim()
    if unified:
        if not explicit_out:
            job.read_stdout = max(job.read_stdout, len(job.stdout))
        if not explicit_err:
            job.read_stderr = max(job.read_stderr, len(job.stderr))
        if not (explicit_out or explicit_err):
            job.read_combined = max(job.read_combined, job.log.total)
    return ToolExecutionResult.text(
        text,
        context_note=(
            f"[{tool_name} {job_id}: output evicted; call {tool_name} again with "
            "the recorded offsets to re-read it]"
        ),
    )


async def run(
    args: dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    """Legacy entry point for reading buffered output."""
    return await _read_job_output(args, ctx)


async def _wait_for_output(
    job: _jobs.ShellJob,
    stdout_offset: int,
    stderr_offset: int,
    wait_s: float,
    ctx: ToolContext,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s

    def ready() -> bool:
        return (
            job.done
            or job.stdout.has_new(stdout_offset)
            or job.stderr.has_new(stderr_offset)
        )

    while not ready():
        if ctx.cancel_token is not None and ctx.cancel_token.cancelled:
            ctx.cancel_token.raise_if_cancelled()
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await job.wait_for_output(ready, min(remaining, 0.2))


def _grep_timeout(ctx: ToolContext) -> float:
    value = _jobs._tools_number(ctx.config, "grep_timeout_s")
    return value if value is not None else 5.0


async def _wait_for_match(
    job: _jobs.ShellJob,
    matcher: _jobs.LineMatcher,
    wait_s: float,
    ctx: ToolContext,
) -> tuple[str, ...]:
    """Wait for a line matching ``matcher`` (header lines say what happened).

    Returns when a line matches, the job exits (its unterminated last line is
    tested first) or ``wait_s`` elapses; the job is never killed. A cancelled wait
    leaves it running and hands its completion back to delivery, as ``until=exit``
    does. Scanning runs off the event loop with the ``tools.grep_timeout_s``
    deadline, so a pathological pattern costs the model an error, not the daemon.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    scan_s = _grep_timeout(ctx)
    job.begin_wait()
    matched: str | None = None
    try:
        while True:
            if ctx.cancel_token is not None and ctx.cancel_token.cancelled:
                ctx.cancel_token.raise_if_cancelled()
            finished = job.done
            pieces = matcher.collect(job)

            def work(pieces=pieces, finished=finished) -> str | None:
                hit = matcher.scan(pieces)
                return hit if hit is not None or not finished else matcher.flush()

            try:
                matched = await asyncio.wait_for(asyncio.to_thread(work), scan_s)
            except TimeoutError:
                raise ValueError(
                    f"matching the pattern took longer than {scan_s:g}s and was "
                    "abandoned; use a simpler pattern"
                ) from None
            if matched is not None or finished:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await job.wait_for_output(
                lambda: job.done or job.log.total > matcher.offset,
                min(remaining, 0.2),
            )
    except BaseException:
        job.end_wait(consumed=False)
        raise
    job.end_wait(consumed=job.done)
    lines: list[str] = []
    if matched is not None:
        lines.append(f"match: {matched[:300]}")
    elif job.done:
        lines.append("match: none (the job exited first)")
    else:
        lines.append(
            f"match: none within {wait_s:g}s (the job is still running)"
        )
    if matcher.skipped:
        lines.append(
            f"match_unscanned_bytes: {matcher.skipped} (use shell action=log "
            "grep=... to search the full log)"
        )
    return tuple(lines)


async def _wait_for_exit(
    job: _jobs.ShellJob, wait_s: float, ctx: ToolContext
) -> None:
    """Block until the job exits or ``wait_s`` elapses; never kills the job.

    Cancellation stops the wait and leaves the job running: it was started or
    yielded to the background on purpose.
    """
    # While blocked here the wait owns the completion: an exit that happens
    # now is returned as this call's result and is not also queued. A wait that
    # ends without the exit (cancelled) hands the completion back to delivery.
    job.begin_wait()
    try:
        await _jobs.await_job(
            job,
            yield_after=wait_s,
            cancel_token=ctx.cancel_token,
            kill_on_cancel=False,
            tick=_progress_tick(job, ctx),
        )
    except BaseException:
        job.end_wait(consumed=False)
        raise
    job.end_wait(consumed=job.done)


def _progress_tick(job: _jobs.ShellJob, ctx: ToolContext):
    """Throttled ``tool.progress`` reporter: the job's last output line."""
    sent = 0
    last = ""

    async def tick() -> None:
        nonlocal sent, last
        if sent >= _jobs.MAX_PROGRESS_EVENTS:
            return
        line = _jobs.last_output_line(job)
        if not line or line == last:
            return
        last = line
        sent += 1
        await ctx.report(line, {"job_id": job.job_id})

    return tick
