"""Real conversion, multimodal provider delivery, replay, and failure boundaries."""

import base64
from types import SimpleNamespace

import msgspec
import pytest
from attachment_fixtures import PNG, docx_bytes, pdf_bytes

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection
from nexus.host import HostFacade, protocol as p
from nexus.host_support.attachments import AttachmentStore, MAX_ATTACHMENT_BYTES
from nexus.host_support.browser_view import web_view
from nexus.model.capabilities import Capabilities
from nexus.model.message import Image, Text
from nexus.model.providers.openai import _chat_content_parts, _responses_part
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.view import fold


@pytest.fixture
def store(tmp_path):
    config = Config(
        version=2,
        v2=ConfigV2(permissions=PermissionsSection(read_denyroots=["private"])),
    )
    runtime = SimpleNamespace(
        workspace=tmp_path, context=SimpleNamespace(effective_config=lambda: config)
    )
    return AttachmentStore(runtime)


@pytest.mark.parametrize(
    "name,payload,expected",
    [
        ("report.pdf", pdf_bytes(), "Attachment PDF works"),
        ("renamed.data", pdf_bytes(), "Attachment PDF works"),
        ("unicode.txt", "Unicode attachment".encode("utf-16"), "Unicode attachment"),
        ("report.docx", docx_bytes(), "Attachment Word works"),
        ("table.csv", b"name,value\nAda,42\n", "Ada"),
        ("report.rtf", rb"{\rtf1\ansi Attachment RTF works}", "Attachment RTF works"),
        ("code.py", b'print("hello")\n', 'print("hello")'),
        ("custom.extension", b"# custom text\n", "# custom text"),
    ],
)
async def test_real_document_conversion(store, name, payload, expected):
    item = await store.prepare(p.AttachmentPrepare(name=name, data=payload))
    assert item.kind == "markdown"
    assert expected in item.preview
    blocks = store.content("Read this", [], [item.attachment_id])
    assert blocks[0] == Text("Read this")
    assert expected in blocks[1].text
    assert name in blocks[1].text


async def test_image_bytes_and_provider_payloads(store):
    item = await store.prepare(p.AttachmentPrepare(name="photo.png", data=PNG))
    assert item.kind == "image"
    blocks = store.content("", [], [item.attachment_id])
    image = blocks[-1]
    assert isinstance(image, Image) and image.data == PNG
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert item.preview == url
    assert _chat_content_parts(blocks)[1][-1]["image_url"]["url"] == url
    assert _responses_part(image, text_type="input_text")["image_url"] == url


async def test_attachment_preview_is_bounded_daemon_owned_image_only(store):
    item = await store.prepare(p.AttachmentPrepare(name="photo.png", data=PNG))
    result = await store.preview(p.AttachmentPreview(item.attachment_id, max_bytes=len(PNG)))
    assert result.attachment_id == item.attachment_id
    assert result.media_type == "image/png" and result.data == PNG
    with pytest.raises(ValueError, match="byte limit"):
        await store.preview(p.AttachmentPreview(item.attachment_id, max_bytes=len(PNG) - 1))
    with pytest.raises(ValueError, match="between"):
        await store.preview(p.AttachmentPreview(item.attachment_id, max_bytes=0))
    document = await store.prepare(p.AttachmentPrepare(name="note.txt", data=b"hello"))
    with pytest.raises(ValueError, match="Only prepared images"):
        await store.preview(p.AttachmentPreview(document.attachment_id))


async def test_path_read_limits_denied_symlinks_and_expiry(store, tmp_path):
    (tmp_path / "note.txt").write_text("local note")
    item = await store.prepare(p.AttachmentPrepare(path="note.txt"))
    assert item.preview == "local note"
    store.drafts[item.attachment_id] = (0, store.drafts[item.attachment_id][1])
    with pytest.raises(ValueError, match="expired"):
        store.content("", [], [item.attachment_id])
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("secret")
    (tmp_path / "link.txt").symlink_to(private / "secret.txt")
    with pytest.raises(Exception, match="denied"):
        await store.prepare(p.AttachmentPrepare(path="link.txt"))
    with pytest.raises(ValueError, match="8 MiB"):
        await store.prepare(
            p.AttachmentPrepare(
                name="large.txt", data=b"x" * (MAX_ATTACHMENT_BYTES + 1)
            )
        )
    with pytest.raises(ValueError, match="8 attachments"):
        store.content("", [], ["x"] * 9)


