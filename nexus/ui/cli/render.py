"""Terminal rendering of the event stream (PLAN section 14.11).

The renderer is deliberately thin: semantics live in the pure ``view/`` reducer,
and this module only turns an event into terminal text. Two properties matter
and are pinned by tests:

* **Dedup.** Streaming providers emit ``text.delta`` then a finalized ``text``;
  a naive renderer prints the response twice. The renderer suppresses the final
  text when deltas already printed, and ignores any event whose ``seq`` it has
  already rendered, so a reconnect that replays the log is a no-op.
* **Untrusted text.** Tool names, keys, previews, and errors come from tools and
  from the model, so control characters are escaped before they reach a
  terminal (the same rule the retired ``ui/native.py`` approver followed).

The renderer writes human text to ``stdout`` and diagnostics to ``stderr``; the
JSONL path never uses it.
"""
from __future__ import annotations

import sys
import unicodedata
from collections.abc import Mapping
from typing import Any, TextIO

from ...events import Event

#: Event types that terminate a turn, shared by every runner.
TERMINAL_EVENTS = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})

_LIMIT = 240


def sanitize(value: object, limit: int = _LIMIT) -> str:
    """Single-line, control-free, length-capped rendering of untrusted text."""
    out: list[str] = []
    for char in str(value):
        code = ord(char)
        if char in "\t\n\r\v\f":
            out.append(" ")
        elif code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif unicodedata.category(char) == "Cf":
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    text = "".join(out)
    return text if len(text) <= limit else text[:limit] + "\u2026"


class TerminalRenderer:
    """Render one event at a time to a terminal, deduping replays."""

    def __init__(
        self,
        stdout: TextIO | None = None,
        *,
        stderr: TextIO | None = None,
        show_thinking: bool = False,
    ) -> None:
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.show_thinking = show_thinking
        self._last_seq = 0
        self._seen: set[str] = set()
        self._streamed = False

    # -- dedup -------------------------------------------------------------

    def _already_rendered(self, event: Event) -> bool:
        if event.seq and event.seq > 0:
            return event.seq <= self._last_seq
        return event.id in self._seen

    def _mark(self, event: Event) -> None:
        if event.seq and event.seq > 0:
            self._last_seq = max(self._last_seq, event.seq)
        else:
            self._seen.add(event.id)

    # -- IO ----------------------------------------------------------------

    def _out(self, text: str) -> None:
        self.stdout.write(text)
        self.stdout.flush()

    def _err(self, text: str) -> None:
        self.stderr.write(text)
        self.stderr.flush()

    # -- public API --------------------------------------------------------

    def render(self, event: Event) -> bool:
        """Render ``event``; return ``False`` when it was a duplicate."""
        if self._already_rendered(event):
            return False
        self._mark(event)
        data: Mapping[str, Any] = event.data if isinstance(event.data, Mapping) else {}
        handler = getattr(self, f"_on_{event.type.replace('.', '_')}", None)
        if handler is None:
            return True
        handler(data)
        return True

    # -- handlers ----------------------------------------------------------

    def _on_model_started(self, data: Mapping[str, Any]) -> None:
        self._streamed = False

    def _on_text_delta(self, data: Mapping[str, Any]) -> None:
        text = data.get("text") or ""
        if text:
            self._out(str(text))
            self._streamed = True

    def _on_text(self, data: Mapping[str, Any]) -> None:
        text = str(data.get("text") or "")
        if text and not self._streamed:
            self._out(text)
        if text or self._streamed:
            self._out("\n")
        self._streamed = False

    def _on_thinking_delta(self, data: Mapping[str, Any]) -> None:
        if not self.show_thinking:
            return
        text = data.get("text") or ""
        if text:
            self._out(f"[thinking] {text}")

    def _on_thinking(self, data: Mapping[str, Any]) -> None:
        if not self.show_thinking:
            return
        text = str(data.get("text") or "")
        if text:
            self._out("\n")

    def _on_tool_requested(self, data: Mapping[str, Any]) -> None:
        self._out(f"\n[tool] {sanitize(data.get('tool') or '?')}\n")

    def _on_tool_started(self, data: Mapping[str, Any]) -> None:
        name = sanitize(data.get("tool") or "?")
        bundle = data.get("bundle")
        suffix = f" [{sanitize(bundle, 40)}]" if bundle else ""
        self._out(f"[tool] {name}{suffix} running\u2026\n")

    def _on_tool_progress(self, data: Mapping[str, Any]) -> None:
        text = data.get("text") or ""
        if text:
            self._out(f"  {sanitize(text)}\n")

    def _on_tool_completed(self, data: Mapping[str, Any]) -> None:
        name = sanitize(data.get("tool") or "?")
        duration = data.get("duration_ms")
        marker = "error" if data.get("is_error") else "done"
        if isinstance(duration, int):
            self._out(f"[tool] {name} {marker} ({duration}ms)\n")
        else:
            self._out(f"[tool] {name} {marker}\n")

    def _on_tool_failed(self, data: Mapping[str, Any]) -> None:
        name = sanitize(data.get("tool") or "?")
        error = sanitize(data.get("error") or "failed")
        self._out(f"[tool] {name} failed: {error}\n")

    def _on_permission_requested(self, data: Mapping[str, Any]) -> None:
        tool = sanitize(data.get("tool") or "?")
        self._out(f"[permission] {tool} awaiting approval\n")

    def _on_permission_resolved(self, data: Mapping[str, Any]) -> None:
        decision = sanitize(data.get("decision") or "?")
        self._out(f"[permission] {decision}\n")

    def _on_turn_completed(self, data: Mapping[str, Any]) -> None:
        if self._streamed:
            self._out("\n")
        self._streamed = False

    def _on_turn_failed(self, data: Mapping[str, Any]) -> None:
        self._streamed = False
        detail = sanitize(data.get("error") or "turn failed")
        self._err(f"Error: {detail}\n")

    def _on_turn_cancelled(self, data: Mapping[str, Any]) -> None:
        self._streamed = False
        self._err("Cancelled.\n")

    def _on_error(self, data: Mapping[str, Any]) -> None:
        detail = sanitize(data.get("message") or data.get("error") or "error")
        self._err(f"Error: {detail}\n")


def exit_code(terminal: Event | None) -> int:
    """Map a terminal event to a process exit code."""
    if terminal is None:
        return 1
    if terminal.type == "turn.failed":
        return 1
    if terminal.type == "turn.cancelled":
        return 130
    return 0


__all__ = ["TERMINAL_EVENTS", "TerminalRenderer", "exit_code", "sanitize"]
