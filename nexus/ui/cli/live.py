"""``nexus live``: a headless GPT Live voice call from the terminal (docs/live.md, GPT_LIVE_PLAN section 5).

A client like the others: it reaches the daemon only through the host client
(``LiveStatus``, ``LiveConsent``, ``LiveStart``/``LiveStop`` via
``ui_support.live_helper.LiveCall``, and the session's ``live.*`` events), and runs the
``nexus-live`` audio helper through that same wrapper. Consent is recorded only after a
"y" on this terminal; a non-terminal stdin without recorded consent is refused, never
auto-accepted. Ctrl+C, end of input or a failing helper hangs up (``LiveStop``, then the
helper is released). Typed input during a call is read and ignored: steering by voice
goes through the agent, not this terminal.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TextIO

from ...ui_support import live_helper
from ...ui_support.manager import CONSENT_LINES as MANAGER_CONSENT_LINES
from ...ui_support.manager import MANAGER_SESSION_ID
from ...ui_support.text import escape_controls

#: The stop reason sent to the daemon when the user hangs up (Ctrl+C or end of input).
_HANGUP_REASON = "user"


def _text(value: Any) -> str:
    """Caption or instruction text as the user sees it: controls escaped, newlines kept, indented."""
    return escape_controls("" if value is None else str(value)).replace("\n", "\n    ")


def format_event(event: Any, call_id: str) -> str | None:
    """One labelled output line for a ``live.*`` event of ``call_id``; ``None`` when it is not shown.

    Partial captions (``live.transcript.delta``), levels and phases are not printed: the
    headless surface shows final text only.
    """
    data = event.data if isinstance(event.data, dict) else {}
    if data.get("call_id") != call_id:
        return None
    kind = event.type
    if kind == "live.transcript":
        role = "voice" if data.get("role") == "assistant" else "you"
        clipped = " (truncated)" if data.get("truncated") else ""
        return f"{role}: {_text(data.get('text'))}{clipped}"
    if kind == "live.delegated":
        return f"→ agent ({_text(data.get('mode') or 'queue')}): {_text(data.get('text'))}"
    if kind == "live.appended":
        return f"← voice [{_text(data.get('channel') or 'final')}]: {_text(data.get('text'))}"
    if kind == "live.error":
        return f"error: {_text(data.get('message'))}"
    if kind == "live.ended":
        reason = _text(data.get("reason") or "ended")
        seconds = data.get("duration_s")
        took = f", {int(seconds)} s" if isinstance(seconds, (int, float)) else ""
        error = f" · {_text(data.get('error'))}" if data.get("error") else ""
        return f"Call ended ({reason}{took}){error}"
    return None


async def read_line() -> str | None:
    """One line from the terminal, or ``None`` at end of input.

    A daemon thread does the blocking read, so Ctrl+C never waits for a newline.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def resolve(line: str) -> None:
        if not future.done():
            future.set_result(line)

    def reader() -> None:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            line = ""
        with contextlib.suppress(RuntimeError):  # the loop already closed: nobody is waiting
            loop.call_soon_threadsafe(resolve, line)

    threading.Thread(target=reader, name="nexus-live-stdin", daemon=True).start()
    line = await future
    return None if line == "" else line.rstrip("\r\n")


async def stdin_end() -> None:
    """Return when standard input ends (Ctrl+D on a terminal, EOF when piped)."""
    while await read_line() is not None:
        pass


