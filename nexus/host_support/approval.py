"""Bounded permission-request projection shared by attended host clients."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MAX_PERMISSION_TARGETS = 64
MAX_PERMISSION_TARGET_REQUEST_CHARS = 65_536
MAX_PERMISSION_TARGET_ROLE_CHARS = 64
MAX_PERMISSION_TARGET_PATH_CHARS = 4096
MAX_PERMISSION_TARGET_REASON_CHARS = 512


def approval_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate target details and mark unavailable lists for fail-closed review."""
    result = dict(data)
    result.pop("_display_targets", None)
    result.pop("_targets_unavailable", None)
    result.pop("_targets_persistence_unavailable", None)
    if "targets" not in data:
        return result

    targets = data.get("targets")
    valid = type(targets) is list and 0 < len(targets) <= MAX_PERMISSION_TARGETS
    rows: list[dict[str, str]] = []
    if valid:
        for target in targets:
            if type(target) is not dict or not all(
                type(target.get(field)) is str for field in ("role", "path", "reason")
            ):
                valid = False
                break
            if (
                not target["role"]
                or not target["path"]
                or len(target["role"]) > MAX_PERMISSION_TARGET_ROLE_CHARS
                or len(target["path"]) > MAX_PERMISSION_TARGET_PATH_CHARS
                or len(target["reason"]) > MAX_PERMISSION_TARGET_REASON_CHARS
            ):
                valid = False
                break
            try:
                for field in ("role", "path", "reason"):
                    target[field].encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                valid = False
                break
            rows.append({field: target[field] for field in ("role", "path", "reason")})

    if valid:
        preview = "\n".join(
            f"{row['role']}: {row['path']} — {row['reason']}" for row in rows
        )
        payload = json.dumps(
            {"targets": rows, "preview": preview},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        valid = len(payload) <= MAX_PERMISSION_TARGET_REQUEST_CHARS

    if not valid:
        result["persistence_available"] = False
        result["_targets_unavailable"] = True
    else:
        result["_display_targets"] = rows
        if not data.get("persistence_available", True):
            result["_targets_persistence_unavailable"] = True
    return result


__all__ = [
    "MAX_PERMISSION_TARGETS",
    "MAX_PERMISSION_TARGET_REQUEST_CHARS",
    "MAX_PERMISSION_TARGET_ROLE_CHARS",
    "MAX_PERMISSION_TARGET_PATH_CHARS",
    "MAX_PERMISSION_TARGET_REASON_CHARS",
    "approval_data",
]