@pytest.mark.parametrize(
    "name,data",
    [
        ("bad.pdf", b"%PDF-broken"),
        ("archive.bin", b"\x00\xffbinary"),
        ("empty.txt", b""),
    ],
)
async def test_failed_conversion_is_not_staged(store, name, data):
    with pytest.raises(ValueError):
        await store.prepare(p.AttachmentPrepare(name=name, data=data))
    assert not store.drafts


async def test_provider_receives_images_and_markdown_and_replay_retains_them(tmp_path):
    provider = ScriptedProvider(
        text_response("Received"),
        capabilities=Capabilities(
            vision=True,
            streaming=True,
            max_context_tokens=200000,
            max_output_tokens=8192,
        ),
    )
    config = Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(model=ModelSection(default="scripted/m")),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider})
    facade = HostFacade(runtime)
    try:
        facade.open_session("attachments")
        image = await facade.handle(
            p.AttachmentPrepare(name="image.png", data=PNG + b"x" * 10000)
        )
        pdf = await facade.handle(
            p.AttachmentPrepare(name="report.pdf", data=pdf_bytes())
        )
        large = await facade.handle(
            p.AttachmentPrepare(name="long.txt", data=b"long content " * 1000)
        )
        assert isinstance(image, p.AttachmentPrepareResult)
        result = await facade.handle(
            p.SessionEnqueue(
                session="attachments",
                content="Inspect these",
                attachments=[
                    image.attachment_id,
                    pdf.attachment_id,
                    large.attachment_id,
                ],
            )
        )
        assert isinstance(result, p.SessionEnqueueResult)
        assert not facade.attachments.drafts
        await runtime.session("attachments").wait_idle()
        assert provider.requests
        blocks = provider.requests[0].messages[-1].content
        assert any(
            isinstance(block, Image) and block.data == PNG + b"x" * 10000
            for block in blocks
        )
        assert any(
            isinstance(block, Text) and "Attachment PDF works" in block.text
            for block in blocks
        )
        live = facade.state("attachments")[0]
        replay = fold(runtime.session("attachments").events)
        assert live.to_dict() == replay.to_dict()
        for projection in (live.to_dict(), web_view(live)):
            user = projection["turns"][0]["messages"][0]
            image_block = next(b for b in user["blocks"] if b["kind"] == "image")
            assert image_block["image_url"] == image.preview
            assert any(
                b.get("text", "").endswith("long content " * 1000)
                for b in user["blocks"]
            )
        # Persisted user history retains original bytes after reopening.
        runtime.sessions.evict("attachments")
        assert any(
            isinstance(block, Image) and block.data == PNG + b"x" * 10000
            for block in runtime.session("attachments").messages[0].content
        )
    finally:
        await runtime.aclose()


async def test_protocol_attachment_roundtrip():
    command = p.AttachmentPrepare(name="image.png", data=PNG)
    assert p.decode_command(msgspec.json.encode(command)) == command
    assert isinstance(
        p.decode_command(
            msgspec.json.encode(p.SessionEnqueue("s", attachments=["id"]))
        ),
        p.SessionEnqueue,
    )
    preview = p.AttachmentPreview("attachment", 1024)
    assert p.decode_command(msgspec.json.encode(preview)) == preview


def test_large_text_redaction_does_not_scan_every_possible_scheme_start():
    from nexus.util import redact_url_userinfo

    text = "x" * 1_000_000 + " https://user:password@example.com/path"
    assert (
        redact_url_userinfo(text)
        == "x" * 1_000_000 + " https://<redacted>@example.com/path"
    )


async def test_numbered_references_pair_with_payload_and_validate(store):
    image = await store.prepare(p.AttachmentPrepare(name="a.png", data=PNG))
    document = await store.prepare(p.AttachmentPrepare(name="b.txt", data=b"Full document"))
    ids = [image.attachment_id, document.attachment_id]
    blocks = store.content("Use image 2 with document 1", [], ids, ["image 2", "document 1"])
    assert blocks[0].text == "Use image 2 with document 1"
    assert "image 2" in blocks[1].text
    assert blocks[2].data == PNG
    assert "document 1" in blocks[3].text and "Full document" in blocks[3].text
    for labels in (["image 1"], ["document 1", "document 1"], ["image 0", "document 1"]):
        with pytest.raises(ValueError):
            store.content("", [], ids, labels)