async def _pump_events(client: Any, session: str, from_seq: int, call_id: str, queue: asyncio.Queue) -> None:
    """Forward this call's ``live.*`` events; earlier calls in the same session are skipped."""
    try:
        async for event in client.stream(session, from_seq, follow=True):
            if str(event.type).startswith("live.") and isinstance(event.data, dict) \
                    and event.data.get("call_id") == call_id:
                await queue.put(("event", event))
        await queue.put(("stream", "the session event stream closed"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as one labelled row, then hung up
        await queue.put(("stream", f"lost the connection to the daemon ({type(exc).__name__})"))


async def _pump_end(end_of_input: Callable[[], Awaitable[None]], queue: asyncio.Queue) -> None:
    await end_of_input()
    await queue.put(("input", None))


async def run_live(
    client: Any,
    *,
    session: str,
    voice: str = "",
    new_conversation: bool = False,
    stdout: TextIO,
    stderr: TextIO,
    interactive: bool,
    ask: Callable[[], Awaitable[str | None]] = read_line,
    end_of_input: Callable[[], Awaitable[None]] = stdin_end,
) -> int:
    """Run one call in ``session`` until it ends; returns the process exit code.

    ``0`` when the call ended normally (or the user hung up), ``1`` when it could not
    start, the audio helper stopped or the daemon connection was lost. Cancellation
    (Ctrl+C) hangs up first and then propagates.
    """
    status = await client.live_status()
    if not status.available:
        stderr.write(f"{live_helper.unavailable_message(status.reason, status.active_session)}\n")
        if fix := live_helper.fix_for(status.reason):
            stderr.write(f"Fix: {fix}\n")
        return 1
    try:
        live_helper.binary_path()
    except live_helper.LiveHelperError as exc:
        stderr.write(f"Error: {exc}\n")
        if exc.hint:
            stderr.write(f"Fix: {exc.hint}\n")
        return 1

    # A call on the manager also sends what it reads of other sessions: its own, separate notice.
    manager = session == MANAGER_SESSION_ID
    agreed = bool(status.consent)
    if agreed and manager:
        agreed = bool((await client.live_consent(None, scope="manager")).accepted)
    if not agreed:
        if not interactive:
            stderr.write(
                "Error: GPT Live consent is not recorded on this machine, and stdin is not a terminal, "
                "so nothing was started. Run `nexus live` in a terminal to read the notice and agree.\n"
            )
            return 1
        lines = [*live_helper.CONSENT_LINES, "", *MANAGER_CONSENT_LINES] if manager else live_helper.CONSENT_LINES
        stdout.write("\n".join(lines) + "\n\n")
        stderr.write("Agree and start the call? [y/N] ")
        stderr.flush()
        answer = (await ask() or "").strip().lower()
        if answer not in ("y", "yes"):
            stderr.write("Not started. Nothing was recorded.\n")
            return 1
        await client.live_consent(True, **({"scope": "manager"} if manager else {}))

    summary = await client.open_session(session)
    from_seq = int(getattr(summary, "last_seq", 0) or 0)
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=256)
    pumps: list[asyncio.Task] = []

    def on_helper_event(event: dict[str, Any]) -> None:
        if event.get("type") == "closed" and event.get("reason") != "stop":
            pumps.append(asyncio.create_task(queue.put(
                ("helper", str(event.get("reason") or "closed")),
            )))

    call = live_helper.LiveCall(client, session, on_event=on_helper_event)
    try:
        result = await call.start(voice=voice, new_conversation=new_conversation)
    except Exception as exc:  # noqa: BLE001 - the helper is already released by LiveCall
        message, fix = live_helper.describe_failure(exc)
        stderr.write(f"Error: GPT Live could not start: {message}\n")
        if fix:
            stderr.write(f"Fix: {fix}\n")
        return 1

    stdout.write(f"Live call connected in session {session}. Speak naturally; Ctrl+C or Ctrl+D hangs up.\n")
    stdout.flush()
    pumps = [
        asyncio.create_task(_pump_events(client, session, from_seq, result.call_id, queue)),
        asyncio.create_task(_pump_end(end_of_input, queue)),
    ]
    exit_code, stop_reason = 0, _HANGUP_REASON
    try:
        while True:
            kind, value = await queue.get()
            if kind == "event":
                if line := format_event(value, result.call_id):
                    stdout.write(line + "\n")
                    stdout.flush()
                if value.type == "live.ended":  # the daemon ended it: nothing left to hang up
                    break
            elif kind == "helper":
                hint = live_helper.classify_failure(value)
                stderr.write(f"Error: the audio helper stopped ({value}); the call was hung up.\n")
                if hint:
                    stderr.write(f"Fix: {hint}\n")
                exit_code, stop_reason = 1, "helper_failed"
                break
            elif kind == "stream":
                stderr.write(f"Error: {value}; the call was hung up.\n")
                exit_code, stop_reason = 1, "daemon_lost"
                break
            else:  # the input ended
                stdout.write("Input ended; hanging up.\n")
                break
    except asyncio.CancelledError:
        stdout.write("Hanging up.\n")
        stdout.flush()
        raise
    finally:
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        with contextlib.suppress(Exception):  # LiveStop is idempotent; the helper is released regardless
            await call.stop(stop_reason)
    return exit_code


__all__ = ["format_event", "read_line", "run_live", "stdin_end"]
