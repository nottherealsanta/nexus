"""Pure rules for the ``/speak`` model download, shared by both terminal clients.

Mirrors the ``/voice download`` flow: check the host's status, show the size and
ask for consent, start the download, show progress, then speak. Nothing here
touches a widget or the host; it turns a ``SpeechStatusResult`` into the next
step and its wording so native speech flows use consistent wording.
"""
from __future__ import annotations

from typing import Any

__all__ = ["CONSENT_PROMPT", "CONSENT_TITLE", "next_step", "progress_text", "ready_text"]

CONSENT_TITLE = "Speech model · about 345 MB"
CONSENT_PROMPT = (
    "Download the local Kokoro speech model, the voice and the English phonemizer to enable "
    "/speak? It runs on this device; your answers are never sent to a speech service."
)


def _get(status: Any, key: str, default: Any = "") -> Any:
    return status.get(key, default) if isinstance(status, dict) else getattr(status, key, default)


def _megabytes(value: Any) -> str:
    return f"{max(0, int(value or 0)) / 1_000_000:.0f}"


def progress_text(status: Any) -> str:
    """``Downloading… 42% · 145 / 345 MB`` (the total is approximate)."""
    done, total = _get(status, "bytes_done", 0), _get(status, "bytes_total", 0)
    percent = f"{max(0.0, min(1.0, float(_get(status, 'progress', 0.0) or 0.0))):.0%}"
    sizes = f" · {_megabytes(done)} / {_megabytes(total)} MB" if total else ""
    return f"Downloading the local speech model… {percent}{sizes}"


def ready_text() -> str:
    return "The local speech model is available."


def next_step(status: Any, *, download_only: bool) -> str:
    """What the client does next.

    ``speak`` (model ready, speak now), ``ready`` (``/speak download`` with the
    model already present), ``consent`` (ask, then download), ``progress`` (a
    download is running), ``unsupported`` (packages missing: show how to install).
    """
    state = str(_get(status, "state", ""))
    if state == "ready":
        return "ready" if download_only else "speak"
    if state == "unsupported":
        return "unsupported"
    if state == "downloading":
        return "progress"
    return "consent"
