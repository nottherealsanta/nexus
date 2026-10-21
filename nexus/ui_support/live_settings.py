"""Host-backed GPT Live configuration shared by terminal UIs (docs/live.md, ``[live]`` in docs/config.md).

The UI layer may not import ``config`` or ``live``, so the schema's bounds and the pinned voice list are
mirrored here; ``tests/test_live_settings.py`` pins both to the real ones. Any valid configured value is
shown and kept as it is, including numbers off the stepper grid; an invalid one falls back to the default.
"""
from __future__ import annotations

import json
import re
import tomllib
from typing import Any

from ..client.protocol import ClientError

LIVE_MODEL = "gpt-live-1-codex"
LIVE_DEFAULT_VOICE = "sol"
LIVE_VOICES: tuple[str, ...] = ("arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale")
#: ``(minimum, maximum)`` of each integer field, as ``LiveSection`` validates them.
LIVE_RANGES: dict[str, tuple[int, int]] = {"max_minutes": (1, 240), "carry_turns": (0, 100), "pause_seconds": (5, 3600)}
LIVE_MAX_INSTRUCTIONS = 4096
LIVE_DEFAULTS: dict[str, object] = {
    "enabled": True, "voice": "default", "max_minutes": 30, "carry_turns": 12, "pause_seconds": 120,
    "progress": True, "instructions": "", "log_raw": False,
}
_BOOLS = ("enabled", "progress", "log_raw")


def _valid(key: str, value: object) -> bool:
    if key in _BOOLS:
        return type(value) is bool
    if key in LIVE_RANGES:
        low, high = LIVE_RANGES[key]
        return type(value) is int and low <= value <= high
    if key == "voice":
        return isinstance(value, str) and (value in ("default", *LIVE_VOICES))
    if key == "instructions":
        return isinstance(value, str) and len(value) <= LIVE_MAX_INSTRUCTIONS and "\x00" not in value
    return False


def normalize_live_value(key: str, value: object) -> object:
    """``value`` as the stored type (a whole float from a stepper becomes an int); ``ValueError`` if invalid."""
    if key in LIVE_RANGES and type(value) is float and value.is_integer():
        value = int(value)
    if key not in LIVE_DEFAULTS or not _valid(key, value):
        raise ValueError(f"invalid live setting: {key}")
    return value


def _read_values(body: str) -> dict[str, object]:
    values = dict(LIVE_DEFAULTS)
    try:
        live = tomllib.loads(body).get("live", {})
    except (tomllib.TOMLDecodeError, TypeError):
        return values
    if isinstance(live, dict):
        for key in values:
            if key in live and _valid(key, live[key]):
                values[key] = live[key]
    return values


def _set_value(body: str, key: str, value: object) -> str:
    value = normalize_live_value(key, value)
    rendered = json.dumps(value, ensure_ascii=True) if isinstance(value, str) else str(value).lower() if isinstance(value, bool) else str(value)
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[live\]\s*(?:#.*)?", line)), None)
    if start is None:
        prefix = "\n".join(lines).rstrip()
        return prefix + ("\n\n" if prefix else "") + f"[live]\n{key} = {rendered}\n"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    setting = re.compile(rf"^\s*{key}\s*=.*$")
    index = next((i for i in range(start + 1, end) if setting.match(lines[i])), None)
    if index is None:
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, f"{key} = {rendered}")
    else:
        lines[index] = f"{key} = {rendered}"
    return "\n".join(lines) + "\n"


async def read_live_settings(client: Any) -> dict[str, object]:
    """Read config through host settings and return the valid ``[live]`` values (defaults otherwise)."""
    current = await client.settings_read("global", "config", "config")
    return _read_values(current.body)


async def set_live_config(client: Any, **updates: object) -> None:
    """Persist ``[live]`` fields with one optimistic retry; only the named keys are replaced."""
    if not updates:
        raise ValueError("invalid live setting")
    updates = {key: normalize_live_value(key, value) for key, value in updates.items()}
    for attempt in range(2):
        current = await client.settings_read("global", "config", "config")
        body = current.body
        for key, value in updates.items():
            body = _set_value(body, key, value)
        try:
            result = await client.settings_write("global", "config", "config", body, current.sha256)
        except ClientError:
            if attempt:
                raise
            continue
        if result.status == "conflict":
            if attempt:
                raise ClientError("GPT Live settings changed; retry the action")
            continue
        return


async def reset_live_config(client: Any) -> None:
    """Restore the GPT Live defaults without touching other configuration sections."""
    await set_live_config(client, **LIVE_DEFAULTS)


__all__ = [
    "LIVE_DEFAULTS", "LIVE_DEFAULT_VOICE", "LIVE_MAX_INSTRUCTIONS", "LIVE_MODEL", "LIVE_RANGES", "LIVE_VOICES",
    "normalize_live_value", "read_live_settings", "reset_live_config", "set_live_config",
]
