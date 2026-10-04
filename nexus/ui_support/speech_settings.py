"""Host-backed Kokoro speech configuration shared by terminal UIs."""
from __future__ import annotations

import json
import re
from typing import Any
import tomllib

from ..client.protocol import ClientError

SPEECH_DEFAULTS: dict[str, object] = {
    "voice": "af_heart",
    "language": "a",
    "speed": 1.0,
    "device": "cpu",
}
SPEECH_CHOICES: dict[str, tuple[object, ...]] = {
    "voice": ("af_heart", "af_bella", "af_nicole", "am_adam", "am_michael", "bf_emma", "bm_george"),
    "language": ("a", "b"),
    "speed": (0.5, 0.75, 1.0, 1.25, 1.5, 2.0),
    "device": ("cpu", "mps"),
}


def _valid(key: str, value: object) -> bool:
    if key not in SPEECH_CHOICES or type(value) is not type(SPEECH_CHOICES[key][0]) or value not in SPEECH_CHOICES[key]:
        return False
    if key == "voice":
        prefix = str(value)[:1]
        return (prefix == "a" and value in {"af_heart", "af_bella", "af_nicole", "am_adam", "am_michael"}) or (
            prefix == "b" and value in {"bf_emma", "bm_george"}
        )
    return True


def _read_values(body: str) -> dict[str, object]:
    values = dict(SPEECH_DEFAULTS)
    try:
        parsed = tomllib.loads(body)
        speech = parsed.get("speech", {})
        if isinstance(speech, dict):
            language = speech.get("language", values["language"])
            if language in SPEECH_CHOICES["language"]:
                values["language"] = language
            for key in ("voice", "speed", "device"):
                value = speech.get(key, values[key])
                if _valid(key, value):
                    values[key] = value
            return values
    except (tomllib.TOMLDecodeError, TypeError):
        pass
    section = re.search(r"(?ms)^\s*\[speech\]\s*(?:#.*)?$([\s\S]*?)(?=^\s*\[|\Z)", body)
    if section is None:
        return values
    for key in SPEECH_CHOICES:
        match = re.search(rf"(?m)^\s*{key}\s*=\s*(.*?)\s*(?:#.*)?$", section.group(1))
        if match is None:
            continue
        raw = match.group(1)
        try:
            parsed = json.loads(raw) if key in {"voice", "language", "device"} else float(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        if _valid(key, parsed):
            values[key] = parsed
    return values


def _set_value(body: str, key: str, value: object) -> str:
    if key not in SPEECH_CHOICES or not _valid(key, value):
        raise ValueError(f"unsupported speech setting: {key}")
    if key == "voice" and not str(value).startswith(str(_read_values(body)["language"])):
        raise ValueError("speech voice must match the selected language")
    rendered = json.dumps(value, ensure_ascii=True) if isinstance(value, str) else str(value)
    lines = body.splitlines()
    match = next((i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[speech\]\s*(?:#.*)?", line)), None)
    if match is None:
        prefix = "\n".join(lines).rstrip()
        return prefix + ("\n\n" if prefix else "") + f"[speech]\n{key} = {rendered}\n"
    end = next((i for i in range(match + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    setting = re.compile(rf"^\s*{key}\s*=.*$")
    index = next((i for i in range(match + 1, end) if setting.match(lines[i])), None)
    if index is None:
        while end > match + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, f"{key} = {rendered}")
    else:
        lines[index] = f"{key} = {rendered}"
    return "\n".join(lines) + "\n"


async def read_speech_settings(client: Any) -> dict[str, object]:
    """Read config through host settings and return supported speech values."""
    current = await client.settings_read("global", "config", "config")
    return _read_values(current.body)


async def set_speech_config(client: Any, **updates: object) -> None:
    """Persist speech fields with optimistic retries; only replace keys in [speech]."""
    if not updates or any(not _valid(key, value) for key, value in updates.items()):
        raise ValueError("invalid speech setting")
    if "language" in updates and "voice" not in updates:
        language = str(updates["language"])
        if language not in {"a", "b"}:
            raise ValueError("unsupported speech language")
        updates = {**updates, "voice": "af_heart" if language == "a" else "bf_emma"}
    for attempt in range(2):
        current = await client.settings_read("global", "config", "config")
        current_values = _read_values(current.body)
        effective_language = str(updates.get("language", current_values["language"]))
        effective_voice = str(updates.get("voice", current_values["voice"]))
        if not effective_voice.startswith(effective_language):
            raise ValueError("speech voice must match the selected language")
        body = current.body
        ordered = dict(updates)
        if "language" in ordered and "voice" in ordered:
            ordered = {"language": ordered["language"], **{k: v for k, v in ordered.items() if k != "language"}}
        if "language" in updates:
            body = _set_value(body, "language", updates["language"])
        for key, value in ordered.items():
            if key == "language":
                continue
            body = _set_value(body, key, value)
        try:
            result = await client.settings_write("global", "config", "config", body, current.sha256)
        except ClientError:
            if attempt:
                raise
            continue
        if result.status == "conflict":
            if attempt:
                raise ClientError("Speech settings changed; retry the action")
            continue
        return


async def reset_speech_config(client: Any) -> None:
    """Restore speech defaults without resetting other configuration sections."""
    await set_speech_config(client, **SPEECH_DEFAULTS)


def compatible_voices(language: str) -> tuple[str, ...]:
    """Return installed-choice names whose Kokoro prefix matches language."""
    if language == "a":
        return tuple(str(voice) for voice in SPEECH_CHOICES["voice"] if str(voice).startswith("a"))
    if language == "b":
        return tuple(str(voice) for voice in SPEECH_CHOICES["voice"] if str(voice).startswith("b"))
    return ()


def _set_speech_toml_value(body: str, key: str, value: object) -> str:
    """Compatibility seam for focused tests of safe [speech] updates."""
    return _set_value(body, key, value)


__all__ = [
    "SPEECH_CHOICES", "SPEECH_DEFAULTS", "compatible_voices", "read_speech_settings",
    "reset_speech_config", "set_speech_config",
]
