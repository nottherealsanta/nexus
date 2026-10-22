"""NVIDIA (build.nvidia.com / NIM) adapter: Chat Completions with NVIDIA's rules.

Plan section 8 keeps one adapter per wire protocol. NVIDIA's hosted catalogue
serves the OpenAI Chat Completions dialect at ``https://integrate.api.nvidia.com/v1``
(models.dev lists it as ``@ai-sdk/openai-compatible``; Pi's ``nvidia.ts`` uses
its ``openai-completions`` API with ``NVIDIA_API_KEY``), so this module is a thin
preset over :class:`~nexus.model.providers.openai.OpenAIProvider`, not a second
parser. It only owns what differs:

* **Endpoint and credential.** The default ``base_url`` and an
  ``${env:NVIDIA_API_KEY}`` key when neither a key nor a stored credential is
  configured. A pasted key from Settings → Providers (``auth = "keychain"``) works
  through the shared ``auth_headers`` seam. The dialect is always Chat
  Completions (``/chat/completions``); there is no Responses route.
* **Reasoning.** NVIDIA-hosted reasoning models stream their thoughts as
  ``reasoning_content``, which the shared chat decoder already maps to
  ``ThinkingDelta``; nothing is added here.
* **Attribution.** OpenCode (``packages/core/src/plugin/provider/nvidia.ts``)
  sends ``HTTP-Referer``, ``X-Title`` and ``X-BILLING-INVOKE-ORIGIN``. The first
  two name OpenCode's site and are not copied; Nexus sends only
  ``X-BILLING-INVOKE-ORIGIN: Nexus``. Whether NVIDIA uses it is not verified.
  A user header of the same name wins.
* **No token counter.** NVIDIA publishes no input-token endpoint, so
  ``count_tokens`` returns ``None`` and the caller uses the heuristic.

Not implemented: ``NVCF-POLL-SECONDS`` (Pi does not send it), and NVIDIA's image,
embedding and classifier models. Nothing here is verified against the live API.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...errors import ProviderError
from .openai import API_CHAT, OpenAIProvider

__all__ = ["BILLING_ORIGIN_HEADER", "DEFAULT_BASE_URL", "ENV_API_KEY", "NvidiaProvider"]

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
#: The catalogue's key variable (models.dev ``env``; Pi reads the same name).
ENV_API_KEY = "NVIDIA_API_KEY"
BILLING_ORIGIN_HEADER = "X-BILLING-INVOKE-ORIGIN"
BILLING_ORIGIN_VALUE = "Nexus"


class NvidiaProvider(OpenAIProvider):
    """A :class:`~nexus.model.provider.Provider` for NVIDIA's hosted models."""

    name = "nvidia"
    DEFAULT_BASE_URL = DEFAULT_BASE_URL

    def __init__(self, *, api: str | None = None, api_key: str | None = None, **kwargs: Any) -> None:
        if api not in (None, API_CHAT):
            raise ProviderError("nvidia: only api = 'chat' is supported")
        if not api_key and kwargs.get("auth_headers") is None:
            api_key = "${env:" + ENV_API_KEY + "}"
        super().__init__(api=API_CHAT, api_key=api_key, **kwargs)

    def __repr__(self) -> str:
        return super().__repr__().replace("OpenAIProvider(", "NvidiaProvider(", 1)

    def supports_count_tokens(self) -> bool:
        return False

    async def _headers(
        self, body: Mapping[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, str]:
        headers = await super()._headers(body, session_id)
        if not any(key.lower() == BILLING_ORIGIN_HEADER.lower() for key in headers):
            headers[BILLING_ORIGIN_HEADER] = BILLING_ORIGIN_VALUE
        return headers
