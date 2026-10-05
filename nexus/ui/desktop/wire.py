"""Incremental desktop serialization; see desktop overhaul plan §3.3."""

from __future__ import annotations

import hashlib
import json
import base64
from typing import Any


# Snapshot fields are divided by their update topic. Unknown fields deliberately
# fall back to the header topic so additions are never silently omitted.
_TOPIC_FIELDS = {
    "header": {
        "title",
        "status",
        "agent",
        "model",
        "provider",
        "effort",
        "theme",
        "update_notice",
        "disconnected",
        "breadcrumb",
        "agent_page",
        "nav",
        "completion_bell",
        "completion_query",
        "completion_prefix",
        "completions",
        "voice_phase",
        "voice_preview",
        "voice_level",
        "attachments",
        "attachment_lines",
        "lines",
    },
    "composer": {
        "composer_key",
        "queue_lines",
        "insert",
        "auto_send_insert",
        "insert_kind",
        "restore",
    },
    "sessions": {
        "sessions",
        "archived_label",
        "sessions_truncated",
        "sessions_sidebar",
        "tabs",
        "panel_layout",
    },
    "details": {"details_panel", "details_sidebar"},
    "logs": {"logs"},
    "panel": {
        "panel_format",
        "preview_image",
        "preview_image_media",
        "panel_loading",
        "panel_title",
        "panel_hint",
        "panel_lines",
        "panel_tones",
        "items",
        "context_lines",
        "context_used",
        "context_window",
        "context_marks",
        "context_tiers",
        "context_usage",
        "context_note",
        "context_label",
        "context_preview",
    },
    "form": {"form"},
    "prompt": {"prompt"},
    "history": {"history"},
}
_EXCLUDED_FIELDS = {"schema", "revision", "generation", "blocks", "blocks_from"}
_IMAGE_LIMIT = 9  # eight inline images and one panel preview
_IMAGE_BYTES_LIMIT = 4 * 1024 * 1024
_IMAGE_TOTAL_BYTES_LIMIT = 4 * 1024 * 1024 * _IMAGE_LIMIT


def _stable_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return hashlib.blake2s(_stable_json(value)).hexdigest()


