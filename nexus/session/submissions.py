"""Bounded request identity validation and canonical admission digests."""
from __future__ import annotations

import hashlib
import json
import re

import msgspec


def validate_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) is None:
        raise ValueError("client_request_id must be 1-64 ASCII letters, digits, _ or -")


def submission_digest(operation, content, mode, extra=None) -> str:
    value = msgspec.json.decode(msgspec.json.encode([operation, content, mode, extra]))
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
