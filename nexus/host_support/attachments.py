"""Bounded user attachments behind the host contract (PLAN §14.4).

Images remain multimodal input; documents use the isolated AnyDoc worker.
Prepared drafts expire, while submitted blocks belong to the durable log.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import stat
import time
from pathlib import Path

import msgspec

from ..host import protocol as p
from ..model.message import ContentBlock, Image, Text
from ..tools.builtin._anydoc_client import AnyDocError, convert_document
from ..tools.builtin.read import _DOCUMENT_EXTENSIONS
from ..tools.permissions import PathGuard
from .workspace import _file_search_denied_roots

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENTS = 8
TTL_SECONDS = 3600


def image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class AttachmentStore:
    def __init__(self, runtime):
        self.runtime = runtime
        self.drafts: dict[str, tuple[float, list[ContentBlock]]] = {}
        self.lock = asyncio.Lock()

    def expire(self):
        now = time.monotonic()
        self.drafts = {
            key: value for key, value in self.drafts.items() if value[0] > now
        }

    async def prepare(self, command: p.AttachmentPrepare) -> p.AttachmentPrepareResult:
        async with self.lock:
            self.expire()
            if len(self.drafts) >= 16:
                raise ValueError(
                    "Too many prepared attachments; try again after drafts expire"
                )
            if command.path and command.data:
                raise ValueError("Choose a path or uploaded bytes")
            name, data = command.name, command.data
            if command.path:
                guard = PathGuard(
                    self.runtime.workspace,
                    read_denyroots=tuple(
                        str(root) for root in _file_search_denied_roots(self.runtime)
                    ),
                )
                resolved = guard.resolve(command.path, for_write=False)
                name = resolved.absolute.name

                def read():
                    # A user-selected pipe/device must never block the daemon.
                    fd = os.open(
                        resolved.absolute,
                        os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                    )
                    with os.fdopen(fd, "rb") as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise ValueError("Attachments must be regular files")
                        guard.recheck(resolved.absolute, for_write=False)
                        return source.read(MAX_ATTACHMENT_BYTES + 1)

                data = await asyncio.to_thread(read)
            name = Path(name).name
            if not name or len(name) > 255 or any(ord(c) < 32 for c in name):
                raise ValueError("Attachment needs a valid filename")
            if not data or len(data) > MAX_ATTACHMENT_BYTES:
                raise ValueError("Attachment must contain between 1 byte and 8 MiB")
            media = image_type(data)
            if media:
                kind = "image"
                preview = f"data:{media};base64,{base64.b64encode(data).decode()}"
                blocks = [
                    Text(f"\n\nAttachment: {name} · {media} · {len(data)} bytes\n"),
                    Image(media, data=data),
                ]
            else:
                suffix = Path(name).suffix.lower()
                if data.startswith(b"%PDF-"):
                    suffix = ".pdf"
                try:
                    if (
                        suffix in _DOCUMENT_EXTENSIONS
                        or suffix == ".csv"
                        or data.startswith(
                            (b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0")
                        )
                    ):
                        markdown = (await convert_document(data, suffix, None)).decode(
                            "utf-8"
                        )
                    else:
                        encoding = "utf-8-sig"
                        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
                            encoding = "utf-32"
                        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
                            encoding = "utf-16"
                        markdown = data.decode(encoding)
                        if "\x00" in markdown or any(
                            ord(c) < 32 and c not in "\n\r\t" for c in markdown
                        ):
                            markdown = (
                                await convert_document(data, suffix, None)
                            ).decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        markdown = (await convert_document(data, suffix, None)).decode(
                            "utf-8"
                        )
                    except AnyDocError as exc:
                        raise ValueError(str(exc)) from exc
                except AnyDocError as exc:
                    raise ValueError(str(exc)) from exc
                if not markdown.strip():
                    raise ValueError("No readable text found in attachment")
                kind, preview = "markdown", markdown
                blocks = [Text(f"\n\nAttachment: {name}\n\n{markdown}")]
            token = secrets.token_hex(24)
            self.drafts[token] = (time.monotonic() + TTL_SECONDS, blocks)
            return p.AttachmentPrepareResult(token, name, kind, preview)

    def release(self, tokens: list[str]) -> None:
        """Free prepared bytes once their content has been durably submitted."""
        for token in tokens:
            self.drafts.pop(token, None)

    def content(self, text: str, blocks: list[dict], attachments: list[str]):
        self.expire()
        if len(attachments) > MAX_ATTACHMENTS:
            raise ValueError("At most 8 attachments per message")
        if not attachments:
            return msgspec.convert(blocks, type=list[ContentBlock]) if blocks else text
        content = [Text(text)] if text else []
        content.extend(msgspec.convert(blocks, type=list[ContentBlock]))
        for token in attachments:
            if token not in self.drafts:
                raise ValueError("Attachment expired; attach the file again")
            content.extend(self.drafts[token][1])
        if len(msgspec.json.encode(content)) > 12 * 1024 * 1024:
            raise ValueError("Combined attachments exceed the message size limit")
        return content
