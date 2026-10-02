"""Shared terminal shortcut reference and leader table (PLAN §14.11)."""
from __future__ import annotations

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
    ("ctrl+u", "show_usage", "Provider usage and limits"),
    ("a", None, "Open the root-agent picker"),
    ("shift+tab", None, "Cycle root agent"),
    ("ctrl+c", "cancel_turn", "Return to conversation or cancel the active turn"),
    ("ctrl+r", "reconnect", "Reconnect"),
    ("ctrl+q", "quit_shell", "Quit"),
    ("escape", None, "Return to conversation"),
    ("escape twice", None, "Stop the active turn (within 1.5 seconds)"),
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
    ("u", "show_usage", "Provider usage and limits"),
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