class DesktopWire:
    """Produce topic deltas and block updates for native desktop snapshots."""

    def __init__(self) -> None:
        self._fingerprints: dict[str, str] = {}
        self._blocks: list[dict[str, Any]] = []
        self._identity: tuple[Any, Any] | None = None
        self._initialized = False
        self._images: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _content_id(media: str, encoded: str) -> str:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return ""
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def encode(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Encode a projected snapshot, returning ``None`` if it is unchanged."""
        snapshot = dict(snapshot)
        images: dict[str, tuple[str, str]] = {}

        def image_ref(media: Any, encoded: Any) -> str:
            if not isinstance(media, str) or media not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                return ""
            if not isinstance(encoded, str) or not encoded:
                return ""
            image_id = self._content_id(media, encoded)
            if not image_id:
                return ""
            try:
                decoded_length = len(base64.b64decode(encoded, validate=True))
                if decoded_length > _IMAGE_BYTES_LIMIT:
                    return ""
            except (ValueError, TypeError):
                return ""
            if sum(len(base64.b64decode(data, validate=True)) for _, data in images.values()) + decoded_length > _IMAGE_TOTAL_BYTES_LIMIT:
                return ""
            images[image_id] = (media, encoded)
            return image_id

        preview = snapshot.get("preview_image")
        if isinstance(preview, str) and preview.startswith("data:"):
            header, separator, encoded = preview.partition(",")
            media = header.removeprefix("data:").removesuffix(";base64")
            preview = encoded if separator else ""
            if media and not snapshot.get("preview_image_media"):
                snapshot["preview_image_media"] = media
        preview_id = image_ref(snapshot.get("preview_image_media"), preview)
        if "preview_image" in snapshot:
            if preview_id:
                snapshot["preview_image"] = preview_id
            else:
                snapshot.pop("preview_image")
        inline = snapshot.get("inline_images")
        if isinstance(inline, list):
            normalized = []
            for item in inline:
                if not isinstance(item, dict):
                    normalized.append(item)
                    continue
                item = dict(item)
                image_id = image_ref(item.get("media"), item.pop("data", ""))
                item["data_ref"] = image_id
                normalized.append(item)
            snapshot["inline_images"] = normalized

        # Keep the current references only. A snapshot can carry at most nine
        # image references; missing/invalid references remain absent.
        if len(images) > _IMAGE_LIMIT:
            refs = list(images)
            retained_refs = set(refs[-_IMAGE_LIMIT:])
            images = {key: value for key, value in images.items() if key in retained_refs}
            if snapshot.get("preview_image") not in retained_refs:
                snapshot["preview_image"] = ""
            if isinstance(snapshot.get("inline_images"), list):
                for item in snapshot["inline_images"]:
                    if item.get("data_ref") not in retained_refs:
                        item["data_ref"] = ""
        while sum(len(base64.b64decode(value[1])) for value in images.values()) > _IMAGE_TOTAL_BYTES_LIMIT:
            oldest = next(iter(images))
            images.pop(oldest)
            if snapshot.get("preview_image") == oldest:
                snapshot["preview_image"] = ""
            for item in snapshot.get("inline_images", []):
                if isinstance(item, dict) and item.get("data_ref") == oldest:
                    item["data_ref"] = ""

        fields_by_topic: dict[str, dict[str, Any]] = {
            topic: {} for topic in _TOPIC_FIELDS
        }
        for key, value in snapshot.items():
            if key in _EXCLUDED_FIELDS:
                continue
            topic = next(
                (name for name, fields in _TOPIC_FIELDS.items() if key in fields),
                "header",
            )
            fields_by_topic[topic][key] = value

        identity = (snapshot.get("generation"), snapshot.get("agent_page", ""))
        reset = not self._initialized or self._identity != identity
        topics: dict[str, dict[str, Any]] = {}
        fingerprints: dict[str, str] = {}
        for topic, fields in fields_by_topic.items():
            fingerprint = _fingerprint(fields)
            fingerprints[topic] = fingerprint
            if reset or self._fingerprints.get(topic) != fingerprint:
                topics[topic] = fields

        blocks = snapshot.get("blocks", [])
        current_ids = [block.get("id") for block in blocks]
        prior_ids = [block.get("id") for block in self._blocks]
        block_update: dict[str, Any] | None = None
        if reset:
            block_update = {"splice": {"from": 0, "blocks": blocks}}
        elif blocks != self._blocks:
            append: list[dict[str, Any]] = []
            appendable = len(blocks) == len(self._blocks) and current_ids == prior_ids
            if appendable:
                for old, new in zip(self._blocks, blocks):
                    # All metadata other than text/rev must remain stable. An edit
                    # is appendable only when its new text extends the prior text.
                    if {k: v for k, v in old.items() if k not in ("text", "rev")} != {
                        k: v for k, v in new.items() if k not in ("text", "rev")
                    }:
                        appendable = False
                        break
                    old_text, new_text = (
                        str(old.get("text", "")),
                        str(new.get("text", "")),
                    )
                    if old_text == new_text and old.get("rev") == new.get("rev"):
                        continue
                    if not new_text.startswith(old_text) or len(new_text) == len(
                        old_text
                    ):
                        appendable = False
                        break
                    append.append(
                        {
                            "id": new.get("id"),
                            "text": new_text[len(old_text) :],
                            "rev": new.get("rev"),
                        }
                    )
            if appendable and append:
                block_update = {"append": append}
            else:
                prefix = 0
                while (
                    prefix < min(len(blocks), len(self._blocks))
                    and blocks[prefix] == self._blocks[prefix]
                ):
                    prefix += 1
                block_update = {"splice": {"from": prefix, "blocks": blocks[prefix:]}}

        # A one-shot insertion/restore is always resent once. Once observed, it
        # is absent from the next snapshot and the normal fingerprints clear it.
        changed_oneshot = any(snapshot.get(key) for key in ("insert", "restore"))
        if changed_oneshot:
            topics["composer"] = {
                key: value
                for key, value in snapshot.items()
                if key in _TOPIC_FIELDS["composer"] and key != "history"
            }
        image_put = [
            {"id": image_id, "media": media, "data": data}
            for image_id, (media, data) in images.items()
            if reset or image_id not in self._images
        ]
        image_drop = sorted(set(self._images) - set(images))
        if not reset and not topics and block_update is None and not changed_oneshot and not image_put and not image_drop:
            return None

        result: dict[str, Any] = {
            "schema": 3,
            "revision": snapshot.get("revision", 0),
            "generation": snapshot.get("generation", 0),
        }
        if reset:
            result["reset"] = True
        result["topics"] = topics
        if block_update is not None:
            result["blocks"] = block_update
        if image_put or image_drop or reset:
            result["images"] = {"put": image_put, "drop": image_drop}

        self._fingerprints = fingerprints
        self._blocks = [dict(block) for block in blocks]
        self._identity = identity
        self._initialized = True
        self._images = images
        return result


__all__ = ["DesktopWire"]
