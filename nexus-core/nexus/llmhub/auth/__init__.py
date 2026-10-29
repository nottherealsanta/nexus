"""Provider authentication. Each provider's sign-in lives here and hands the
LLM hub a header source (`async headers() -> dict`); adapters never see stored
credentials.

Every sign-in has the same shape: `methods`, `async login(method, notify)`,
`logged_in()`, `logout()` and `async headers()`.
"""
from __future__ import annotations

from .api_key import api_key_auth
from .codex import CodexAuth
from .copilot import CopilotAuth
from .errors import AuthError
from .grok import GrokAuth
from .ollama import OllamaAuth
from .opencode import OpenCodeAuth
from .store import CredentialError, CredentialStore

SIGN_INS = {
    "codex": CodexAuth,
    "copilot": CopilotAuth,
    "grok": GrokAuth,
    "opencode": OpenCodeAuth,
    "openai": api_key_auth("openai", "OPENAI_API_KEY", "from platform.openai.com/api-keys"),
    "groq": api_key_auth("groq", "GROQ_API_KEY", "from console.groq.com/keys"),
    "nvidia": api_key_auth("nvidia", "NVIDIA_API_KEY", "from build.nvidia.com"),
    "ollama": OllamaAuth,
}

__all__ = ["SIGN_INS", "AuthError", "CodexAuth", "CopilotAuth", "CredentialError", "CredentialStore", "GrokAuth", "OllamaAuth", "OpenCodeAuth"]
