"""Validate inline image attachments before accepting a turn."""
from __future__ import annotations

import base64
import binascii
from typing import Any

from .llmhub.types import Image

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 8
PREFIXES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff", "image/gif": b"GIF"}


def image_parts(images: Any) -> tuple[Image, ...]:
    if images is None:
        return ()
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError("images must be a list of at most eight images")
    parts = []
    total = 0
    for item in images:
        if not isinstance(item, dict):
            raise ValueError("each image must have a name and data_url")  # noqa: TRY004
        url, name = item.get("data_url"), item.get("name", "image.png")
        if not isinstance(url, str) or len(url) > MAX_IMAGE_BYTES * 4 // 3 + 128:
            raise ValueError("image exceeds 8 MiB or has no data_url")
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise ValueError("image name must be 1–255 characters")
        header, sep, encoded = url.partition(",")
        mime = header.removeprefix("data:").removesuffix(";base64")
        if not sep or header != f"data:{mime};base64" or mime not in (*PREFIXES, "image/webp"):
            raise ValueError("images must be base64 PNG, JPEG, GIF or WebP data URLs")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid image base64") from None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("image is empty or exceeds 8 MiB")
        valid = (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP") if mime == "image/webp" else raw.startswith(PREFIXES[mime])
        if not valid:
            raise ValueError("image bytes do not match its media type")
        total += len(raw)
        if total > MAX_IMAGE_BYTES:
            raise ValueError("images exceed 8 MiB in total")
        parts.append(Image(url, name))
    return tuple(parts)
