"""Native terminal adapter over ``Runtime`` + ``Session`` (Phase 2 Packet F).

This is an **opt-in** UI adapter reached only through ``nexus native-run`` and
``nexus native-chat``. The legacy ``nexus run`` / ``nexus chat`` path is
untouched and never imports this module, so plain ``argparse`` help and the
Codex-backed path do not eagerly load ``httpx``, the Runtime, or the tool stack
(``build_runtime`` imports :class:`nexus.runtime.Runtime` lazily).

Rendering contract
------------------

* Text mode streams ``text.delta`` to stdout and writes a newline when the
  finalized ``text`` event arrives, so a response is never printed twice. A
  provider that emits only a final ``text`` (no deltas) is printed once too.
* ``--json`` writes every persisted :class:`~nexus.events.Event` envelope to
  stdout in the exact order ``Session.send`` yielded them and never renders
  human text there. Unknown event types pass through unchanged.
* Human diagnostics (approval prompts, failures, cancellation) go to stderr.

Approval contract
-----------------

Interactive (non-JSON) runs mark the session attended and answer each
``permission.requested`` while the send generator is live, resolving the request
through :meth:`Session.resolve_permission`. The blocking read is offloaded with
:func:`asyncio.to_thread`, so event consumption continues on the loop and a
prompt cannot deadlock the turn. EOF, repeated invalid input, and an exhausted
prompt all resolve as ``DENY_ONCE`` — input exhaustion can never auto-allow.
JSON runs never prompt; the session stays unattended, so the configured
``permissions.on_unattended`` policy applies.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import aclosing
from pathlib import Path
from typing import Any, TextIO

from ..events import Event
from ..tools.permissions import Decision

__all__ = [
    "Approver",
    "build_runtime",
    "chat_native",
    "run_native",
]

#: Event types that terminate a turn.
_TERMINAL = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})

#: How many unrecognized entries to tolerate before falling back to deny-once.
_MAX_PROMPT_ATTEMPTS = 3

#: Longest tool key/preview rendered in an approval prompt.
_KEY_LIMIT = 240

_PROMPT = "approve? [y] once / [a] always / [n] deny once / [never] deny always: "

#: Accepted answers, lower-cased. Deliberately conservative: an unrecognized
#: answer re-prompts, and exhaustion falls back to deny-once.
_DECISIONS: dict[str, Decision] = {
    "y": Decision.ALLOW_ONCE,
    "yes": Decision.ALLOW_ONCE,
    "once": Decision.ALLOW_ONCE,
    "allow": Decision.ALLOW_ONCE,
    "a": Decision.ALLOW_ALWAYS,
    "always": Decision.ALLOW_ALWAYS,
    "n": Decision.DENY_ONCE,
    "no": Decision.DENY_ONCE,
    "deny": Decision.DENY_ONCE,
    "never": Decision.DENY_ALWAYS,
    "deny_always": Decision.DENY_ALWAYS,
}

#: ``read_line(prompt, stream) -> str``; raises ``EOFError`` on end of input.
ReadLine = Callable[[str, TextIO], str]


def _default_read(prompt: str, stream: TextIO) -> str:
    if prompt:
        stream.write(prompt)
        stream.flush()
    return input()


def _sanitize(value: object) -> str:
    """Render untrusted text with every control character made visible.

    ESC, C0, C1, and DEL are escaped (``\\xNN``) rather than written raw, so a
    malicious tool name/key/preview/suggestion cannot inject terminal escape
    sequences (OSC/CSI) or control bytes into stderr. Intentional whitespace is
    turned into a single visible space by :func:`_clip`.
    """
    out: list[str] = []
    for char in str(value):
        code = ord(char)
        if char in "\t\n\r\v\f":
            out.append(" ")
        elif code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif unicodedata.category(char) == "Cf":
            # Unicode format controls (bidi overrides/isolates, zero-width
            # joiners, soft hyphen, BOM, ...) can reorder or hide prompt text.
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def _clip(value: object, limit: int = _KEY_LIMIT) -> str:
    """Single-line, control-free, length-capped rendering of untrusted data."""
    text = " ".join(_sanitize(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _describe(data: Mapping[str, Any], index: int) -> str:
    tool = _clip(data.get("tool") or "?", 80)
    bundle = data.get("bundle")
    key = data.get("key")
    preview = data.get("preview")
    suggestions = data.get("suggestions") or ()
    header = f"Permission requested (#{index}): {tool}"
    if bundle:
        header += f" [{_clip(bundle, 40)}]"
    lines = [header]
    if key:
        lines.append(f"  key: {_clip(key)}")
    elif preview:
        lines.append(f"  preview: {_clip(preview)}")
    if suggestions:
        shown = ", ".join(_clip(item, 80) for item in list(suggestions)[:4])
        lines.append(f"  suggestions: {shown}")
    default_rule = data.get("default_rule")
    persistence_available = data.get("persistence_available", True)
    if default_rule and persistence_available:
        lines.append(f"  always persists: {_clip(default_rule, 160)}")
    else:
        # A keyed request that cannot be represented exactly must not imply a
        # whole-tool grant; make the once-only fallback explicit.
        lines.append("  always persists: unavailable; [a] grants once only")
    return "\n".join(lines)


def _parse_decision(raw: str) -> Decision | None:
    return _DECISIONS.get(raw.strip().lower())


class Approver:
    """Interactive, terminal-side approval driver for one attended session.

    The approver owns no session state beyond a display counter; the caller
    resolves the returned :class:`~nexus.tools.permissions.Decision` through the
    live session handle.
    """

    def __init__(
        self,
        *,
        stderr: TextIO,
        read_line: ReadLine | None = None,
        max_attempts: int = _MAX_PROMPT_ATTEMPTS,
    ) -> None:
        self._stderr = stderr
        self._read = read_line or _default_read
        self._max_attempts = max(1, max_attempts)
        self._count = 0

    def _write(self, text: str) -> None:
        self._stderr.write(text)
        self._stderr.flush()

    async def prompt(self, data: Mapping[str, Any]) -> Decision:
        """Display one request and return the chosen decision (never ``None``)."""
        self._count += 1
        self._write(_describe(data, self._count) + "\n")
        for _ in range(self._max_attempts):
            try:
                raw = await asyncio.to_thread(self._read, _PROMPT, self._stderr)
            except EOFError:
                self._write("  input closed; denying this request once\n")
                return Decision.DENY_ONCE
            decision = _parse_decision(raw)
            if decision is not None:
                self._write(f"  -> {decision.value}\n")
                return decision
            self._write("  unrecognized choice; enter y, a, n, or never\n")
        self._write("  too many invalid entries; denying this request once\n")
        return Decision.DENY_ONCE


async def _consume(
    session: Any,
    message: str,
    *,
    json_output: bool,
    approver: Approver | None,
    stdout: TextIO,
    stderr: TextIO,
) -> Event | None:
    """Consume one ``send`` stream, rendering it; return the terminal event.

    Approval resolution happens *inside* the ``async for`` so the producer task
    stays live and unblocks as soon as the decision is recorded.
    """
    terminal: Event | None = None
    streamed_text = False
    async with aclosing(session.send(message)) as events:
        async for event in events:
            if terminal is None and event.type in _TERMINAL:
                terminal = event

            if json_output:
                stdout.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                stdout.flush()
            elif event.type == "model.started":
                streamed_text = False
            elif event.type == "text.delta":
                text = event.data.get("text") or ""
                if text:
                    stdout.write(text)
                    stdout.flush()
                    streamed_text = True
            elif event.type == "text":
                text = event.data.get("text") or ""
                if not streamed_text and text:
                    stdout.write(text)
                if text or streamed_text:
                    stdout.write("\n")
                    stdout.flush()
                streamed_text = False

            if event.type == "turn.failed":
                detail = event.data.get("error") or "turn failed"
                stderr.write(f"Error: {detail}\n")
                stderr.flush()
            elif event.type == "turn.cancelled":
                stderr.write("Cancelled.\n")
                stderr.flush()

            if approver is not None and event.type == "permission.requested":
                decision = await approver.prompt(event.data)
                request_id = event.data.get("id")
                if request_id and not session.resolve_permission(
                    request_id, decision
                ):
                    # The request could not be resolved (stale/unknown id). The
                    # producer is still parked waiting for this decision, so
                    # surface the error and cancel safely rather than deadlock.
                    stderr.write(
                        f"Error: permission request {request_id!r} is no "
                        "longer pending; cancelling the turn\n"
                    )
                    stderr.flush()
                    cancel = getattr(session, "cancel", None)
                    if callable(cancel):
                        cancel("permission request was no longer pending")

    return terminal


def _exit_code(terminal: Event | None) -> int:
    if terminal is None:
        return 1
    if terminal.type == "turn.failed":
        return 1
    if terminal.type == "turn.cancelled":
        return 130
    return 0


async def _run_once(runtime: Any, args: Any, *, read_line: ReadLine | None) -> int:
    json_output = bool(getattr(args, "json", False))
    session = runtime.session(args.session)
    approver: Approver | None = None
    if not json_output:
        session.mark_attended()
        approver = Approver(stderr=sys.stderr, read_line=read_line)
    message = sys.stdin.read() if args.message == "-" else args.message
    terminal = await _consume(
        session,
        message,
        json_output=json_output,
        approver=approver,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if terminal is None:
        sys.stderr.write("Error: the turn ended without a terminal event\n")
        sys.stderr.flush()
    return _exit_code(terminal)


async def _chat(runtime: Any, args: Any, *, read_line: ReadLine | None) -> int:
    session = runtime.session(args.session).mark_attended()
    sys.stdout.write(
        f"Nexus native · session {args.session} · /exit to quit · Ctrl-C to stop\n"
    )
    sys.stdout.flush()
    approver = Approver(stderr=sys.stderr, read_line=read_line)
    read = read_line or _default_read
    while True:
        try:
            message = await asyncio.to_thread(read, "you> ", sys.stdout)
        except EOFError:
            return 0
        if message.strip() == "/exit":
            return 0
        if not message.strip():
            continue
        try:
            terminal = await _consume(
                session,
                message,
                json_output=False,
                approver=approver,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            sys.stderr.write(f"Error: {exc}\n")
            sys.stderr.flush()
            continue
        if terminal is None:
            sys.stderr.write("Error: the turn ended without a terminal event\n")
            sys.stderr.flush()


def build_runtime(workspace: Path) -> Any:
    """Construct the native :class:`~nexus.runtime.Runtime` for ``workspace``.

    Imported lazily so ``import nexus.cli`` (and the legacy path) never pulls the
    Runtime, httpx, or the provider adapters. Tests monkeypatch this seam or pass
    ``runtime_factory`` to run offline against a ``ScriptedProvider``.
    """
    from ..runtime import Runtime

    return Runtime(workspace)


def run_native(
    args: Any,
    *,
    runtime_factory: Callable[[Path], Any] | None = None,
    read_line: ReadLine | None = None,
) -> int:
    """Run one native turn (``nexus native-run``); return a process exit code."""
    factory = runtime_factory or build_runtime
    runtime = factory(Path(args.workspace).resolve())
    return asyncio.run(_drive_run(runtime, args, read_line=read_line))


async def _drive_run(runtime: Any, args: Any, *, read_line: ReadLine | None) -> int:
    try:
        return await _run_once(runtime, args, read_line=read_line)
    finally:
        await runtime.aclose()


def chat_native(
    args: Any,
    *,
    runtime_factory: Callable[[Path], Any] | None = None,
    read_line: ReadLine | None = None,
) -> int:
    """Open the native interactive prompt (``nexus native-chat``)."""
    factory = runtime_factory or build_runtime
    runtime = factory(Path(args.workspace).resolve())
    return asyncio.run(_drive_chat(runtime, args, read_line=read_line))


async def _drive_chat(runtime: Any, args: Any, *, read_line: ReadLine | None) -> int:
    try:
        return await _chat(runtime, args, read_line=read_line)
    finally:
        await runtime.aclose()
