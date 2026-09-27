"""Attended permission response screen; policy and race arbitration stay daemon-side."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..cli.approve import approval_data, describe


class PermissionScreen(ModalScreen[str | None]):
    """Present the request and return one of the protocol decision strings."""

    DECISION_KEYS: ClassVar[dict[str, str]] = {
        "y": "allow_once", "a": "allow_always", "n": "deny_once", "d": "deny_always",
    }

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = approval_data(data)
        if self.data.get("_targets_unavailable"):
            self.DECISION_KEYS = {
                "y": "deny_once", "a": "deny_once",
                "n": "deny_once", "d": "deny_always",
            }
        elif not self.data.get("persistence_available", True):
            self.DECISION_KEYS = {"y": "allow_once", "a": "allow_once", "n": "deny_once", "d": "deny_once"}

    def compose(self) -> ComposeResult:
        persistence = bool(self.data.get("persistence_available", True))
        targets_available = not self.data.get("_targets_unavailable", False)
        details_scroll = VerticalScroll(id="permission-details-scroll")
        details_scroll.styles.width = "1fr"
        details_scroll.styles.height = "1fr"
        details_scroll.styles.min_height = 3
        details_scroll.styles.margin_bottom = 1
        details_scroll.styles.scrollbar_size_vertical = 1
        with Vertical(id="permission-dialog"):
            with details_scroll:
                description = describe(self.data)
                if not targets_available:
                    description += "\n  approval: unavailable; this request will be denied"
                yield Static(description, id="permission-description", markup=False)
            yield Button(
                "Allow once [Y]" if targets_available else "Allow once [Y] · targets unavailable",
                id="allow-once",
                variant="success",
                disabled=not targets_available,
            )
            yield Button(
                "Allow once [A] · persistence unavailable"
                if targets_available and not persistence
                else "Allow always [A]" if targets_available
                else "Allow always [A] · targets unavailable",
                id="allow-always",
                disabled=not targets_available,
            )
            yield Button("Deny once [N]", id="deny-once", variant="warning")
            yield Button(
                "Deny always [D]"
                if self.data.get("_targets_unavailable") or persistence
                else "Deny once [D] · persistence unavailable",
                id="deny-always",
                variant="error",
            )

    def on_mount(self) -> None:
        focus_target = "#deny-once" if self.data.get("_targets_unavailable") else "#allow-once"
        self.query_one(focus_target, Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {
            "allow-once": "allow_once", "allow-always": "allow_always",
            "deny-once": "deny_once", "deny-always": "deny_always",
        }
        decision = choices.get(event.button.id or "", "deny_once")
        if self.data.get("_targets_unavailable") and decision in {"allow_once", "allow_always"}:
            decision = "deny_once"
        elif (
            not self.data.get("_targets_unavailable")
            and not self.data.get("persistence_available", True)
            and decision in {"allow_always", "deny_always"}
        ):
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
