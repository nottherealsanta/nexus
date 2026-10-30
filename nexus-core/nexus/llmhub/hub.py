"""Joins sign-ins (`auth/`) to the provider table (`providers.py`).

The only module that knows both, and the entry point the rest of core uses.
`connected()` lists providers with stored credentials; `provider_for("grok/grok-4.7")`
returns the provider and the bare model id. There is no default model: the one a session
uses is the user's last selection (see `Runtime.remembered_model`).
"""

from __future__ import annotations

from . import Provider
from .auth import SIGN_INS
from .providers import PROVIDERS, make


def connected() -> list[str]:
    return [pid for pid in PROVIDERS if pid == "mock" or SIGN_INS[pid]().logged_in()]


def split_ref(ref: str) -> tuple[str, str]:
    """`provider/model` → (provider, model). A bare model id means codex."""
    provider, sep, model = ref.partition("/")
    if not sep:
        return "codex", ref
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; known: {', '.join(PROVIDERS)}")
    return provider, model


_SIGN_INS: dict[str, object] = {}


def sign_in(provider_id: str):
    """One sign-in object per provider per process, so access tokens are reused
    across turns instead of refreshing (and rotating) every time."""
    if provider_id not in _SIGN_INS:
        _SIGN_INS[provider_id] = SIGN_INS[provider_id]()
    return _SIGN_INS[provider_id]


def provider_for(ref: str) -> tuple[Provider, str]:
    provider_id, model = split_ref(ref)
    return make(provider_id, sign_in(provider_id) if provider_id != "mock" else None, model), model  # type: ignore[arg-type]
