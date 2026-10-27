"""Provider authentication. Each provider's sign-in lives here and hands the
LLM hub a header source (`async headers() -> dict`); adapters never see stored
credentials.

Every sign-in has the same shape: `methods`, `async login(method, notify)`,
`logged_in()`, `logout()` and `async headers()`.
"""
from __future__ import annotations

from .codex import CodexAuth
from .copilot import CopilotAuth
from .errors import AuthError
from .grok import GrokAuth
from .opencode import OpenCodeAuth
from .store import CredentialError, CredentialStore

SIGN_INS = {"codex": CodexAuth, "copilot": CopilotAuth, "grok": GrokAuth, "opencode": OpenCodeAuth}

__all__ = ["SIGN_INS", "AuthError", "CodexAuth", "CopilotAuth", "CredentialError", "CredentialStore", "GrokAuth", "OpenCodeAuth"]
