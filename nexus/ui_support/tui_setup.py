"""First-run setup: connect a provider, then chat (plan section 7).

The screen only asks the user to connect a provider. It reuses the Settings →
Providers pane for sign-in and lists the environment-key providers. As soon as
the host reports one connected (already connected ones count, in list order),
``SetupSave`` without a model saves that provider's newest model as the global
default and reloads the daemon's routes, and the screen closes into the chat.
``/model`` changes the model afterwards.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from ..client.protocol import Client, ClientError
from ..host import protocol as p
from .text import sanitize
from .tui_providers import PROVIDERS, ProvidersPane

_POLL_SECONDS = 2.0
_SIGNED_IN = {provider for provider, _, _ in PROVIDERS}


async def open_first_run_setup(app, client: Client) -> bool:
    """Open setup after bootstrap when this daemon reports first-run state."""
    try:
        status = await client.setup_status()
    except (ClientError, AssertionError):
        return False
    if status.required:
        app.push_screen(SetupScreen(client, status))
    return status.required


def block_unconfigured_turn(app, content: str) -> bool:
    """Keep the draft and reopen setup until a provider is connected."""
    if not getattr(app, "_setup_required", False):
        return False
    app.query_one("#chat-editor", TextArea).text = content
    app._sync_status("Connect a provider to start chatting", error=True)
    if not isinstance(app.screen, SetupScreen):
        app.push_screen(SetupScreen(app.controller.client))
    return True


class SetupScreen(ModalScreen[None]):
    """Connect a provider; the first connected one becomes the global default."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "close", "Not now")]

    def __init__(self, client: Client, status: p.SetupStatusResult | None = None) -> None:
        super().__init__()
        self.client = client
        self.status = status
        self._saving = False
        self._dismissed = False
        #: Saved but the daemon must restart: keep the message, stop polling.
        self._restart = False
        #: A provider whose save failed is not retried until "Try again".
        self._failed: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-dialog"):
            yield Static("Connect a provider", id="setup-title")
            yield Static(
                "Nexus starts with the newest model of the first provider you connect. "
                "Change it any time with /model.",
                id="setup-intro",
            )
            yield ProvidersPane(self.client, heading=False)
            yield Static("", id="setup-env")
            yield Static("", id="setup-message")
            with Horizontal(id="setup-buttons"):
                yield Button("Try again", id="setup-retry")
                yield Button("Not now", id="setup-close")

    def on_mount(self) -> None:
        self.query_one("#setup-retry", Button).display = False
        if self.status is not None:
            self._apply_status(self.status)
        self.run_worker(self._poll(), group="setup-poll", exclusive=True)
        self.set_interval(_POLL_SECONDS, lambda: self.run_worker(self._poll(), group="setup-poll", exclusive=True))

    def action_close(self) -> None:
        self._close()

    def _close(self) -> None:
        # A status poll and a finished save can both close the screen.
        if not self._dismissed:
            self._dismissed = True
            self.dismiss(None)

    async def _poll(self) -> None:
        if self._saving or self._dismissed or self._restart:
            return
        try:
            status = await self.client.setup_status()
        except (ClientError, AssertionError):
            self._message("Setup status unavailable. Check that the daemon is running.")
            return
        if self.is_mounted:
            self._apply_status(status)

    def _apply_status(self, status: p.SetupStatusResult) -> None:
        self.status = status
        rows = [row for row in status.providers if isinstance(row, dict)][:32]
        lines = []
        for row in rows:
            provider = str(row.get("id", ""))
            if provider in _SIGNED_IN:
                continue
            state = "● connected" if row.get("connected") is True else "○"
            lines.append(f"{state}  {sanitize(row.get('label', provider), 40)} · "
                         f"{sanitize(row.get('instruction', ''), 160)}")
        self.query_one("#setup-env", Static).update("\n".join(lines))
        if not status.required:
            self._close()
            return
        chosen = next((str(row.get("id", "")) for row in rows
                       if row.get("connected") is True and row.get("auto") is True
                       and str(row.get("id", "")) not in self._failed), "")
        if chosen and not self._saving:
            # Set before the worker starts, so the startup status and a poll save once.
            self._saving = True
            self.run_worker(self._complete(chosen), group="setup-save", exclusive=True)

    async def _complete(self, provider: str) -> None:
        self._message(f"Connected {sanitize(provider, 40)} · choosing its newest model…")
        try:
            result = await self.client.setup_save(provider)
        except ClientError as exc:
            self._failed.add(provider)
            self._message(f"Could not finish setup with {sanitize(provider, 40)}: {sanitize(str(exc), 200)}")
            self.query_one("#setup-retry", Button).display = True
            return
        finally:
            self._saving = False
        model = sanitize(result.global_model, 120)
        if result.restart_required:
            self._restart = True
            self._message(f"Saved {model} as the default. Restart the daemon (nexus daemon stop) to use it.")
            return
        await self._refresh_app(model)
        self._close()

    async def _refresh_app(self, model: str) -> None:
        """The reloaded routes serve the next turn; show the new model now."""
        app = self.app
        app._setup_required = False
        controller = getattr(app, "controller", None)
        if controller is not None:
            try:
                await controller.refresh_agent_metadata()
            except ClientError:
                pass
        for hook in ("_sync_agent", "_start_context_preview"):
            method = getattr(app, hook, None)
            if callable(method):
                method()
        status = getattr(app, "_sync_status", None)
        if callable(status):
            status(f"Using {model} · /model to change")

    def _message(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#setup-message", Static).update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "setup-retry":
            event.stop()
            self._failed.clear()
            event.button.display = False
            self.run_worker(self._poll(), group="setup-poll", exclusive=True)
        elif event.button.id == "setup-close":
            event.stop()
            self._close()


__all__ = ["SetupScreen", "block_unconfigured_turn", "open_first_run_setup"]
