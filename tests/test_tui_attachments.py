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
