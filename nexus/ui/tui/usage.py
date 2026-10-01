"""Provider usage modal: plan limits for every connected provider (Ctrl+U, ``/usage``).

One ``ProvidersUsage`` host command fills it; each provider is a labelled
section with one bar per limit window (5-hour, weekly, monthly…), its reset
time, notes and the endpoint the numbers came from. Wording comes from
``ui_support/usage.py`` so the web modal (``js/usage.js``) reads the same.
``r`` refreshes; Escape or ``q`` closes.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ...ui_support import usage
from ...ui_support.text import sanitize

_TONES = {"ok": "green", "warn": "yellow", "critical": "red", "unknown": "dim"}


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def render_usage(result: Any, now: float | None = None) -> Text:
    """The modal body for one ``ProvidersUsageResult``."""
    out = Text()
    rows = list(_field(result, "providers", ()) or ())[:8]
    if not rows:
        out.append("No connected provider reports usage.\n", style="bold")
        out.append("Connect ChatGPT, Claude, GitHub Copilot or OpenCode Go in Settings → Providers (Ctrl+S).\n", style="dim")
    # One label column for every provider so the bars line up down the modal.
    width = max((len(sanitize(str(_field(w, "label", "") or "Limit"), 40))
                 for row in rows for w in list(_field(row, "windows", ()) or ())[:16]), default=0)
    for index, row in enumerate(rows):
        if index:
            out.append("\n")
        out.append(sanitize(usage.heading(row), 80) + "\n", style="bold")
        error = _field(row, "error")
        if error:
            out.append("  Unavailable: ", style="red")
            out.append(sanitize(str(error), 240) + "\n")
        windows = list(_field(row, "windows", ()) or ())[:16]
        if not windows and not error:
            out.append("  No limit windows reported.\n", style="dim")
        for window in windows:
            label = sanitize(str(_field(window, "label", "") or "Limit"), 40)
            out.append(f"  {label:<{width}}  ")
            out.append(usage.bar(window), style=_TONES[usage.tone(window)])
            out.append("  " + sanitize(usage.summary(window, now), 200) + "\n")
        for note in list(_field(row, "notes", ()) or ())[:8]:
            out.append(f"  · {sanitize(str(note), 160)}\n", style="dim")
        if source := _field(row, "source"):
            out.append(f"  Source: {sanitize(str(source), 80)}\n", style="dim")
    missing = [sanitize(str(name), 40) for name in list(_field(result, "not_connected", ()) or ())[:8]]
    if missing and rows:
        out.append("\nNot connected: " + ", ".join(missing) + "\n", style="dim")
    if fetched := usage.fetched_text(_field(result, "fetched_at")):
        out.append(f"\n{fetched} · r refresh · Esc close", style="dim")
    return out


class UsageScreen(ModalScreen[None]):
    """Read-only usage and limits for all connected providers."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "dismiss", "Close"), ("q", "dismiss", "Close"), ("r", "refresh", "Refresh"),
    ]
    DEFAULT_CSS = """
    UsageScreen {
        align: center middle;
        background: $background 70%;
    }
    #usage-dialog {
        width: 90%;
        max-width: 112;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        background: $panel;
        border: tall $border;
    }
    #usage-title {
        height: auto;
        margin-bottom: 1;
        color: $accent;
        text-style: bold;
    }
    #usage-body {
        height: auto;
    }
    """

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="usage-dialog"):
            yield Static("Provider usage and limits", id="usage-title", markup=False)
            yield Static("Reading usage from connected providers…", id="usage-body", markup=False)

    def on_mount(self) -> None:
        self.call_after_refresh(lambda: self.query_one("#usage-dialog", VerticalScroll).focus())
        self.action_refresh()

    def action_refresh(self) -> None:
        self.run_worker(self._load(), group="usage", exclusive=True)

    async def action_dismiss(self, result: None = None) -> None:
        self.dismiss(result)

    async def _load(self) -> None:
        body = self.query_one("#usage-body", Static)
        body.update(Text("Reading usage from connected providers…", style="dim"))
        try:
            result = await self._client.providers_usage()
        except Exception as exc:  # noqa: BLE001 - show the host/transport error
            body.update(Text(f"Usage unavailable: {sanitize(str(exc), 200)}", style="red"))
            return
        if self.is_mounted:
            body.update(render_usage(result))


__all__ = ["UsageScreen", "render_usage"]
