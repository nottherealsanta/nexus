"""Host-only voice configuration shared by terminal surfaces (VOICE_PLAN §8.2)."""
from __future__ import annotations
import json
import re
from typing import Any
from ..client.protocol import ClientError

async def set_voice_config(client: Any, **updates: object) -> None:
    """Persist voice fields as a valid v2 TOML config via the host settings path."""
    for attempt in range(2):
        current = await client.settings_read("global", "config", "config")
        body = current.body
        if not re.search(r"(?m)^\s*config_version\s*=", body):
            body = "config_version = 2\n" + body.lstrip("\n")
        for key, value in updates.items():
            body = _set_toml_voice_value(body, key, value)
        try:
            result = await client.settings_write("global", "config", "config", body, current.sha256)
            if result.status == "conflict":
                if attempt:
                    raise ClientError("Voice settings changed; retry the action")
                continue
            return
        except ClientError:
            if attempt:
                raise


def _set_toml_voice_value(body: str, key: str, value: object) -> str:
    if key not in {"enabled", "auto_send", "device", "max_seconds"}:
        raise ValueError("unsupported voice setting")
    rendered = str(value).lower() if isinstance(value, bool) else str(value)
    if isinstance(value, str):
        rendered = json.dumps(value, ensure_ascii=True)
    lines = body.splitlines()
    version = next((i for i, line in enumerate(lines) if re.match(r"^\s*config_version\s*=", line)), None)
    if version is None:
        lines.insert(0, "config_version = 2")
    else:
        lines[version] = "config_version = 2"
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[voice\]\s*(?:#.*)?", line)), None)
    if start is None:
        prefix = "\n".join(lines).rstrip()
        return prefix + ("\n\n" if prefix else "") + f"[voice]\n{key} = {rendered}\n"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$")
    existing = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
    if existing is None:
        lines.insert(end, f"{key} = {rendered}")
    else:
        lines[existing] = f"{key} = {rendered}"
    return "\n".join(lines) + "\n"

