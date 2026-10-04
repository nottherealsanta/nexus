"""Desktop images are bounded, host-backed capabilities, not remote fetches."""
import base64
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from nexus.ui_support.native_images import INLINE_BUDGET, decode_image_url, inline_images, refresh_draft_images


def url(data=b"image"):
    return "data:image/png;base64," + base64.b64encode(data).decode()


def test_image_decoder_rejects_remote_malformed_and_unsupported_images():
    for value in ("https://example.org/image.png", "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,%%%", url(b"x" * (4 * 1024 * 1024 + 1))):
        assert decode_image_url(value) is None
    assert decode_image_url(url()) == ("image/png", b"image")


def test_inline_budget_and_recent_image_cap_preserve_only_projected_operations():
    messages = [NS(id=str(i), role="user", blocks=[NS(kind="image", image_url=url(b"x" * 600_000))]) for i in range(20)]
    shell = NS(attachments=[], inline_image_cache={})
    images = inline_images(NS(turns=[NS(messages=messages)]), shell)
    assert len(images) <= 8
    assert sum(len(image["data"]) for image in images) <= INLINE_BUDGET
    assert images[-1]["operation"] == {"kind": "submitted_image", "id": "19", "index": 0}
    assert all(int(image["message"]) >= 12 for image in images)


async def test_draft_images_are_cached_and_stale_fetches_are_discarded():
    shell = NS(attachments=[NS(kind="image", attachment_id="a", name="a.png")], generation=1,
               client=NS(preview_attachment=AsyncMock(return_value=NS(media_type="image/png", data=b"image"))))
    await refresh_draft_images(shell)
    await refresh_draft_images(shell)
    shell.client.preview_attachment.assert_awaited_once_with("a")
    assert inline_images(NS(turns=[]), shell)[0]["operation"] == {"kind": "attachment_preview", "id": "a"}
    shell.inline_image_cache.clear()
    async def stale(_):
        shell.generation += 1
        return NS(media_type="image/png", data=b"stale")
    shell.client.preview_attachment.side_effect = stale
    await refresh_draft_images(shell)
    assert shell.inline_image_cache == {}


async def test_submitted_message_opens_images_without_dumping_base64():
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.view.model import BlockView, MessageView

    message = MessageView(id="m", role="user", blocks=[BlockView(kind="text", text="Inspect this"), BlockView(kind="image", image_url=url())])
    shell = ShellActions(NS(client=NS(), view=NS(turns=[NS(messages=[message])], agents={}), session="s"))
    await shell.workflows.operate({"kind": "message_page", "id": "m"})
    assert shell.preview_image == b"image"
    assert shell.panel_format == "image"
    assert "base64" not in " ".join(shell.panel_lines)
    assert "Inspect this" in " ".join(shell.panel_lines)
    operation = shell.items[0]["operation"]
    assert operation == {"kind": "submitted_image", "id": "m", "index": 1}
    await shell.workflows.operate(operation)
    assert shell.panel_title == "Submitted image"
    assert shell.preview_image_media == "image/png"
