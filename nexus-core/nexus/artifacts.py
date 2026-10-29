"""Durable storage for media a session produced: an MCP image, a fetched picture (PLAN K8).

Bytes live once per content in `~/.nexus10/artifacts/<sha256>`; the `artifact` table says which session
made each one, so deleting the session removes its records and any file no record still names. Artifacts
are served by `GET /api/artifact/{id}`; they are never inlined into the stored context, which keeps
the timeline small and the wire events unclipped.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from pathlib import Path

from .paths import home

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MODEL_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")  # what providers take as input
_SAFE_MIME = re.compile(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*")


def directory() -> Path:
    return home() / "artifacts"


def blob_path(sha: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("not an artifact hash")
    return directory() / sha


def clean_mime(mime: str | None) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    return mime if _SAFE_MIME.fullmatch(mime) else "application/octet-stream"


def decode(data: str) -> bytes:
    """Base64 from a server, tolerant of whitespace and a data-URL prefix; ValueError if it is not base64."""
    if data.startswith("data:"):
        data = data.partition(",")[2]
    try:
        return base64.b64decode("".join(data.split()), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("not valid base64") from None


def write_blob(raw: bytes) -> str:
    """Store the bytes (once) and return their hash."""
    if not raw:
        raise ValueError("artifact is empty")
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    sha = hashlib.sha256(raw).hexdigest()
    path = blob_path(sha)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{sha}.{os.getpid()}.tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)
    return sha


def read_blob(sha: str) -> bytes:
    return blob_path(sha).read_bytes()


def remove_blob(sha: str) -> None:
    blob_path(sha).unlink(missing_ok=True)
