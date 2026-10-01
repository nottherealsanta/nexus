"""TUI attachment command, preview, submit modes, and failure recovery."""

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.host import protocol as p
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.attachments import AttachmentPreview
from nexus.ui.tui.widgets import ChatEditor


class AttachmentTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.sent = None
        self.fail_attachment = False

    async def request(self, command):
        if isinstance(command, p.AttachmentPrepare):
            if self.fail_attachment:
                return p.ErrorResult(
                    kind="ValueError", message="document format is unsupported"
                )
            return p.AttachmentPrepareResult(
                "prepared",
                command.path,
                "markdown",
                "# Complete document\n\nEnd of document",
            )
        if isinstance(command, p.SessionEnqueue):
            self.sent = command
            self.started.set()
        return await super().request(command)


@pytest.mark.parametrize(
    "key,mode",
    [("enter", "queue"), ("ctrl+enter", "steer"), ("alt+enter", "interrupt")],
)
async def test_attach_preview_and_submit_even_without_prompt(key, mode):
    transport = AttachmentTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = '/attach "report with spaces.pdf"'
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AttachmentPreview)
        assert app.screen.item.name == "report with spaces.pdf"
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#file-attachments").display
        editor.focus()
        await pilot.press(key)
        await pilot.pause()
        assert transport.sent.attachments == ["prepared"]
        assert transport.sent.mode == mode
        assert transport.sent.content == ""
        assert not app.query_one("#file-attachments").display


async def test_clear_and_conversion_failure_leave_composer_usable():
    transport = AttachmentTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._dispatch_chat_command("/attach report.pdf")
        await pilot.press("escape")
        await app._dispatch_chat_command("/attach clear")
        assert not app.query_one("#file-attachments").display
        transport.fail_attachment = True
        await app._dispatch_chat_command("/attach unknown.bin")
        assert not app._attachments
        assert not app.query_one(ChatEditor).disabled


async def test_ctrl_v_uploads_clipboard_image(monkeypatch):
    from nexus.ui.tui import attachments

    image = b"\x89PNG\r\n\x1a\nclipboard image"
    monkeypatch.setattr(attachments, "read_clipboard_image", lambda: image)
    transport = AttachmentTransport()
    original = transport.request
    prepared = []

    async def request(command):
        if isinstance(command, p.AttachmentPrepare):
            prepared.append(command)
            return p.AttachmentPrepareResult("image-id", command.name, "image", "image preview")
        return await original(command)

    transport.request = request
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.focus()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert prepared[0].data == image
        assert prepared[0].path == ""
        assert app._attachments[0].name == "clipboard.png"
        assert app.query_one("#file-attachments").display
        await pilot.press("enter")
        await pilot.pause()
        assert transport.sent.attachments == ["image-id"]


async def test_ctrl_v_text_fallback_and_clipboard_error(monkeypatch):
    from nexus.ui.tui import attachments

    monkeypatch.setattr(attachments, "read_clipboard_image", lambda: None)
    app = NexusTextualApp(_client(AttachmentTransport()), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        app.copy_to_clipboard("ordinary text")
        editor.focus()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert editor.text == "ordinary text"

        def fail():
            raise ValueError("Clipboard image exceeds 8 MiB")

        monkeypatch.setattr(attachments, "read_clipboard_image", fail)
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert editor.text == "ordinary text"
        assert not getattr(app, "_attachments", [])
        assert not editor.disabled


def test_native_clipboard_bounds_and_ssh(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from nexus.ui_support import clipboard

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")

    def read(command, **kwargs):
        assert kwargs["timeout"] == 5
        Path(command[-1]).write_bytes(b"PNG data")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clipboard.subprocess, "run", read)
    assert clipboard.read_clipboard_image() == b"PNG data"
    monkeypatch.setattr(clipboard, "MAX_IMAGE_BYTES", 2)
    with pytest.raises(ValueError, match="exceeds 8 MiB"):
        clipboard.read_clipboard_image()
    monkeypatch.setenv("SSH_CONNECTION", "remote connection")
    assert clipboard.read_clipboard_image() is None


def test_native_clipboard_timeout(monkeypatch):
    from nexus.ui_support import clipboard

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")

    def timeout(*args, **kwargs):
        raise clipboard.subprocess.TimeoutExpired("osascript", 5)

    monkeypatch.setattr(clipboard.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="timed out"):
        clipboard.read_clipboard_image()
