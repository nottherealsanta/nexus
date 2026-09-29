"""Small Textual-only presentation widgets for the Nexus shell."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Collapsible, Markdown, Static, TextArea

from ..client.protocol import ClientError
from ..host import TransportError
from ..host import protocol as p
from ..ui.cli import commands
from ..ui.tui.messages import InputSubmitted
from .context import (
    MAX_ROWS,
    MAX_TEXT,
    ContextEntry,
    ContextGroup,
    _compact_tokens,
    context_detail_usage,
    context_groups,
    context_summary,
    context_usage,
    render_context_summary,
)
from .text import escape_controls, redact, sanitize
from .tui_history import append_history, load_history
from .tui_list import ListItem, ListPanel

_WORKTREE_REVIEW_ID = re.compile(r"^[0-9a-f]{32}$")
_WORKTREE_DIGEST = re.compile(r"^[0-9a-f]{64}$")


_AGENT_COLORS = (
    "#a78bfa",  # violet
    "#69b7d5",  # blue
    "#86b97a",  # green
    "#d18a38",  # amber
    "#dc8295",  # rose
    "#55b9a5",  # teal
)


def agent_color(name: str) -> str:
    """Return a stable readable identity color when the host has none yet."""
    digest = hashlib.sha256(name.casefold().encode("utf-8")).digest()
    return _AGENT_COLORS[int.from_bytes(digest[:4], "big") % len(_AGENT_COLORS)]


def _model_display_name(model: str | None) -> str:
    """Turn provider ids into compact readable composer labels."""
    if not model:
        return "Default"
    parts = model.split("-")
    if parts[0].casefold() == "gpt" and len(parts) > 1:
        return f"GPT-{parts[1]} {' '.join(part.title() for part in parts[2:])}".rstrip()
    return model


def _provider_display_name(provider: str | None) -> str:
    """Present route aliases by their provider identity, not internal id."""
    if not provider:
        return ""
    labels = {
        "codex": "OpenAI",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "google": "Google",
        "gemini": "Google",
        "ollama": "Ollama",
    }
    return labels.get(provider.casefold(), provider)


class ChatEditor(TextArea):
    """Multiline prompt editor with an unambiguous submit/newline contract.

    Enter submits; Shift+Enter (or Alt+Enter/Ctrl+Enter) inserts a newline.
    Handlers live on the focused editor because Textual dispatches keys there
    first, so a container ``on_key`` would only run after TextArea inserted its
    default newline for ``enter``.

    ``shift+enter`` / ``ctrl+enter`` / ``alt+enter`` are the key names Textual
    derives from a Kitty-capable terminal, and from the xterm ``modifyOtherKeys``
    encoding after :mod:`nexus.ui.tui.keys` rewrites it. ``ctrl+shift+enter`` is
    the name the browser serving bridge produces (see ``tests/browser_serve.py``):
    the bridge reports both modifiers, so Ctrl+Shift+Enter also newlines. ``ctrl+j``
    is the ``LF`` fallback that works in every terminal, including those that
    collapse Shift+Enter onto a bare carriage return.
    """

    #: Modified-Enter key names that insert a newline instead of submitting.
    NEWLINE_KEYS: frozenset[str] = frozenset(
        {"shift+enter", "ctrl+enter", "ctrl+shift+enter", "alt+enter", "ctrl+j"}
    )

    #: TextArea binds word motion to Ctrl+Left/Right only. macOS terminals send
    #: Option+Left/Right as ``alt+left``/``alt+right`` (``ESC [ 1 ; 3 D``), so the
    #: editor maps those, and their Shift-selecting forms, to the same actions.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("alt+left", "cursor_word_left", "Cursor word left", show=False),
        Binding("alt+right", "cursor_word_right", "Cursor word right", show=False),
        Binding("alt+shift+left", "cursor_word_left(True)", "Cursor left word select", show=False),
        Binding("alt+shift+right", "cursor_word_right(True)", "Cursor right word select", show=False),
    ]

    async def _on_paste(self, event: events.Paste) -> None:
        """Collapse large terminal pastes into editable composer attachments."""
        if self._collapse_paste(event.text):
            event.stop()
            event.prevent_default()
            return
        await super()._on_paste(event)

    def action_paste(self) -> None:
        """Apply the same collapse rule to Textual's local clipboard action."""
        text = self.app.clipboard
        if self._collapse_paste(text):
            return
        super().action_paste()

    def _collapse_paste(self, text: str) -> bool:
        if not is_large_paste(text):
            return False
        self.parent.add_pasted_content(text, self)
        return True

    class SubmitRequested(Message):
        """Enter was pressed with a non-empty draft."""

        def __init__(self, content: str) -> None:
            super().__init__()
            self.content = content

    def on_key(self, event) -> None:
        actions = {
            "ctrl+n": "action_new_session",
            "ctrl+o": "action_list_sessions",
            "ctrl+f": "action_fork_session",
        }
        if action := actions.get(event.key):
            event.stop()
            event.prevent_default()
            self.app.run_worker(getattr(self.app, action)(), group="editor-command")
        elif event.key in {"ctrl+t", "ctrl+e"} and self.app._is_main_screen():
            event.stop()
            event.prevent_default()
            if event.key == "ctrl+t":
                self.app.run_worker(self.app.action_cycle_reasoning_effort(), group="reasoning-effort")
            else:
                self.app.action_toggle_logs()
        elif event.key in {"escape", "up", "down", "enter", "tab"} and self.parent.completion_visible:
            event.stop()
            event.prevent_default()
            if event.key == "escape":
                self.parent.dismiss_completion()
            elif event.key in {"up", "down"}:
                self.parent.move_completion(-1 if event.key == "up" else 1)
            elif event.key == "enter" and self.parent.is_completed_standalone_command(self):
                command = self.text if self.parent.is_exact_argument_completion(self) else self.parent.selected_completion
                self.parent.close_completion()
                if command:
                    self.post_message(self.SubmitRequested(command))
                    self.clear()
            else:
                self.parent.accept_completion()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.text.strip():
                self.post_message(self.SubmitRequested(self.text))
                self.clear()
        elif event.key in self.NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
        elif event.key in {"up", "down"} and self.parent.recall_history(
            -1 if event.key == "up" else 1, self
        ):
            event.stop()
            event.prevent_default()
        else:
            # TextArea moves its cursor after this handler. Refresh once the
            # default key action has completed so suggestions track that cursor.
            self.app.call_after_refresh(self.parent.refresh_completion)

class CompletionPopup(ListPanel):
    """Small, non-focusable completion list rendered above composer metadata."""

    can_focus = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.display = False

    def show_items(self, items: list[str], selected: int) -> None:
        """Full-width rows with an eight-row viewport and native scrolling."""
        rows = []
        for item in items:
            spec = commands.BY_NAME.get(item) if item.startswith("/") else None
            summary = f"{spec.summary} ({spec.name} {spec.usage})" if spec and spec.usage else spec.summary if spec else ""
            if spec and spec.aliases:
                summary += f" · also {', '.join(spec.aliases)}"
            summary = sanitize(summary, 120)
            name = sanitize(item, 60)
            rows.append(ListItem(name, summary))
        self.set_items(rows, selected=selected)


class Transcript(Markdown):
    """Markdown transcript with safe handling for streamed untrusted content."""

    can_focus = False

    def __init__(self, content: str = "", **kwargs) -> None:
        super().__init__(content, open_links=False, **kwargs)
        self._stream = None
        self._pending = ""
        self._finished = False

    async def on_mount(self) -> None:
        if self._pending and not self._finished:
            self._stream = Markdown.get_stream(self)
            await self._stream.write(self._pending)
            self._pending = ""
        elif not self._finished:
            self._stream = Markdown.get_stream(self)

    async def append_text(self, text: str) -> None:
        if not text or self._finished:
            return
        # Controls are neutralized, markup is parsed as Markdown (never Rich
        # markup), and links are not opened by terminal interaction.
        safe = escape_controls(text)[:8192]
        if self.is_mounted:
            if self._stream is None:
                self._stream = Markdown.get_stream(self)
            await self._stream.write(safe)
        else:
            self._pending += safe

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None
        elif self._pending:
            await self.update(self._pending)
            self._pending = ""

    def begin_next(self) -> None:
        """Start a new stream segment after a completed turn."""
        self._finished = False
        self._stream = None

    async def on_unmount(self) -> None:
        await self.finish()


