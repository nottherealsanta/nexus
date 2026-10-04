"""Bounded desktop image presentation from host data (docs/desktop.md).

Never reads paths or fetches remote URLs. Full images remain daemon-owned;
inline previews share a four-MiB encoded budget and eight-image count cap.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque

MEDIA = {"image/png", "image/jpeg", "image/gif", "image/webp"}
INLINE_BUDGET = 4 * 1024 * 1024


def decode_image_url(url):
    """Accept only supported, bounded embedded images, never external URLs."""
    if not isinstance(url, str) or len(url) > 6 * 1024 * 1024:
        return None
    header, separator, encoded = url.partition(",")
    media = header.removeprefix("data:").removesuffix(";base64")
    if not separator or header != f"data:{media};base64" or media not in MEDIA:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    return (media, data) if 0 < len(data) <= 4 * 1024 * 1024 else None


async def refresh_draft_images(shell):
    """Fetch once per active attachment, with generation guards and time bounds."""
    items = [item for item in shell.attachments if item.kind == "image"][:8]
    cache = getattr(shell, "inline_image_cache", {})
    ids = {item.attachment_id for item in items}
    cache = {key: value for key, value in cache.items() if key in ids}
    shell.inline_image_cache = cache
    generation = shell.generation
    for item in items:
        if item.attachment_id in cache:
            continue
        try:
            preview = await asyncio.wait_for(shell.client.preview_attachment(item.attachment_id), 2)
            value = (preview.media_type, preview.data) if preview.media_type in MEDIA and 0 < len(preview.data) <= 4 * 1024 * 1024 else None
        except Exception:  # a thumbnail never blocks attaching or full preview
            value = None
        if generation != shell.generation:
            return
        cache[item.attachment_id] = value


def inline_images(view, shell):
    """Project recent sent images and draft previews within one encoded budget."""
    candidates = deque(maxlen=8)
    for turn in view.turns:
        for message in turn.messages:
            if message.role != "user":
                continue
            ordinal = 0
            for index, block in enumerate(message.blocks):
                if block.kind == "image":
                    ordinal += 1
                    candidates.append((f"{message.id}:{index}", message.id, f"Image {ordinal}",
                        {"kind": "submitted_image", "id": message.id, "index": index}, block.image_url))
    cache = getattr(shell, "inline_image_cache", {})
    for item in shell.attachments:
        if value := cache.get(item.attachment_id):
            candidates.append((item.attachment_id, "", item.name,
                {"kind": "attachment_preview", "id": item.attachment_id}, value))
    images = []
    remaining = INLINE_BUDGET
    for identity, message, label, operation, value in reversed(candidates):
        value = decode_image_url(value) if isinstance(value, str) else value
        if not value:
            continue
        media, data = value
        encoded = base64.b64encode(data).decode("ascii") if media in MEDIA else ""
        if not encoded or len(encoded) > remaining:
            continue
        remaining -= len(encoded)
        images.append({"id": identity, "message": message, "label": label,
                       "draft_index": next((i for i, item in enumerate(shell.attachments) if item.attachment_id == identity), None) if not message else None,
                       "operation": operation, "media": media, "data": encoded})
    from .text import escape_controls
    for image in images:
        image["label"] = escape_controls(image["label"])
    return list(reversed(images))
