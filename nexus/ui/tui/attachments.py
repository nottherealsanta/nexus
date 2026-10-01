"""TUI file attachment preparation and preview through the host (PLAN §14.4)."""

import asyncio

from ...ui_support.clipboard import read_clipboard_image

from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TextArea

from ...client import ClientError


class AttachmentPreview(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Back")]
    DEFAULT_CSS = """
    AttachmentPreview { align: center middle; }
    AttachmentPreview > VerticalScroll { width: 85%; height: 80%; background: $surface; padding: 1 2; }
    """

    def __init__(self, item):
        super().__init__()
        self.item = item

    def compose(self):
        with VerticalScroll():
            yield Static(f"Attachment: {self.item.name}", markup=False)
            yield Markdown(self.item.preview)


class AttachmentsMixin:
    def clear_attachments(self):
        self._attachments = []
        self._attachment_labels = {}
        self._attachment_counts = {}
        widget = self.query_one("#file-attachments", Static)
        widget.update("")
        widget.display = False

    async def paste_clipboard_image(self):
        """Upload local clipboard bytes through the host attachment contract."""
        session = self.controller.session
        try:
            data = await asyncio.to_thread(read_clipboard_image)
            if data is None:
                return False
            if session != self.controller.session:
                return True
            if len(getattr(self, "_attachments", [])) >= 8:
                raise ValueError("At most 8 attachments per message")
            item = await self.controller.client.prepare_attachment(name="clipboard.png", data=data)
            if session == self.controller.session:
                if len(getattr(self, "_attachments", [])) >= 8:
                    raise ValueError("At most 8 attachments per message")
                self._attachments = [*getattr(self, "_attachments", []), item]
                self._add_attachment_reference(item)
                self._render_attachments()
        except (ClientError, ValueError, OSError, TimeoutError) as exc:
            self._sync_status(str(exc), error=True)
        return True

    def _add_attachment_reference(self, item):
        kind = "image" if item.kind == "image" else "document"
        counts = getattr(self, "_attachment_counts", {})
        counts[kind] = counts.get(kind, 0) + 1
        self._attachment_counts = counts
        label = f"{kind} {counts[kind]}"
        labels = getattr(self, "_attachment_labels", {})
        labels[item.attachment_id] = label
        self._attachment_labels = labels
        editor = self.query_one("#chat-editor", TextArea)
        replaced = editor.replace(label, *editor.selection)
        editor.move_cursor(replaced.end_location)

    def _render_attachments(self):
        widget = self.query_one("#file-attachments", Static)
        widget.update("\n".join(
            f"{self._attachment_labels[value.attachment_id]}: {value.name} · {value.kind} · /attach clear to remove"
            for value in self._attachments
        ))
        widget.display = bool(self._attachments)

    async def attach_file(self, raw):
        path = raw.partition(" ")[2].strip().strip("\"'")
        if path == "clear":
            self.clear_attachments()
            return
        if not path:
            self._sync_status("Usage: /attach <path> | clear", error=True)
            return
        items = getattr(self, "_attachments", [])
        session = self.controller.session
        if len(items) >= 8:
            self._sync_status("At most 8 attachments per message", error=True)
            return
        item = await self.controller.client.prepare_attachment(path)
        if session != self.controller.session:
            return
        self._attachments = [*items, item]
        self._add_attachment_reference(item)
        self._render_attachments()
        if item.kind == "markdown":
            await self.push_screen(AttachmentPreview(item))

    async def send_attachments(self, message):
        items = getattr(self, "_attachments", [])
        if not items:
            return False
        session = self.controller.session
        try:
            await self.controller.client.enqueue(
                session,
                message.content,
                mode=message.mode,
                attachments=[item.attachment_id for item in items],
                attachment_labels=[self._attachment_labels[item.attachment_id] for item in items],
            )
            if session == self.controller.session:
                self.clear_attachments()
                if not self.controller.running:
                    self.controller.resume(self._post_event)
        except ClientError as exc:
            if session == self.controller.session:
                self.query_one("#chat-editor", TextArea).text = message.content
                self._sync_status(str(exc), error=True)
        return True
