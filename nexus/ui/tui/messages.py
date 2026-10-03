"""Typed messages between the event bridge and Textual widgets."""

from __future__ import annotations

import asyncio

from textual.message import Message

from ...events import Event


class EventReceived(Message):
    """A daemon event delivered to the UI thread."""

    def __init__(self, event: Event, handled: asyncio.Future[None] | None = None) -> None:
        super().__init__()
        self.event = event
        self.handled = handled


class TurnFinished(Message):
    """The event bridge observed a terminal turn event."""

    def __init__(self, event: Event) -> None:
        super().__init__()
        self.event = event


class StreamDisconnected(Message):
    """The live subscription ended unexpectedly."""


class PermissionRequested(Message):
    """A permission request needs an attended user decision."""

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data


class InputSubmitted(Message):
    """The chat editor submitted a prompt."""

    def __init__(self, content: str, mode: str = "steer") -> None:
        super().__init__()
        self.content = content
        self.mode = mode


class CancelRequested(Message):
    """The user asked to cancel the current turn."""


class QuitRequested(Message):
    """The user requested a clean application exit."""


class AgentPickerRequested(Message):
    """The active root-agent badge was clicked or activated."""


class AgentSelected(Message):
    """The picker selected a root agent name."""

    def __init__(self, name: str | None) -> None:
        super().__init__()
        self.name = name


class AgentOpenRequested(Message):
    """An inline Task card or nested inspector row was opened."""

    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id


__all__ = [
    "AgentOpenRequested", "AgentPickerRequested", "AgentSelected", "CancelRequested", "EventReceived",
    "InputSubmitted", "PermissionRequested", "QuitRequested", "StreamDisconnected", "TurnFinished",
]
