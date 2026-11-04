"""The once-per-provider privacy notice (docs/manager-design.md section 9, "Privacy"), like old Nexus's `LiveConsent`.

The Manager can read any session, so session text reaches the provider that runs it, and whatever the Manager
speaks is generated from that text. The user acknowledges that once per provider. The acknowledgement is stored in
the preference table as ``{"version", "accepted_at"}`` per provider; changing `NOTICE_VERSION`, or switching
`[manager] model` to another provider, asks again. The offline `mock` provider needs none.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..llmhub.providers import PROVIDERS
from ..store import Store

NOTICE_VERSION = 1
EXEMPT = frozenset({"mock"})


def provider_of(ref: str) -> str:
    return ref.partition("/")[0] if "/" in ref else "codex"


def notice(provider: str) -> str:
    info = PROVIDERS.get(provider)
    label = info.label if info else provider
    text = (f"The Manager can read any of your sessions, so their text (titles, messages, answers, questions, and the "
            f"commands sessions ask to run) is sent to {label}, which runs it. What it says aloud is generated from "
            "that text. Secrets in session text are masked before they reach the model or the speaker, but the "
            "masking is a pattern match, not a guarantee.")
    if provider in ("codex", "openai"):
        text += " With a ChatGPT sign-in this goes to OpenAI."
    return text


class Consent:
    def __init__(self, store: Store, clock: Any = time.time) -> None:
        self.store = store
        self.clock = clock

    @staticmethod
    def _key(provider: str) -> str:
        return f"manager.consent.{provider}"

    def accepted_at(self, provider: str) -> float | None:
        raw = self.store.preference(self._key(provider))
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("version") != NOTICE_VERSION:
            return None  # an older notice counts as not given
        value = data.get("accepted_at")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def status(self, ref: str) -> dict[str, Any]:
        provider = provider_of(ref)
        given = self.accepted_at(provider)
        required = provider not in EXEMPT
        return {"provider": provider, "version": NOTICE_VERSION, "required": required and given is None,
                "accepted_at": given, "notice": notice(provider) if required else ""}

    def set(self, ref: str, accept: bool) -> dict[str, Any]:
        provider = provider_of(ref)
        self.store.set_preference(
            self._key(provider), json.dumps({"version": NOTICE_VERSION, "accepted_at": self.clock()}) if accept else None)
        return self.status(ref)
