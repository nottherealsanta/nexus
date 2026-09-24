"""Attended permission response screen; policy and race arbitration stay daemon-side."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..cli.approve import describe


class PermissionScreen(ModalScreen[str | None]):
    """Present the request and return one of the protocol decision strings."""

    DECISION_KEYS = {
        "y": "allow_once", "a": "allow_always", "n": "deny_once", "d": "deny_always",
    }

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data
        if not data.get("persistence_available", True):
            self.DECISION_KEYS = {"y": "allow_once", "a": "allow_once", "n": "deny_once", "d": "deny_once"}

    def compose(self) -> ComposeResult:
        persistence = bool(self.data.get("persistence_available", True))
        with Vertical(id="permission-dialog"):
            yield Static(describe(self.data), id="permission-description", markup=False)
            yield Button("Allow once [Y]", id="allow-once", variant="success")
            yield Button("Allow once [A] · persistence unavailable" if not persistence else "Allow always [A]", id="allow-always")
            yield Button("Deny once [N]", id="deny-once", variant="warning")
            yield Button("Deny once [D] · persistence unavailable" if not persistence else "Deny always [D]", id="deny-always", variant="error")

    def on_mount(self) -> None:
        self.query_one("#allow-once", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {
            "allow-once": "allow_once", "allow-always": "allow_always",
            "deny-once": "deny_once", "deny-always": "deny_always",
        }
        decision = choices.get(event.button.id or "", "deny_once")
        if not self.data.get("persistence_available", True) and decision in {"allow_always", "deny_always"}:
            decision = "allow_once" if decision == "allow_always" else "deny_once"
        self.dismiss(decision)

    def on_key(self, event) -> None:
        decision = self.DECISION_KEYS.get(event.key.casefold())
        if decision:
            event.stop()
            self.dismiss(decision)
        elif event.key == "escape":
            event.stop()
            self.dismiss("deny_once")


__all__ = ["PermissionScreen"]
