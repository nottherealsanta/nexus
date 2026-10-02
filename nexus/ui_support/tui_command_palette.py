"""Textual command-palette entries and the keyboard shortcut reference (PLAN §14.11)."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.command import Hit, Hits, Provider
from textual.screen import Screen
from textual.widgets import Static

from ..ui.cli import commands

from .shortcuts import (SHORTCUTS as SHORTCUTS, LEADER_KEY as LEADER_KEY,
    LEADER_SHORTCUTS as LEADER_SHORTCUTS, KEYBOARD_SHORTCUTS as KEYBOARD_SHORTCUTS)


def model_reference(row: dict) -> str:
    provider, model_id = row.get("provider"), row.get("id")
    if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
        return f"{provider}/{model_id}"
    return ""


def model_display_name(row: dict) -> str:
    reference = model_reference(row)
    value = row.get("name")
    label = value.strip() if isinstance(value, str) else ""
    return label or reference or "?"


class ShortcutsScreen(Screen):
    """Read-only modal that shows the full keyboard reference."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "dismiss", "Close"), ("q", "dismiss", "Close")
    ]

    def __init__(self, lines: tuple[str, ...]) -> None:
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        yield Static("\n".join(self._lines), id="shortcuts")


class ShortcutsCommandProvider(Provider):
    """Palette entry that opens the keyboard-shortcut reference."""

    async def search(self, query: str) -> Hits:
        app, matcher = self.app, self.matcher(query)
        label = "Show keyboard shortcuts"

        def show() -> None:
            app.push_screen(ShortcutsScreen(KEYBOARD_SHORTCUTS))

        if (score := matcher.match(label)) > 0:
            yield Hit(score, matcher.highlight(label), show, help=label)


class ChatCommandProvider(Provider):
    """Textual command palette backed by the established chat command specs."""

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        worktrees_score = matcher.match("/worktrees review child worktree lifecycle")
        if worktrees_score > 0:
            yield Hit(
                worktrees_score,
                matcher.highlight("/worktrees — review and manage child worktrees"),
                lambda: app.run_worker(
                    app._dispatch_chat_command("/worktrees"),
                    group="chat-command",
                    exclusive=True,
                ),
                help="Review, acknowledge, integrate, or discard daemon-owned child worktrees",
            )
        for spec in commands.SPECS:
            if spec.hidden:
                continue
            label = (
                f"Quit chat — {spec.summary}"
                if spec.name == "/exit"
                else f"{', '.join((spec.name, *spec.aliases))} {spec.usage} — {spec.summary}".strip()
            )
            raw = "/exit" if spec.name == "/exit" else spec.name
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    lambda command=raw: app.run_worker(
                        app._dispatch_chat_command(command),
                        group="chat-command",
                        exclusive=True,
                    ),
                    help=spec.summary,
                )
