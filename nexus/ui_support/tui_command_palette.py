"""Textual command-palette entries and the keyboard shortcut reference (PLAN §14.11)."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.command import Hit, Hits, Provider
from textual.screen import Screen
from textual.widgets import Static

from ..ui.cli import commands

SHORTCUTS: tuple[tuple[str, str | None, str], ...] = (
    ("enter", None, "Send message (queue while working)"),
    ("ctrl+enter", None, "Steer at the next model step"),
    ("alt+enter", None, "Interrupt and send message"),
    ("shift+enter", None, "Insert newline (or Ctrl+J)"),
    ("ctrl+p", "command_palette", "Commands (Show keyboard shortcuts, chat commands)"),
    ("ctrl+n", "new_session", "New session"),
    ("ctrl+o", "list_sessions", "List sessions"),
    ("ctrl+f", "fork_session", "Fork session"),
    ("ctrl+g", "pick_agent", "Open the root-agent picker"),
    ("ctrl+b", "toggle_sessions", "Toggle the sessions sidebar"),
    ("ctrl+l", "toggle_details", "Toggle the details sidebar"),
    ("ctrl+s", "open_settings", "Settings"),
    ("ctrl+i", "open_context", "Inspect context preview and usage"),
    ("ctrl+t", None, "Cycle root reasoning effort"),
    ("ctrl+space", "toggle_voice", "Dictate (voice input)"),
    ("ctrl+e", None, "Toggle the Logs drawer"),
    ("a", None, "Open the root-agent picker"),
    ("shift+tab", None, "Cycle root agent"),
    ("ctrl+c", "cancel_turn", "Cancel the active turn"),
    ("ctrl+r", "reconnect", "Reconnect"),
    ("ctrl+q", "quit_shell", "Quit"),
    ("escape", None, "Back from an agent transcript"),
)


#: Ctrl+X leader: press Ctrl+X, then one letter. Rows are (letter, app action, description).
LEADER_KEY = "ctrl+x"
LEADER_SHORTCUTS: tuple[tuple[str, str, str], ...] = (
    ("m", "open_model_picker", "Choose model"),
    ("v", "start_voice", "Dictate; any key stops, Esc discards"),
    ("n", "new_session", "New session"),
    ("o", "list_sessions", "List sessions"),
    ("f", "fork_session", "Fork session"),
    ("g", "pick_agent", "Open the root-agent picker"),
    ("b", "toggle_sessions", "Toggle the sessions sidebar"),
    ("l", "toggle_details", "Toggle the details sidebar"),
    ("s", "open_settings", "Settings"),
    ("i", "open_context", "Inspect context preview and usage"),
    ("e", "toggle_logs", "Toggle the Logs drawer"),
    ("t", "cycle_reasoning_effort", "Cycle root reasoning effort"),
    ("r", "reconnect", "Reconnect"),
    ("?", "show_shortcuts", "Show keyboard shortcuts"),
)


def _shortcut_lines() -> tuple[str, ...]:
    labels = [(key.title(), description) for key, _, description in SHORTCUTS]
    leader = [(f"Ctrl+X {key.upper()}", description) for key, _, description in LEADER_SHORTCUTS]
    width = max(len(key) for key, _ in (*labels, *leader))
    return (
        "Keyboard shortcuts",
        *(f"  {key:<{width}} {description}" for key, description in labels),
        "",
        "Ctrl+X leader (press Ctrl+X, then a key)",
        *(f"  {key:<{width}} {description}" for key, description in leader),
    )


KEYBOARD_SHORTCUTS = _shortcut_lines()


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
