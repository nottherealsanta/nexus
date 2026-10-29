"""OpenCode Go: an API key from the subscription (opencode.ai/auth), pasted once."""
from __future__ import annotations

from .api_key import api_key_auth

OpenCodeAuth = api_key_auth("opencode", "OPENCODE_API_KEY", "from opencode.ai/auth")
