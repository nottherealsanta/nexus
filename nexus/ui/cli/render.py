"""Plain one-shot/JSONL human rendering of the event stream (PLAN section 14.11).

Semantics live in the pure ``view/`` reducer; this module only turns an event
into terminal text. Textual chat renders through Markdown widgets instead.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TextIO

from ...events import Event
from ...ui_support.text import escape_controls, redact, sanitize

#: Event types that terminate a turn, shared by every runner.
TERMINAL_EVENTS = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})

class TerminalRenderer:
    """Render one event at a time to a terminal, deduping replays."""

    def __init__(
        self,
        stdout: TextIO,
        *,
        stderr: TextIO | None = None,
        show_thinking: bool = False,
        color: bool | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr if stderr is not None else stdout
        self.show_thinking = show_thinking
        self.color = False if color is None else bool(color)
        #: Highest rendered ``seq`` per session, so a switch still renders.
        self._last_seq: dict[str, int] = {}
        self._seen: set[str] = set()
        self._streamed = False

    # -- dedup -------------------------------------------------------------

    def _already_rendered(self, event: Event) -> bool:
        if event.seq and event.seq > 0:
            return event.seq <= self._last_seq.get(event.session or "", 0)
        return event.id in self._seen

    def _mark(self, event: Event) -> None:
        if event.seq and event.seq > 0:
            key = event.session or ""
            self._last_seq[key] = max(self._last_seq.get(key, 0), event.seq)
        else:
            self._seen.add(event.id)

    # -- IO ----------------------------------------------------------------

    def _out(self, text: str, role: str = "") -> None:
        self.stdout.write(text)
        self.stdout.flush()

    def _err(self, text: str, role: str = "") -> None:
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
        text = escape_controls(str(data.get("text") or ""))
        if text:
            self._out(text)
            self._streamed = True

    def _on_text(self, data: Mapping[str, Any]) -> None:
        text = escape_controls(str(data.get("text") or ""))
        if text and not self._streamed:
            self._out(text)
        if text or self._streamed:
            self._out("\n")
        self._streamed = False

    def _on_thinking_delta(self, data: Mapping[str, Any]) -> None:
        if not self.show_thinking:
            return
        text = sanitize(data.get("text"), 2000)
        if text:
            self._out(f"[thinking] {text}", "thinking")

    def _on_thinking(self, data: Mapping[str, Any]) -> None:
        if self.show_thinking and data.get("text"):
            self._out("\n", "thinking")

    def _on_tool_requested(self, data: Mapping[str, Any]) -> None:
        # ``tool.requested`` carries only the call id and name; the actionable
        # signal is the tool, not an invented preview.
        self._out(f"\n[tool] {sanitize(data.get('tool') or '?', 80)}\n", "tool")

    def _on_tool_started(self, data: Mapping[str, Any]) -> None:
        name = sanitize(data.get("tool") or "?", 80)
        bundle = data.get("bundle")
        suffix = f" [{sanitize(bundle, 40)}]" if bundle else ""
        self._out(f"[tool] {name}{suffix} running\u2026\n", "tool")

    def _on_tool_progress(self, data: Mapping[str, Any]) -> None:
        text = data.get("text") or ""
        if text:
            self._out(f"  {sanitize(text, 200)}\n")

    def _on_tool_completed(self, data: Mapping[str, Any]) -> None:
        # ``tool.completed`` carries no result payload, so this reports the
        # actionable status and duration only rather than a misleading preview.
        name = sanitize(data.get("tool") or "?", 80)
        marker = "error" if data.get("is_error") else "done"
        duration = data.get("duration_ms")
        suffix = f" ({duration}ms)" if isinstance(duration, int) else ""
        self._out(f"[tool] {name} {marker}{suffix}\n", "tool")

    def _on_tool_failed(self, data: Mapping[str, Any]) -> None:
        name = sanitize(data.get("tool") or "?", 80)
        code = data.get("code")
        prefix = f"[{sanitize(code, 24)}] " if code else ""
        error = sanitize(data.get("error") or "failed", 160)
        self._out(f"[tool] {name} failed: {prefix}{error}\n", "error")

    def _on_permission_requested(self, data: Mapping[str, Any]) -> None:
        tool = sanitize(data.get("tool") or "?", 80)
        if "targets" in data:
            targets = data.get("targets")
            summary = (
                f"{len(targets)} targets — open details"
                if isinstance(targets, list)
                else "targets unavailable — open details"
            )
            self._out(f"[permission] {tool} awaiting approval \u00b7 {summary}\n", "permission")
            return
        detail = data.get("key") or data.get("preview")
        shown = sanitize(detail, 100) if detail else ""
        self._out(f"[permission] {tool} awaiting approval" + (f" \u00b7 {shown}" if shown else "") + "\n", "permission")

    def _on_permission_resolved(self, data: Mapping[str, Any]) -> None:
        decision = sanitize(data.get("decision") or "?", 40)
        scope = data.get("scope")
        suffix = f" ({sanitize(scope, 60)})" if scope else ""
        self._out(f"[permission] {decision}{suffix}\n", "permission")

    def _on_context_compacted(self, data: Mapping[str, Any]) -> None:
        parts = [sanitize(data["strategy"], 40) if data.get("strategy") else "",
                 f"dropped {data['dropped']}" if data.get("dropped") else "",
                 f"evicted {data['evicted']}" if data.get("evicted") else "",
                 "summary" if data.get("summary_id") else ""]
        if any(parts): self._out(f"[context] compacted: {', '.join(p for p in parts if p)}\n", "context")

    def _on_turn_completed(self, data: Mapping[str, Any]) -> None:
        if self._streamed:
            self._out("\n")
        self._streamed = False

    def _on_turn_failed(self, data: Mapping[str, Any]) -> None:
        self._streamed = False
        self._err(f"Error: {sanitize(data.get('error') or 'turn failed', 200)}\n", "error")

    def _on_turn_cancelled(self, data: Mapping[str, Any]) -> None:
        self._streamed = False
        self._err("Cancelled.\n", "error")

    def _on_error(self, data: Mapping[str, Any]) -> None:
        detail = sanitize(data.get("message") or data.get("error") or "error", 200)
        self._err(f"Error: {detail}\n", "error")


def exit_code(terminal: Event | None) -> int:
    """Map a terminal event to a process exit code."""
    if terminal is None:
        return 1
    if terminal.type == "turn.failed":
        return 1
    if terminal.type == "turn.cancelled":
        return 130
    return 0


__all__ = [
    "TERMINAL_EVENTS",
    "TerminalRenderer",
    "escape_controls",
    "exit_code",
    "redact",
    "sanitize",
]