class TranscriptPane(Vertical):
    """Scrollable transcript root with stable extension points for detail panes."""

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll

        with VerticalScroll(id="transcript-scroll"):
            yield Transcript("", id="transcript")

    def set_markdown(self, content: str) -> None:
        self.query_one("#transcript", Transcript).update(content)


PASTE_COLLAPSE_LINES = 20
PASTE_COLLAPSE_CHARS = 2000
MAX_PASTED_CONTENT_CHARS = 128_000
MAX_PASTED_CONTENT_ATTACHMENTS = 8


def is_large_paste(text: str) -> bool:
    """Whether pasted text should be kept in a composer attachment."""
    return len(text) > PASTE_COLLAPSE_CHARS or len(text.splitlines()) > PASTE_COLLAPSE_LINES


class PastedContentScreen(ModalScreen[tuple[str, str] | None]):
    """Preview and edit the full content behind a collapsed paste."""

    DEFAULT_CSS = """
    PastedContentScreen { align: center middle; background: $nx-scrim; }
    #pasted-content-dialog { width: 80%; height: 80%; max-width: 100; background: $nx-dialog; border: round $nx-border-focus; padding: 1 2; }
    #pasted-content-title { color: $nx-accent; text-style: bold; height: 1; margin-bottom: 1; }
    #pasted-content-editor { height: 1fr; }
    #pasted-content-actions { height: 3; align-horizontal: right; }
    #pasted-content-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def __init__(self, number: int, content: str) -> None:
        super().__init__()
        self.number = number
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="pasted-content-dialog"):
            yield Static(f"Pasted content #{self.number}", id="pasted-content-title")
            yield TextArea(self.content, id="pasted-content-editor", soft_wrap=True)
            with Horizontal(id="pasted-content-actions"):
                yield Button("Remove", id="pasted-content-remove")
                yield Button("Cancel", id="pasted-content-cancel")
                yield Button("Save", variant="primary", id="pasted-content-save")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pasted-content-save":
            content = self.query_one("#pasted-content-editor", TextArea).text
            if len(content) > MAX_PASTED_CONTENT_CHARS:
                self.app.notify(
                    "Edited paste exceeds the 128,000 character attachment limit",
                    severity="warning",
                )
                return
            self.dismiss(("save", content))
        elif event.button.id == "pasted-content-remove":
            self.dismiss(("remove", ""))
        elif event.button.id == "pasted-content-cancel":
            self.dismiss(None)


class ChatInput(Vertical):
    """Keyboard-first multiline prompt editor.

    There are no send/cancel controls: Enter submits (handled by
    :class:`ChatEditor`); cancellation remains available through the command
    palette, ``/cancel``, and the terminal interrupt key.
    """

    def compose(self) -> ComposeResult:
        popup = CompletionPopup("", id="completion-popup")
        popup.display = False
        yield popup
        yield Vertical(id="paste-attachments")
        yield ChatEditor(id="chat-editor", soft_wrap=True, tab_behavior="indent")
        with Horizontal(id="runtime-info"):
            yield RootAgentBar(id="root-agent")
        with Horizontal(id="bottom-info"):
            yield ContextUsage("", id="context-usage", markup=False)

    async def on_mount(self) -> None:
        self._completion_task: asyncio.Task | None = None
        self._completion_generation = 0
        self._completion_token: tuple[str, int, int, int] | None = None
        self._completion_items: list[str] = []
        self._completion_selected = 0
        self._completion_query: str | None = None
        self._dismissed_token: tuple[str, int, int, int, str] | None = None
        self._history_index = -1
        self._history_value = ""
        self._history_draft = ""
        self._pasted_content: dict[int, str] = {}
        self._paste_markers: dict[int, str] = {}
        self._next_paste_id = 1
        self.query_one(CompletionPopup).display = False
        self._sync_completion_layout()
        self.query_one(ChatEditor).focus()

    @property
    def completion_visible(self) -> bool:
        return bool(self._completion_items)

    def add_pasted_content(self, content: str, editor: ChatEditor) -> None:
        """Replace the current selection with a marker backed by full text."""
        if len(content) > MAX_PASTED_CONTENT_CHARS:
            self.app.notify("Paste exceeds the 128,000 character attachment limit", severity="warning")
            return
        if len(self._pasted_content) >= MAX_PASTED_CONTENT_ATTACHMENTS:
            self.app.notify("Only 8 pasted attachments can be kept in one draft", severity="warning")
            return
        number = self._next_paste_id
        self._next_paste_id += 1
        # The short nonce distinguishes this marker from user-authored text
        # such as a literal "[Pasted #1]" in the same prompt.
        marker = f"[Pasted #{number} · {uuid.uuid4().hex[:8]}]"
        self._pasted_content[number] = content
        self._paste_markers[number] = marker
        replaced = editor._replace_via_keyboard(marker, *editor.selection)
        if replaced:
            editor.move_cursor(replaced.end_location)
        self._refresh_pasted_content()

    def _refresh_pasted_content(self) -> None:
        if not self.is_mounted:
            return
        row = self.query_one("#paste-attachments", Vertical)
        row.remove_children()
        row.display = bool(self._pasted_content)
        for number, content in self._pasted_content.items():
            lines = max(1, len(content.splitlines()))
            pill = Horizontal(
                Static(f"Pasted #{number} · {lines} lines", classes="pasted-content-label", markup=False),
                Button("×", id=f"pasted-content-remove-{number}", classes="pasted-content-remove"),
                classes="pasted-content-pill",
            )
            row.mount(pill)

    def _expand_pasted_content(self, text: str) -> str:
        markers = {marker: self._pasted_content[number]
                   for number, marker in self._paste_markers.items()
                   if number in self._pasted_content}
        if not markers:
            return text
        pattern = re.compile("|".join(re.escape(marker) for marker in markers))
        return pattern.sub(lambda match: markers[match.group(0)], text)

    def _sync_pasted_content_markers(self, text: str) -> None:
        """Drop attachments whose editor markers the user removed or replaced."""
        orphaned = [number for number, marker in self._paste_markers.items() if marker not in text]
        if not orphaned:
            return
        for number in orphaned:
            self._pasted_content.pop(number, None)
            self._paste_markers.pop(number, None)
        self._refresh_pasted_content()

    def _edit_pasted_content(self, number: int) -> None:
        content = self._pasted_content.get(number)
        if content is None:
            return

        def finished(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            action, value = result
            if action == "remove":
                self._remove_pasted_content(number)
            else:
                self._pasted_content[number] = value
                self._refresh_pasted_content()

        self.app.push_screen(PastedContentScreen(number, content), callback=finished)

    def _remove_pasted_content(self, number: int) -> None:
        self._pasted_content.pop(number, None)
        marker = self._paste_markers.pop(number, None)
        editor = self.query_one(ChatEditor)
        if marker:
            editor.text = editor.text.replace(marker, "")
        self._refresh_pasted_content()

    def on_click(self, event) -> None:
        widget = event.widget
        classes = getattr(widget, "classes", ())
        if "pasted-content-label" in classes:
            event.stop()
            row = widget.parent
            try:
                number = int(row.query_one("Button").id.rsplit("-", 1)[1])
            except (AttributeError, ValueError):
                return
            self._edit_pasted_content(number)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("pasted-content-remove-"):
            try:
                self._remove_pasted_content(int(button_id.rsplit("-", 1)[1]))
            except ValueError:
                return

    def recall_history(self, direction: int, editor: ChatEditor) -> bool:
        """Recall at the editor boundary, preserving a draft for the return trip."""
        lines = editor.text.splitlines() or [""]
        row = editor.cursor_location[0]
        if direction < 0 and row != 0 or direction > 0 and row != len(lines) - 1:
            return False
        if editor.text and (self._history_index < 0 or editor.text != self._history_value):
            return False
        view = self.app.controller.view
        session_prompts = [message.text for turn in view.turns for message in turn.messages
                           if message.role == "user" and message.text and len(message.text) <= 4096]
        entries = list(dict.fromkeys([*load_history(), *session_prompts]))[-500:]
        if not entries:
            return False
        if self._history_index < 0:
            if direction > 0:
                return False
            self._history_draft = editor.text
            index = len(entries) - 1
        else:
            index = self._history_index + direction
        if index >= len(entries):
            self._history_index = -1
            self._history_value = ""
            editor.text = self._history_draft
            return True
        index = max(0, index)
        self._history_index = index
        self._history_value = entries[index]
        editor.text = self._history_value
        editor.move_cursor((len(editor.text.splitlines()) - 1, len(editor.text.splitlines()[-1])))
        return True

    @property
    def selected_completion(self) -> str | None:
        if not self._completion_items:
            return None
        return self._completion_items[self._completion_selected]

    def close_completion(self) -> None:
        self._completion_generation += 1
        if self._completion_task is not None:
            self._completion_task.cancel()
            self._completion_task = None
        self._completion_items = []
        self._completion_token = None
        self._completion_query = None
        if self.is_mounted:
            popup = self.query_one(CompletionPopup)
            popup.display = False
            popup.set_items([])
            self._sync_completion_layout()

    def _sync_completion_layout(self) -> None:
        popup = self.query_one(CompletionPopup)
        popup.styles.display = "block" if self._completion_items else "none"
        # Let the border-box grow around its text rows. Setting this to the
        # item count clips the text when there is one suggestion because the
        # popup border consumes the only allocated row.
        popup.styles.height = "auto" if self._completion_items else 0

    def move_completion(self, delta: int) -> None:
        if not self._completion_items:
            return
        self._completion_selected = (self._completion_selected + delta) % len(self._completion_items)
        self.query_one(CompletionPopup).show_items(
            self._completion_items, self._completion_selected
        )

    def is_completed_standalone_command(self, editor: ChatEditor) -> bool:
        """Whether the active slash token is a standalone command draft."""
        if self._completion_token is None:
            return False
        marker, row, start, end = self._completion_token
        return self.is_exact_argument_completion(editor) or (
            marker == "/"
            and row == 0
            and start == 0
            and end == len(editor.text)
        )

    def is_exact_argument_completion(self, editor: ChatEditor) -> bool:
        if self._completion_token is None:
            return False
        marker, row, start, end = self._completion_token
        return (marker == "agent" and row == 0
                and end == len(editor.text) and editor.text[start:end] in self._completion_items)

    def accept_completion(self) -> None:
        if not self._completion_items or self._completion_token is None:
            return
        _, row, start, end = self._completion_token
        item = self._completion_items[self._completion_selected]
        editor = self.query_one(ChatEditor)
        editor.replace(item, (row, start), (row, end))
        self.close_completion()
        active = self._active_token(editor)
        if active is not None:
            self._dismissed_token = (active[0], active[1], active[2], active[3], active[4])
        editor.focus()

    @staticmethod
    def _active_token(editor: ChatEditor) -> tuple[str, int, int, int, str] | None:
        row, column = editor.cursor_location
        lines = editor.text.split("\n")
        if row >= len(lines):
            return None
        line = lines[row]
        column = min(column, len(line))
        if row == 0:
            argument = re.fullmatch(r"/(agent)\s+([^\s]*)", line[:column])
            if argument:
                token = argument.group(2)
                start = column - len(token)
                return argument.group(1), row, start, column, token
        match = re.search(r"(?:^|\s)([/@][^\s]*)$", line[:column])
        if match:
            token = match.group(1)
            start = match.end() - len(token)
            return token[0], row, start, start + len(token), token
        return None

    def refresh_completion(self) -> None:
        editor = self.query_one(ChatEditor)
        active = self._active_token(editor)
        if active is None:
            self.close_completion()
            return
        marker, row, start, end, token = active
        active_key = (marker, row, start, end, token)
        if active_key == self._dismissed_token:
            self.close_completion()
            return
        if self._dismissed_token is not None:
            self._dismissed_token = None
        if marker == "/":
            if (
                self._completion_token == (marker, row, start, end)
                and self._completion_query == token
                and self._completion_items
            ):
                return
            self._completion_generation += 1
            if self._completion_task is not None:
                self._completion_task.cancel()
                self._completion_task = None
            self._completion_items = []
            self._completion_token = None
            self._completion_query = token
            matches = sorted(
                spec.name
                for spec in commands.SPECS
                if not spec.hidden
                and any(name.casefold().startswith(token.casefold()) for name in (spec.name, *spec.aliases))
            )
            if matches:
                self._completion_token = (marker, row, start, end)
                self._completion_items = matches
                self._completion_selected = 0
                popup = self.query_one(CompletionPopup)
                popup.show_items(self._completion_items, 0)
                popup.display = True
                self._sync_completion_layout()
            else:
                popup = self.query_one(CompletionPopup)
                popup.set_items([])
                popup.display = False
                self._sync_completion_layout()
            return

        key = (marker, row, start, end, token)
        if (
            self._completion_token == (marker, row, start, end)
            and self._completion_query == token
            and self._completion_items
        ):
            return
        if (
            self._completion_token == (marker, row, start, end)
            and self._completion_query == token
            and self._completion_task is not None
            and not self._completion_task.done()
        ):
            return
        previous = self._completion_items if self._completion_token is not None else []
        self._completion_token = (marker, row, start, end)
        self._completion_query = token
        # Narrow the visible file list in place while the search runs, so the
        # popup does not blank and reload on every keystroke.
        needle = token.casefold()
        self._show_completion_items(
            keep=previous,
            items=[item for item in previous if marker == "@" and needle in item.casefold()],
        )
        self._completion_generation += 1
        generation = self._completion_generation
        if self._completion_task is not None:
            self._completion_task.cancel()
        if marker in {"model", "agent"}:
            self._completion_task = asyncio.create_task(
                self._load_argument_completions(marker, token, key, generation)
            )
        else:
            search = getattr(self.app.controller.client, "search_files", None)
            if not callable(search):
                self.close_completion()
                return
            self._completion_task = asyncio.create_task(
                self._load_file_completions(search, token[1:], key, generation)
            )

    async def _load_argument_completions(self, kind: str, query: str, key, generation: int) -> None:
        try:
            await asyncio.sleep(0.12)
            if kind == "agent":
                rows = getattr(self.app, "_agents", ()) or await self.app.controller.client.list_agents()
                values = [str(row.get("name", "")) for row in rows]
            else:
                rows = await self.app.controller.client.list_models(selectable_only=True)
                values = [f"{row.get('provider')}/{row.get('id')}" for row in rows
                          if row.get("provider") and row.get("id")]
        except asyncio.CancelledError:
            return
        except (ClientError, TransportError, OSError, ValueError):
            values = []
        if generation != self._completion_generation or self._active_token(self.query_one(ChatEditor)) != key:
            return
        self._completion_items = [value for value in values if value.casefold().startswith(query.casefold())][:100]
        self._completion_selected = 0
        popup = self.query_one(CompletionPopup)
        popup.show_items(self._completion_items, 0)
        popup.display = bool(self._completion_items)
        self._sync_completion_layout()

    async def _load_file_completions(self, search, query: str, key, generation: int) -> None:
        try:
            await asyncio.sleep(0.12)
            results = await search(query, limit=30)
        except asyncio.CancelledError:
            return
        except (ClientError, TransportError, OSError, ValueError):
            results = []
        if generation != self._completion_generation:
            return
        active = self._active_token(self.query_one(ChatEditor))
        if active is None or active != key:
            return
        items = [f"@{path}" for path in results[:30]]
        if items == self._completion_items:
            return
        self._show_completion_items(keep=self._completion_items, items=items)

    def _show_completion_items(self, *, keep: list[str], items: list[str]) -> None:
        """Render file suggestions, keeping the highlighted path when it survives."""
        current = keep[self._completion_selected] if 0 <= self._completion_selected < len(keep) else None
        self._completion_items = items
        self._completion_selected = (
            self._completion_items.index(current) if current in self._completion_items else 0
        )
        popup = self.query_one(CompletionPopup)
        popup.show_items(self._completion_items, self._completion_selected)
        popup.display = bool(self._completion_items)
        self._sync_completion_layout()

    async def on_unmount(self) -> None:
        if self._completion_task is not None:
            self._completion_task.cancel()
            self._completion_task = None

    def dismiss_completion(self) -> None:
        active = self._active_token(self.query_one(ChatEditor))
        if active is not None:
            self._dismissed_token = (
                active[0], active[1], active[2], active[3], active[4]
            )
        self.close_completion()

    def on_resize(self, event) -> None:
        # Keep the compact context entry visible even in a fresh session and on
        # narrow terminals. Its short label is an entry point, not a transcript
        # of the prompt.
        self.query_one("#context-usage", Static).display = True

    @on(TextArea.Changed, "#chat-editor")
    def _editor_changed(self, _: TextArea.Changed) -> None:
        editor = self.query_one(ChatEditor)
        self._sync_pasted_content_markers(editor.text)
        if self._history_index >= 0 and editor.text != self._history_value:
            self._history_index = -1
        self.app.call_after_refresh(self.refresh_completion)

    @on(ChatEditor.SubmitRequested)
    def _editor_submitted(self, message: ChatEditor.SubmitRequested) -> None:
        self._history_index = -1
        content = self._expand_pasted_content(message.content)
        if not content.lstrip().startswith("/"):
            append_history(content)
        self._pasted_content.clear()
        self._paste_markers.clear()
        self._refresh_pasted_content()
        self.post_message(InputSubmitted(content))
        self.query_one(ChatEditor).focus()

class PickerLink(Static):
    """Compact, independently focusable metadata action."""

    can_focus = True

    def __init__(self, label: str = "", *, picker_kind: str, **kwargs) -> None:
        kwargs["markup"] = False
        super().__init__(label, **kwargs)
        self.picker_kind = picker_kind

    def on_click(self, event) -> None:
        event.stop()
        self.app.run_worker(self.app._open_picker(self.picker_kind), group="inline-picker")

    def on_key(self, event) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            event.prevent_default()
            self.app.run_worker(self.app._open_picker(self.picker_kind), group="inline-picker")


class RootAgentBar(Horizontal):
    """Compact agent and model controls with distinct mouse/keyboard targets."""

    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._name = "General"
        self._identity_color = agent_color(self._name)
        self._model_text = "Default"
        self._model_summary = "Default"
        self.supported_levels = None

    def compose(self) -> ComposeResult:
        yield PickerLink(self._name, picker_kind="agent", id="root-agent-name")
        yield Static("  ", id="root-separator-model", markup=False)
        yield PickerLink("", picker_kind="model", id="root-model")
        yield Static(" ", id="root-separator-provider", markup=False)
        yield PickerLink("", picker_kind="model", id="root-provider")
        yield Static("  ", id="root-separator-effort", markup=False)
        yield PickerLink("", picker_kind="model", id="root-effort")

    def on_mount(self) -> None:
        self._sync_controls()

    def _sync_controls(self) -> None:
        if not self.is_mounted:
            return
        name = self.query_one("#root-agent-name", PickerLink)
        name.update(self._name)
        name.styles.color = self._identity_color
        model, provider, effort = self._model_segments
        self.query_one("#root-model", PickerLink).update(model)
        self.query_one("#root-provider", PickerLink).update(provider)
        effort_link = self.query_one("#root-effort", PickerLink)
        effort_link.update(effort)
        # An effort the model cannot take is noise in the composer line.
        effort = effort if effort not in {"Unsupported", "unknown"} else ""
        effort_link.display = bool(effort)
        for selector, value in (("#root-separator-model", model),
                                ("#root-separator-provider", provider),
                                ("#root-separator-effort", effort)):
            self.query_one(selector, Static).display = bool(value)

    def set_agent(
        self,
        name: str,
        source: str = "default",
        status: str = "idle",
        *,
        color: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        thinking_budget: int | None = None,
    ) -> None:
        identity_color = color or agent_color(name)
        self._name = sanitize(name, 64)
        self._identity_color = identity_color
        display_model = _model_display_name(model)
        display_provider = _provider_display_name(provider)
        model_parts = [part for part in (display_model, display_provider) if part]
        applied_effort = (
            reasoning_effort
            if reasoning_effort and reasoning_effort.casefold() != "not exposed"
            else None
        )
        if applied_effort:
            effort = applied_effort
        elif self.supported_levels is None:
            effort = "unknown"
        elif self.supported_levels == ():
            effort = "Unsupported"
        else:
            effort = "Default"
        if getattr(self, "stored_override", None) and not applied_effort:
            effort = f"Dormant {self.stored_override}"
        model_parts.append(effort)
        self._model_segments = (
            sanitize(display_model, 52),
            sanitize(display_provider, 40),
            sanitize(effort, 32),
        )
        self._model_text = "  ·  ".join(part for part in self._model_segments if part)
        self._model_summary = "  ·  ".join((self._name, *model_parts))
        # Controller metadata is copied before this call by the app.
        self._sync_controls()

    def set_effort_metadata(
        self,
        *,
        supported_levels: list[str] | tuple[str, ...] | None = None,
        stored_override: str | None = None,
        effort_source: str | None = None,
    ) -> None:
        self.supported_levels = None if supported_levels is None else tuple(supported_levels)
        self.stored_override = stored_override
        self.effort_source = effort_source

    def summary(self) -> Text:
        """Return the metadata summary for diagnostics and accessible clients."""
        rendered = Text()
        rendered.append(self._name, style=f"bold {self._identity_color}")
        if self._model_summary and self._model_summary.startswith(self._name):
            suffix = self._model_summary[len(self._name):]
            rendered.append(suffix, style="#aaa39c")
        return rendered

class ConnectionStatus(Static):
    """Connection/turn feedback plus compact session status."""

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.update(text)
        self.set_class(error, "error")
        self.display = bool(text)


class ActivityProgress(Static):
    """One-row context meter with a timer only while a turn is active."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=True, **kwargs)
        self._fraction = 0.0
        self._running = False
        self._loading = False
        self._color = "$nx-accent"
        self._tick = 0
        self._timer = None

    def set_state(self, *, used: int = 0, budget: int = 0, running: bool = False,
                  loading: bool = False, color: str = "$nx-accent") -> None:
        self._fraction = max(0.0, min(1.0, used / budget)) if budget > 0 else 0.0
        self._running, self._loading, self._color = running, loading, color
        if running or loading:
            if self._timer is None and self.is_mounted:
                self._timer = self.set_interval(0.05 if running else 0.2, self._advance)
        elif self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._draw()

    def on_mount(self) -> None:
        self.set_state(used=int(self._fraction * 1000), budget=1000,
                       running=self._running, loading=self._loading, color=self._color)

    def on_resize(self, _event) -> None:
        self._draw()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _advance(self) -> None:
        self._tick += 1
        self._draw()

    def _draw(self) -> None:
        width = max(1, self.size.width)
        if self._running:
            size = max(1, width // 5)
            travel = max(1, width - size)
            offset = self._tick % (travel * 2)
            start = min(offset, travel * 2 - offset)
            segments = [(start, "$nx-border"), (size, self._color),
                        (width - start - size, "$nx-border")]
        elif self._loading:
            segments = [(width, self._color if (self._tick // 3) % 2 else "$nx-border")]
        else:
            color = "$nx-success" if self._fraction < 0.5 else "$nx-warning" if self._fraction < 0.75 else "$nx-error"
            filled = round(width * self._fraction)
            segments = [(filled, color), (width - filled, "$nx-border")]
        self.update("".join(f"[{color}]{'━' * count}[/]" for count, color in segments if count))


class ContextUsage(Static):
    """Clickable, keyboard-focusable entry to the context preview."""

    can_focus = True

    def on_click(self, event) -> None:
        event.stop()
        self.app.run_worker(self.app.action_open_context(), group="context-inspect")

    def on_key(self, event) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            event.prevent_default()
            self.app.run_worker(self.app.action_open_context(), group="context-inspect")


class ContextPreview(Vertical):
    """Inline host-reported current request for the active session."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.display = False
        self._inspection = None
        self._loading = False
        self._error = None
        self._rendered_preview = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="context-preview-heading"):
            yield Static("CURRENT REQUEST CONTEXT", id="context-preview-label", markup=False)
            yield Static("Click for full context · Enter", id="context-preview-more", markup=False)
        with Horizontal(id="context-preview-controls"):
            yield PickerLink("", picker_kind="agent", id="context-preview-agent")
            yield PickerLink("", picker_kind="model", id="context-preview-model")
        yield Static("", id="context-preview-content", markup=False)

    def set_preview(
        self,
        inspection: p.ContextInspectResult | None,
        *,
        loading: bool = False,
        error: str | None = None,
    ) -> None:
        self._inspection = inspection
        self._loading = loading
        self._error = error
        if not self.is_mounted:
            return
        content = self.query_one("#context-preview-content", Static)
        width = content.region.width or max(1, self.region.width - 4)
        rendered = render_context_summary(
            inspection, loading=loading, error=error, width=width
        )
        agent = inspection.agent if inspection is not None and isinstance(inspection.agent, dict) else {}
        agent_name = str(agent.get("name") or "").strip()
        model_name = str(inspection.model or "").strip() if inspection is not None else ""
        provider = str(inspection.provider or "").strip() if inspection is not None else ""
        agent_link = self.query_one("#context-preview-agent", PickerLink)
        model_link = self.query_one("#context-preview-model", PickerLink)
        agent_link.can_focus = model_link.can_focus = False
        agent_link.update(f"Agent · {sanitize(agent_name, 48)}" if agent_name else "")
        model_link.update(
            f"Model · {sanitize(provider + '/' if provider else '', 32)}{sanitize(model_name, 56)}"
            if model_name or provider else ""
        )
        if (width, rendered) != self._rendered_preview:
            content.update(rendered)
            self._rendered_preview = (width, rendered)
        controls = self.query_one("#context-preview-controls", Horizontal)
        if inspection is not None and not loading and not error:
            agent_name = str(agent.get("name") or "").strip()
            model_name = str(inspection.model or "").strip()
            provider = str(inspection.provider or "").strip()
            agent_link.update(f"Agent · {sanitize(agent_name, 48)}" if agent_name else "")
            model_link.update(
                f"Model · {sanitize(provider + '/' if provider else '', 32)}{sanitize(model_name, 56)}"
                if model_name or provider else ""
            )
            agent_link.display = bool(agent_name)
            model_link.display = bool(model_name or provider)
            controls.display = agent_link.display or model_link.display
        else:
            controls.display = False
        more = self.query_one("#context-preview-more", Static)
        more.can_focus = False

    def on_resize(self, event) -> None:
        if self.is_mounted:
            self.set_preview(self._inspection, loading=self._loading, error=self._error)

    def on_click(self) -> None:
        self.app.run_worker(self.app.action_open_context(), group="context-inspect")

    def on_key(self, event) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            event.prevent_default()
            self.app.run_worker(self.app.action_open_context(), group="context-inspect")


#: Entry bodies above this render as plain text instead of Markdown.
MARKDOWN_LIMIT = 60_000


def _group_title(title: str, tokens: int, detail: str) -> Content:
    parts: list[str | tuple[str, str]] = [(title, "bold")]
    if tokens:
        parts.append((f"  ~{_compact_tokens(tokens)} tokens", "dim"))
    if detail:
        parts.append((f"  · {detail}", "dim"))
    return Content.assemble(*parts)


class ContextEntryWidget(Collapsible):
    """One collapsed row: title, estimate and summary. The body mounts on first expand."""

    def __init__(self, entry: ContextEntry) -> None:
        super().__init__(
            title=_group_title(entry.title, entry.tokens, entry.detail),
            classes="context-entry" + (" -error" if entry.error else ""),
        )
        self.entry = entry
        self._filled = False

    async def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if event.collapsible is not self or self._filled:
            return
        self._filled = True
        body = self.entry.body[:MAX_TEXT] or "(empty)"
        widget = (
            Markdown(body, classes="context-entry-body") if len(body) <= MARKDOWN_LIMIT
            else Static(Text(body), classes="context-entry-body")
        )
        await self.query_one(Collapsible.Contents).mount(widget)


def context_group_widgets(
    groups: list[ContextGroup], *, expanded: tuple[str, ...] = (), flatten_single: bool = False,
) -> list[Collapsible]:
    """A collapsible per group holding a collapsible per entry (bounded).

    ``flatten_single`` shows a one-entry group as just its entry, so a tool
    family of one does not repeat the same name and estimate.
    """
    widgets: list[Collapsible] = []
    for group in groups:
        if flatten_single and len(group.entries) == 1:
            widgets.append(ContextEntryWidget(group.entries[0]))
            continue
        rows: list[Static | Collapsible] = [ContextEntryWidget(entry) for entry in group.entries[:MAX_ROWS]]
        if not rows:
            rows.append(Static("(none)", classes="context-entry-empty", markup=False))
        widgets.append(Collapsible(
            *rows, title=_group_title(group.title, group.tokens, group.detail),
            collapsed=group.key not in expanded, classes="context-group",
        ))
    return widgets


class ContextDetailsScreen(ModalScreen[None]):
    """The next request grouped as it is sent: system prompt, tools, then each turn."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "close", "Close"), ("q", "close", "Close"),
        ("j", "scroll_down", "Scroll down"), ("k", "scroll_up", "Scroll up"),
        ("down", "scroll_down", "Scroll down"), ("up", "scroll_up", "Scroll up"),
        ("pagedown", "page_down", "Page down"), ("pageup", "page_up", "Page up"),
        ("home", "scroll_home", "Top"), ("end", "scroll_end", "Bottom"),
    ]
    MAX_TEXT = MAX_TEXT
    MAX_ROWS = MAX_ROWS

    def __init__(
        self,
        inspection: p.ContextInspectResult | None,
        usage: str,
        *,
        error: str | None = None,
        session: str = "",
    ) -> None:
        super().__init__()
        self.inspection = inspection
        self.usage = usage
        self.error = error
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="context-dialog"):
            yield Static(f"Context · {sanitize(self.session, 80)}" if self.session else "Context", id="context-title", markup=False)
            yield Static(context_summary(self.inspection, self.usage, error=self.error), id="context-details", markup=False)
            with VerticalScroll(id="context-scroll"):
                if self.inspection is not None and not self.error:
                    yield from context_group_widgets(context_groups(self.inspection))
            yield Static(
                "tab/shift+tab move · enter expands · j/k scroll · PgUp/PgDn · esc close",
                id="context-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#context-scroll", VerticalScroll).can_focus = True
        self.query_one("#context-scroll", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_scroll_down(self) -> None:
        self.query_one("#context-scroll", VerticalScroll).scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self.query_one("#context-scroll", VerticalScroll).scroll_up(animate=False)

    def action_page_down(self) -> None:
        self.query_one("#context-scroll", VerticalScroll).scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self.query_one("#context-scroll", VerticalScroll).scroll_page_up(animate=False)

    def action_scroll_home(self) -> None:
        self.query_one("#context-scroll", VerticalScroll).scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        self.query_one("#context-scroll", VerticalScroll).scroll_end(animate=False)


class LogsDrawer(Vertical):
    """Bounded, safe presentation of separately paged host diagnostics."""

    can_focus = False
    ROW_LIMIT = 60

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.display = False
        self.daemon_entries: list[object] = []
        self.session_entries: list[object] = []
        self.daemon_truncated = False
        self.session_truncated = False
        self.daemon_has_more = False
        self.session_has_more = False
        self.poll_error: str | None = None
        self.session_name = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="logs-titlebar"):
            yield Static("Logs", id="logs-title")
            yield Button("×", id="logs-close", variant="default")
        with VerticalScroll(id="logs-scroll"):
            yield Static("", id="logs-content", markup=False)

    def set_session(self, session: str) -> None:
        if session == self.session_name:
            return
        self.session_name = session
        self.session_entries.clear()
        self.session_truncated = False
        self.session_has_more = False
        if self.is_mounted:
            self.refresh_content()

    def reset_poll_state(self) -> None:
        """Clear stale page status while retaining useful rows across reopen."""
        self.daemon_truncated = False
        self.session_truncated = False
        self.daemon_has_more = False
        self.session_has_more = False
        self.poll_error = None
        if self.is_mounted:
            self.refresh_content()

    def add_page(self, result, *, include_session: bool = True) -> None:
        if result.daemon.truncated:
            self.daemon_entries.clear()
        self.daemon_entries = self._merge(self.daemon_entries, result.daemon.entries)
        self.daemon_truncated = self.daemon_truncated or result.daemon.truncated
        self.daemon_has_more = result.daemon.has_more
        self.poll_error = None
        if include_session:
            self.session_entries = self._merge(self.session_entries, result.session.entries)
            self.session_truncated = self.session_truncated or result.session.truncated
            self.session_has_more = result.session.has_more
        self.refresh_content()

    @classmethod
    def _merge(cls, current: list[object], incoming) -> list[object]:
        rows = list(current)
        known = {(getattr(row, "seq", None), getattr(row, "ts", None), getattr(row, "kind", None)) for row in rows}
        for row in incoming:
            identity = (getattr(row, "seq", None), getattr(row, "ts", None), getattr(row, "kind", None))
            if identity not in known:
                rows.append(row)
                known.add(identity)
        return rows[-cls.ROW_LIMIT :]

    def set_error(self, error: str) -> None:
        self.poll_error = sanitize(error, 160)
        self.refresh_content()

    @staticmethod
    def _entry_line(entry) -> str:
        try:
            stamp = datetime.fromtimestamp(float(entry.ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OverflowError, OSError):
            stamp = "unknown time"
        level = sanitize(str(getattr(entry, "level", "info")).upper(), 8)
        kind = sanitize(str(getattr(entry, "kind", "event")), 36)
        summary = sanitize(str(getattr(entry, "summary", "")), 140)
        return f"{stamp} [{level}] {kind} · {summary}"

    def refresh_content(self) -> None:
        if not self.is_mounted:
            return
        content = Text()
        if self.poll_error:
            content.append(f"Read error · {self.poll_error}\n\n", style="#d77b72")
        self._append_section(
            content, f"DAEMON · {len(self.daemon_entries)} shown", self.daemon_entries,
            self.daemon_truncated, self.daemon_has_more,
        )
        content.append("\n\n")
        self._append_section(
            content,
            f"SESSION ID · {sanitize(self.session_name or 'none', 80)} · {len(self.session_entries)} shown",
            self.session_entries, self.session_truncated, self.session_has_more,
        )
        self.query_one("#logs-content", Static).update(content)

    @classmethod
    def _append_section(cls, content: Text, heading: str, entries, truncated: bool, has_more: bool) -> None:
        content.append(heading, style="bold #d18a38")
        if truncated:
            content.append("\nEarlier entries unavailable · log may have restarted", style="#d18a38")
        if not entries:
            content.append("\nNo log entries", style="#777777")
        else:
            for row in entries:
                content.append("\n" + cls._entry_line(row), style="#b8b0a9")
        if has_more:
            content.append("\nMore entries available", style="#d18a38")


class WorktreeConfirmScreen(Screen[bool | None]):
    """Explicit confirmation for a fresh host-issued worktree preview."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "cancel", "Cancel")]

    def __init__(self, operation: str, preview, *, force: bool, files: list[str]) -> None:
        super().__init__()
        self.operation = operation
        self.preview = preview
        self.force = force
        self.files = files
        self._submitting = False

    def compose(self) -> ComposeResult:
        with Vertical(id="worktree-confirm-dialog"):
            title = f"Confirm {self.operation} · {self.preview.child_id}"
            yield Static(title, id="worktree-confirm-title", markup=False)
            yield Static(self._description(), id="worktree-confirm-description", markup=False)
            with Horizontal(id="worktree-confirm-actions"):
                yield Button("Cancel", id="worktree-confirm-cancel")
                yield Button("Confirm", id="worktree-confirm-accept", variant="warning")

    def _description(self) -> str:
        impact = getattr(self.preview, "impact", {}) or {}
        lines = [f"Operation: {self.operation}", f"Child: {_worktree_plain(self.preview.child_id)}"]
        if self.force:
            lines.append("WARNING: force discard removes the child worktree, including dirty files.")
        for key in ("parent_clean", "parent_head_matches_base", "child_dirty", "summary"):
            if key in impact:
                lines.append(f"{key}: {_worktree_plain(impact[key])}")
        lines.append("Changed files reported by the host review:")
        lines.extend(f"  {path}" for path in self.files)
        if not self.files and impact.get("child_dirty"):
            lines.append("  Dirty files are present; the host preview did not enumerate them.")
        elif not self.files:
            lines.append("  (none reported)")
        lines.append("\nThis confirmation authorizes the exact fresh preview shown above.")
        return "\n".join(lines)

    def action_cancel(self) -> None:
        if not self._submitting:
            self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "worktree-confirm-cancel":
            self.action_cancel()
        elif event.button.id == "worktree-confirm-accept" and not self._submitting:
            self._submitting = True
            event.button.disabled = True
            self.dismiss(True)


class WorktreesScreen(Screen[None]):
    """Host-backed worktree lifecycle, immutable review, and mutation UI."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "close", "Close")]
    REVIEW_PAGE_SIZE = 8

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.rows: list[dict] = []
        self.selected_id: str | None = None
        self.inspection = None
        self.review = None
        self.review_cursor = 0
        self.review_pages: dict[int, object] = {}
        self._review_id: str | None = None
        self._review_digest: str | None = None
        self._review_entries: list[dict] = []
        self._busy = False
        self._confirmation_pending = False
        self._last_mutation_result = None
        self._files: list[str] = []
        self._status = "Loading daemon-owned worktrees…"

    def compose(self) -> ComposeResult:
        with Vertical(id="worktrees-dialog"):
            yield Static("Worktrees", id="worktrees-title", markup=False)
            with Horizontal(id="worktrees-content"):
                with Vertical(id="worktrees-list-column"):
                    yield Static("Lifecycle", id="worktrees-list-heading", markup=False)
                    with VerticalScroll(id="worktrees-list-scroll"):
                        yield Vertical(id="worktrees-list-items")
                with Vertical(id="worktrees-detail-column"):
                    yield Static(self._status, id="worktrees-status", markup=False)
                    with VerticalScroll(id="worktrees-detail-scroll"):
                        yield Static("Select a child worktree to inspect its status and review.", id="worktrees-detail", markup=False)
                    with Horizontal(id="worktrees-actions"):
                        yield Button("Refresh", id="worktrees-refresh")
                        yield Button("Review", id="worktrees-review", disabled=True)
                        yield Button("Acknowledge digest", id="worktrees-ack", disabled=True)
                        yield Button("Integrate", id="worktrees-integrate", disabled=True)
                        yield Button("Discard", id="worktrees-discard", disabled=True)
                        yield Button("Force discard", id="worktrees-force-discard", disabled=True, variant="error")
                    with Horizontal(id="worktrees-pages"):
                        yield Button("Previous diff", id="worktrees-prev", disabled=True)
                        yield Button("Next diff", id="worktrees-next", disabled=True)

    async def on_mount(self) -> None:
        await self.refresh_worktrees()

    async def action_close(self) -> None:
        if not self._busy:
            self.dismiss(None)

    async def refresh_worktrees(self, *, inspect_selected: bool = True) -> None:
        if self._busy:
            return
        selected_id = self.selected_id
        self._set_busy(True)
        self._set_status("Loading daemon-owned worktrees…")
        try:
            result = await self.client.list_worktrees()
            self.rows = list(result.worktrees)
            await self._render_rows()
            if self.rows:
                self._set_status(
                    "Host worktree list has more entries." if result.has_more
                    else f"{len(self.rows)} daemon-owned worktree(s)."
                )
            else:
                self._set_status("No daemon-owned worktrees.")
            if not selected_id or not any(row.get("child_id") == selected_id for row in self.rows):
                self.selected_id = None
                self.inspection = self.review = None
                self._review_id = self._review_digest = None
                self._review_entries = []
                self._files = []
                self._set_detail("Select a child worktree to inspect its status and review.")
                self._sync_actions()
        except (ClientError, OSError, ValueError, TypeError) as exc:
            self._set_status(f"Worktree list failed · {_worktree_plain(exc)}", error=True)
        finally:
            self._set_busy(False)
        if self._last_mutation_result is not None and not inspect_selected:
            result = self._last_mutation_result
            self._last_mutation_result = None
            self._show_mutation_result(result)
        if inspect_selected and selected_id and any(row.get("child_id") == selected_id for row in self.rows):
            await self._select_worktree(selected_id)

    async def _render_rows(self) -> None:
        container = self.query_one("#worktrees-list-items", Vertical)
        await container.remove_children()
        for row in self.rows:
            child_id = str(row.get("child_id") or "")
            lifecycle = str(row.get("lifecycle") or row.get("status") or "unknown")
            label = f"{lifecycle} · {child_id}"
            await container.mount(
                Button(Text(_worktree_plain(label)), id=f"worktree-row-{len(container.children)}")
            )
            container.children[-1].child_id = child_id

    async def _select_worktree(self, child_id: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.selected_id = child_id
        self.review = None
        self.review_pages.clear()
        self._review_id = self._review_digest = None
        self._review_entries = []
        self.review_cursor = 0
        self._files = []
        self._set_status(f"Inspecting {_worktree_plain(child_id)}…")
        try:
            self.inspection = await self.client.inspect_worktree(child_id)
            self._set_detail(self._inspection_text(self.inspection))
            lifecycle = str(getattr(self.inspection, "status", "unknown"))
            if lifecycle == "finalized":
                await self._load_review_page(0)
            else:
                self._set_status(f"{_worktree_plain(child_id)} · {sanitize(lifecycle, 64)}")
        except (ClientError, OSError, ValueError, TypeError) as exc:
            self.inspection = None
            self._set_status(f"Worktree inspect failed · {_worktree_plain(exc)}", error=True)
            self._set_detail("The host could not inspect this worktree.")
        finally:
            self._set_busy(False)
            self._sync_actions()

    def _inspection_text(self, inspection) -> str:
        record = getattr(inspection, "record", {}) or {}
        lines = [f"Child: {_worktree_plain(inspection.child_id)}", f"Lifecycle: {sanitize(inspection.status, 64)}"]
        for key in (
            "dirty",
            "review_id",
            "digest",
            "acknowledged",
            "acknowledged_digest",
            "base_matches_parent",
            "discard_state",
            "integration_state",
        ):
            if key in record:
                lines.append(f"{key}: {_worktree_plain(record[key])}")
        if bool(record.get("dirty")):
            lines.append("WARNING: child worktree contains uncommitted changes.")
        return "\n".join(lines)

    async def _load_review_page(self, cursor: int) -> None:
        if self.selected_id is None:
            return
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            self._set_status("Review unavailable · invalid review cursor", error=True)
            return
        self._set_busy(True)
        self._set_status(f"Loading review page at file {cursor + 1}…")
        try:
            if self._review_id is None and cursor != 0:
                raise ValueError("review paging must start at the first page")
            pinned_id = self._review_id
            page = await self.client.review_worktree(
                self.selected_id,
                review_id=pinned_id,
                cursor=cursor,
                limit=self.REVIEW_PAGE_SIZE,
            )
            if (
                not isinstance(page.review_id, str)
                or not _WORKTREE_REVIEW_ID.fullmatch(page.review_id)
                or not isinstance(page.digest, str)
                or not _WORKTREE_DIGEST.fullmatch(page.digest)
            ):
                raise ValueError("host returned an invalid review identity or digest")
            if self._review_id is not None and (
                page.review_id != self._review_id or page.digest != self._review_digest
            ):
                raise ValueError("host review identity or digest changed; reload from the first page")
            if self._review_id is None:
                self._review_id = page.review_id
                self._review_digest = page.digest
                self._review_entries = [row for row in page.entries if isinstance(row, dict)]
                self.review = page
                self._files = [_worktree_plain(row.get("path", "")) for row in self._review_entries]
            if page.cursor != cursor:
                raise ValueError("host returned a non-matching review page cursor")
            if page.has_more and not page.diff:
                raise ValueError("host returned an empty review page with more pages")
            self.review_pages[cursor] = page
            self.review_cursor = cursor
            self._set_detail(self._review_text(page))
            self._set_status(
                f"Review {page.review_id} · digest {page.digest} · page {cursor // self.REVIEW_PAGE_SIZE + 1}"
            )
        except (ClientError, OSError, ValueError, TypeError) as exc:
            if "identity" in str(exc) or "digest" in str(exc):
                self.review = None
                self.review_pages.clear()
                self._review_id = self._review_digest = None
                self._review_entries = []
                self._files = []
                self._set_detail("Review identity changed or is invalid. Reload the review from the first page.")
            self._set_status(f"Review unavailable · {_worktree_plain(exc)}", error=True)
        finally:
            self._set_busy(False)
            self._sync_actions()

    def _review_text(self, page) -> str:
        lines = [
            f"Child: {_worktree_plain(page.child_id)} · lifecycle: {sanitize(page.status, 64)}",
            f"Review cursor: {page.cursor}",
            f"Review: {sanitize(page.review_id, 64)}",
            f"Digest: {sanitize(page.digest, 128)}",
            "",
            f"Changed files ({len(self._files)} reported):",
        ]
        lines.extend(f"  {path}" for path in self._files)
        lines.append("\nDiff page:")
        if not page.diff:
            lines.append("(no diff text on this page; binary or metadata-only changes may be listed above)")
        for row in page.diff:
            path = _worktree_plain(row.get("path", "(path unavailable)"))
            lines.append(f"\n--- {path} ---")
            if row.get("binary"):
                lines.append("[binary change; patch text unavailable]")
            patch = row.get("patch")
            if isinstance(patch, str):
                lines.extend(_worktree_patch_line(line) for line in patch.splitlines())
        if page.has_more:
            lines.append(f"\nMore diff pages available · next diff cursor {page.cursor + len(page.diff)}.")
        return "\n".join(lines)

    async def _acknowledge(self) -> None:
        if self._busy or self.selected_id is None or self.review is None:
            return
        self._set_busy(True)
        try:
            result = await self.client.acknowledge_worktree(
                self.selected_id, self._review_id, self._review_digest
            )
            self._set_status(
                f"Acknowledged exact digest {sanitize(result.digest, 128)} · review {sanitize(result.review_id, 64)}"
            )
            await self._refresh_inspection()
            self._sync_actions()
        except (ClientError, OSError, ValueError, TypeError) as exc:
            self._set_status(f"Acknowledge failed · {_worktree_plain(exc)}", error=True)
        finally:
            self._set_busy(False)
            self._sync_actions()

    async def _refresh_inspection(self) -> None:
        if self.selected_id:
            self.inspection = await self.client.inspect_worktree(self.selected_id)
            self._set_detail(self._inspection_text(self.inspection))

    async def _preview_mutation(self, operation: str, *, force: bool = False) -> None:
        if self._busy or self.selected_id is None:
            return
        if operation == "integrate" and (
            self.review is None or self._review_id is None or self._review_digest is None
        ):
            self._set_status("Integrate unavailable · review a finalized child first", error=True)
            return
        self._set_busy(True)
        self._set_status(f"Requesting fresh {operation} preview…")
        self._confirmation_pending = False
        try:
            if operation == "integrate":
                preview = await self.client.integrate_worktree(
                    self.selected_id, self._review_id, self._review_digest
                )
            else:
                review_id = self._review_id
                preview = await self.client.discard_worktree(
                    self.selected_id, force=force, review_id=review_id
                )
            if preview.status != "requires_confirmation":
                self._show_mutation_result(preview)
                return
            if (
                not isinstance(preview.confirmation_token, str)
                or not preview.confirmation_token
                or preview.child_id != self.selected_id
                or preview.operation != operation
            ):
                raise ValueError("host preview is missing a matching operation, child, or confirmation token")
            confirm = WorktreeConfirmScreen(
                operation, preview, force=force, files=self._files
            )
            self._confirmation_pending = True
            self.app.push_screen(confirm, callback=lambda accepted: self._confirm_result(
                accepted, operation, force, preview, self.selected_id,
                self._review_id, self._review_digest,
            ))
        except (ClientError, OSError, ValueError, TypeError) as exc:
            self._set_status(f"{operation.title()} preview failed · {_worktree_plain(exc)}", error=True)
        finally:
            if not self._confirmation_pending:
                self._set_busy(False)
            self._sync_actions()

    def _confirm_result(
        self, accepted, operation: str, force: bool, preview,
        child_id: str, review_id: str | None, digest: str | None,
    ) -> None:
        self._confirmation_pending = False
        if not accepted:
            self._set_busy(False)
            self._set_status("Confirmation cancelled · no mutation was submitted")
            self._sync_actions()
            return
        if (
            child_id != self.selected_id
            or review_id != self._review_id
            or digest != self._review_digest
        ):
            self._set_busy(False)
            self._set_status("Confirmation expired · selection or review changed; request a fresh preview", error=True)
            self._sync_actions()
            return
        self.app.run_worker(
            self._execute_mutation(operation, force, preview, child_id, review_id, digest),
            group="worktree-mutation",
            exclusive=True,
        )

    async def _execute_mutation(
        self, operation: str, force: bool, preview, child_id: str,
        review_id: str | None, digest: str | None,
    ) -> None:
        if not self._busy or self.selected_id != child_id:
            return
        self._set_status(f"Submitting confirmed {operation} to host…")
        try:
            if operation == "integrate":
                result = await self.client.integrate_worktree(
                    child_id,
                    review_id,
                    digest,
                    confirmation_token=preview.confirmation_token,
                )
            else:
                result = await self.client.discard_worktree(
                    child_id,
                    force=force,
                    review_id=review_id,
                    confirmation_token=preview.confirmation_token,
                )
            self._set_busy(False)
            self._last_mutation_result = result
            self.selected_id = None
            self.inspection = self.review = None
            self._review_id = self._review_digest = None
            self._review_entries = []
            self.review_pages.clear()
            self._files = []
            self._set_detail("Refreshing worktree lifecycle after the host mutation…")
            self._set_status("Host mutation returned · refreshing lifecycle list…")
            await self.refresh_worktrees(inspect_selected=False)
        except asyncio.CancelledError:
            self._set_status(
                f"{operation.title()} outcome unknown · cancellation does not undo a host mutation; refresh and inspect before retrying",
                error=True,
            )
            raise
        except (ClientError, OSError, ValueError, TypeError) as exc:
            outcome = (
                f"{operation.title()} outcome unknown · cancellation or a lost response does not undo a host mutation; "
                f"refresh and inspect before retrying · {_worktree_plain(exc)}"
            )
            self._set_busy(False)
            try:
                await self.refresh_worktrees(inspect_selected=False)
            except (ClientError, OSError, ValueError, TypeError):
                pass
            self._set_detail(
                "Host response was lost or rejected after confirmation. The UI cannot infer whether the operation completed.\n"
                "Use Refresh and inspect the child lifecycle/recovery state before any retry.\n\n"
                + outcome
            )
            self._set_status(outcome, error=True)
        finally:
            self._set_busy(False)
            self._sync_actions()

    def _show_mutation_result(self, result) -> None:
        status = str(result.status)
        lines = [
            f"Operation: {sanitize(result.operation or 'worktree mutation', 64)}",
            f"Status: {sanitize(status, 64)}",
            f"Child: {_worktree_plain(result.child_id)}",
        ]
        if result.transaction_id:
            lines.append(f"Transaction: {sanitize(result.transaction_id, 96)}")
        if result.digest:
            lines.append(f"Digest: {sanitize(result.digest, 128)}")
        if result.changed_paths:
            lines.append("Changed paths:")
            lines.extend(f"  {_worktree_plain(path)}" for path in result.changed_paths)
        if result.error:
            lines.append(f"Host detail: {_worktree_plain(result.error)}")
        if status == "rolled_back":
            lines.append("The host reports that it rolled back the integration transaction.")
        elif status in {"recovery_required", "cleanup_pending"}:
            lines.append("WARNING: host recovery or cleanup is required. Refresh and inspect host state.")
            for key, value in (getattr(result, "impact", {}) or {}).items():
                lines.append(f"{sanitize(key, 64)}: {_worktree_plain(value)}")
            if result.transaction_id:
                lines.append("Use the transaction id above when inspecting host recovery records.")
        elif status != "committed":
            lines.append("Mutation did not report a committed result. Inspect host state before retrying.")
        self._set_detail("\n".join(lines))
        self._set_status(f"{sanitize(status, 64)} · host mutation result", error=status in {"recovery_required", "cleanup_pending", "rolled_back"})

    def _set_status(self, text: str, *, error: bool = False) -> None:
        if self.is_mounted:
            widget = self.query_one("#worktrees-status", Static)
            widget.update(_worktree_plain(text))
            widget.set_class(error, "error")

    def _set_detail(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#worktrees-detail", Static).update(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if not self.is_mounted:
            return
        for button in self.query("#worktrees-actions Button, #worktrees-pages Button, #worktrees-refresh"):
            button.disabled = busy
        for button in self.query("#worktrees-list-items Button"):
            button.disabled = busy

    def _sync_actions(self) -> None:
        if not self.is_mounted:
            return
        finalized = self.inspection is not None and self.inspection.status == "finalized"
        record = getattr(self.inspection, "record", {}) if self.inspection is not None else {}
        acknowledged = bool(
            self.review is not None and self._review_id and self._review_digest
            and record.get("acknowledged")
            and record.get("digest") == self.review.digest
        )
        self.query_one("#worktrees-review", Button).disabled = not finalized or self._busy
        valid_review = bool(self.review is not None and self._review_id and self._review_digest)
        self.query_one("#worktrees-ack", Button).disabled = not finalized or not valid_review or acknowledged or self._busy
        self.query_one("#worktrees-integrate", Button).disabled = not finalized or not valid_review or not acknowledged or self._busy
        can_discard = self.inspection is not None and self.inspection.status not in {"integrated", "discarded", "active"}
        self.query_one("#worktrees-discard", Button).disabled = not can_discard or not acknowledged or self._busy
        self.query_one("#worktrees-force-discard", Button).disabled = not can_discard or self._busy
        previous = self.query_one("#worktrees-prev", Button)
        previous.disabled = self._busy or self.review_cursor <= 0
        page = self.review_pages.get(self.review_cursor)
        self.query_one("#worktrees-next", Button).disabled = self._busy or page is None or not page.has_more or not page.diff

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("worktree-row-"):
            index = int(button_id.rsplit("-", 1)[-1])
            if 0 <= index < len(self.rows):
                await self._select_worktree(str(self.rows[index].get("child_id", "")))
        elif button_id == "worktrees-refresh":
            await self.refresh_worktrees()
        elif button_id == "worktrees-review" and self.inspection is not None:
            await self._load_review_page(self.review_cursor)
        elif button_id == "worktrees-ack":
            await self._acknowledge()
        elif button_id == "worktrees-integrate":
            await self._preview_mutation("integrate")
        elif button_id == "worktrees-discard":
            await self._preview_mutation("discard")
        elif button_id == "worktrees-force-discard":
            await self._preview_mutation("discard", force=True)
        elif button_id == "worktrees-prev" and self.review_cursor > 0:
            await self._load_review_page(max(0, self.review_cursor - self.REVIEW_PAGE_SIZE))
        elif button_id == "worktrees-next":
            page = self.review_pages.get(self.review_cursor)
            if page is not None and page.has_more:
                await self._load_review_page(self.review_cursor + len(page.diff))


def _worktree_plain(value: object) -> str:
    """Render untrusted host text as redacted, control-free plain text."""
    if isinstance(value, (BaseException, dict, list, tuple)):
        value = str(value)
    return sanitize(value, 8192)


def _worktree_patch_line(value: str) -> str:
    return sanitize(redact(escape_controls(value)), 4096)


__all__ = [
    "ActivityProgress",
    "ChatEditor",
    "ChatInput",
    "ConnectionStatus",
    "ContextDetailsScreen",
    "ContextPreview",
    "ContextUsage",
    "LogsDrawer",
    "RootAgentBar",
    "Transcript",
    "TranscriptPane",
    "WorktreeConfirmScreen",
    "WorktreesScreen",
    "context_detail_usage",
    "context_usage",
]
