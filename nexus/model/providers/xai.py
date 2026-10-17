"""xAI (Grok) adapter: the OpenAI Responses wire with xAI's own rules.

Plan section 8 keeps one adapter per wire protocol. xAI speaks the OpenAI
Responses dialect (``POST https://cli-chat-proxy.grok.com/v1/responses``, standard
``response.reasoning_summary_text.*`` reasoning events) and Chat Completions,
so this module is a thin preset over :class:`~nexus.model.providers.openai.OpenAIProvider`
rather than a second parser. It only owns what differs, as observed in the
OpenCode reference implementation (``packages/ai/src/providers/xai.ts``,
``packages/core/src/plugin/provider/xai.ts``):

* **Endpoint and credential.** ``https://cli-chat-proxy.grok.com/v1`` (the Grok
  CLI subscription inference proxy; ``api.x.ai`` is the direct API-key route and
  is not used) and ``api = "responses"``. The runtime connects xAI only through
  the SuperGrok subscription sign-in (``auth = "xai_oauth"``,
  :mod:`nexus.auth.xai`), whose OAuth access token is the Bearer, sent with
  ``x-xai-token-auth: xai-grok-cli``; there is no API-key route and no
  environment default. Authenticated inference on the proxy is not verified live.
* **Not stored.** xAI keeps Responses server-side unless ``store`` is false.
  Nexus never continues a chain by ``previous_response_id`` (it resends the
  whole history), so every Responses request sends ``store: false``. Encrypted
  reasoning is not requested: the OpenAI adapter does not replay it
  (``thinking: drop``), so it would only add bytes.
* **Cache routing.** The Nexus session id is sent as ``x-grok-conv-id`` so
  requests in one conversation reach the same prompt cache. OpenCode sends it
  on Chat Completions; its effect on ``/responses`` is not verified.
* **Images.** xAI rejects any image type other than PNG, JPEG and WebP
  (``invalid_image``), and a stored image would fail every later turn, so other
  image blocks become a short text note on the wire. Durable history keeps the
  original block.
* **No token counter.** xAI publishes no ``responses/input_tokens`` endpoint,
  so ``count_tokens`` returns ``None`` and the caller uses the heuristic.

Not implemented: xAI's Responses WebSocket transport and server-side hosted tools (``x_search``, ``web_search``); a
``*_call`` output item from a hosted tool is ignored by the shared decoder.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import msgspec

from ...errors import ProviderError
from ..message import Image, Message, Text, ToolResult
from ..stream import StreamEvent
from ..request import ModelRequest
from .openai import API_RESPONSES, OpenAIProvider

__all__ = ["DEFAULT_BASE_URL", "SUPPORTED_IMAGE_TYPES", "XAIProvider"]

#: The Grok CLI subscription inference proxy (xAI Enterprise docs); ``api.x.ai``
#: is the direct API-key route, which Nexus does not use.
DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
#: Marks a request as authenticated by the Grok CLI OAuth token; the same
#: header ``provider_usage`` sends to this host for billing and settings.
TOKEN_AUTH_HEADER = "x-xai-token-auth"
TOKEN_AUTH_VALUE = "xai-grok-cli"
#: Image media types the xAI API accepts; anything else is ``invalid_image``.
SUPPORTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})
#: Header xAI uses to route one conversation's requests to the same cache.
CONVERSATION_HEADER = "x-grok-conv-id"
#: Client headers the proxy requires (oh-my-pi issue 5978); without a version it
#: answers HTTP 426 "Your Grok CLI version (none) is outdated".
VERSION_HEADER = "x-grok-client-version"
IDENTIFIER_HEADER = "x-grok-client-identifier"
IDENTIFIER_VALUE = "grok-shell"
#: The proxy matches this user agent exactly; Nexus is not the official client.
USER_AGENT = "xai-grok-cli"
#: Explicit override when no Grok CLI is installed.
VERSION_ENV = "NEXUS_XAI_CLIENT_VERSION"
_MAX_VERSION_FILE = 4096
_VERSION_RE = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}(?:-[0-9A-Za-z.]{1,24})?")


def _safe_version(value: object) -> str | None:
    """``value`` if it is a plain version such as ``1.0.13``; never header-unsafe text."""
    if isinstance(value, str) and _VERSION_RE.fullmatch(value):
        return value
    return None


def installed_grok_version(home: Path | None = None) -> str | None:
    """The version in ``~/.grok/version.json`` (``version``, else ``stable_version``).

    A bounded read of one regular file: no subprocess, and a missing, oversized,
    corrupt or unsafe file yields ``None``.
    """
    path = (home or Path.home() / ".grok") / "version.json"
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            raw = handle.read(_MAX_VERSION_FILE + 1)
        if len(raw) > _MAX_VERSION_FILE:
            return None
        data = json.loads(raw)
    except (OSError, ValueError, RuntimeError):
        return None
    if not isinstance(data, dict):
        return None
    return _safe_version(data.get("version")) or _safe_version(data.get("stable_version"))


def _image_note(block: Image) -> Text:
    return Text(text=f"[image {block.media_type} omitted: xAI accepts only PNG, JPEG and WebP]")


def _supported(block: Image) -> bool:
    return block.media_type.strip().lower() in SUPPORTED_IMAGE_TYPES


def _filter_result(block: ToolResult) -> ToolResult:
    if all(not isinstance(item, Image) or _supported(item) for item in block.content):
        return block
    content = [_image_note(item) if isinstance(item, Image) and not _supported(item) else item
               for item in block.content]
    return msgspec.structs.replace(block, content=content)


def _filter_images(message: Message) -> Message:
    """Replace unsupported image blocks, including those inside tool results
    (which the OpenAI dialects forward on vision models), with a note."""
    content = [_filter_result(block) if isinstance(block, ToolResult)
               else _image_note(block) if isinstance(block, Image) and not _supported(block)
               else block
               for block in message.content]
    if all(new is old for new, old in zip(content, message.content, strict=True)):
        return message
    return msgspec.structs.replace(message, content=content)


class XAIProvider(OpenAIProvider):
    """A :class:`~nexus.model.provider.Provider` for xAI's Grok models."""

    name = "xai"
    DEFAULT_BASE_URL = DEFAULT_BASE_URL

    def __init__(self, *, api: str | None = None, grok_home: Path | None = None, **kwargs: Any) -> None:
        self._grok_home = grok_home
        self._grok_version: str | None = None
        # The credential is ``auth_headers`` (the SuperGrok sign-in); the
        # inherited ``api_key`` is only a test seam.
        super().__init__(api=api or API_RESPONSES, **kwargs)

    def __repr__(self) -> str:
        return super().__repr__().replace("OpenAIProvider(", "XAIProvider(", 1)

    def supports_count_tokens(self) -> bool:
        return False

    def _prepare_request(self, req: ModelRequest) -> ModelRequest:
        messages = [_filter_images(message) for message in req.messages]
        if all(new is old for new, old in zip(messages, req.messages, strict=True)):
            return req
        return msgspec.structs.replace(req, messages=messages)

    def _finalize_body(self, body: dict[str, Any], api: str) -> dict[str, Any]:
        if api == API_RESPONSES:
            body["store"] = False
        return body

    def _client_version(self) -> str:
        """The Grok CLI version to present, or a ``ProviderError`` saying how to supply one."""
        if self._grok_version is None:
            override = (os.environ if self._environ is None else self._environ).get(VERSION_ENV, "").strip()
            if override:
                version = _safe_version(override)
                if version is None:
                    raise ProviderError(f"xai: {VERSION_ENV} is not a plain version number such as 1.0.13")
            else:
                version = installed_grok_version(self._grok_home)
            if version is None:
                raise ProviderError(
                    "xai: the Grok subscription proxy requires a Grok CLI version and none was found. "
                    "Install the Grok CLI (https://x.ai/cli) and run `grok update` so ~/.grok/version.json exists, "
                    f"or set {VERSION_ENV} to the version you have installed"
                )
            self._grok_version = version
        return self._grok_version

    async def _stream_api(self, req: ModelRequest, model: str, api: str) -> AsyncIterator[StreamEvent]:
        try:
            async for event in super()._stream_api(req, model, api):
                yield event
        except ProviderError as exc:
            if getattr(exc, "status_code", None) != 426:
                raise
            sent = self._grok_version or "none"
            raise ProviderError(
                f"xai: the Grok proxy rejected client version {sent} as outdated (HTTP 426). Run `grok update`, "
                f"or set {VERSION_ENV} to a current Grok CLI version. Server said: {str(exc)[-200:]}"
            ) from None

    async def _headers(
        self, body: Mapping[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, str]:
        headers = await super()._headers(body, session_id)
        headers.setdefault(TOKEN_AUTH_HEADER, TOKEN_AUTH_VALUE)
        # The proxy treats a missing version as "(none)" and answers 426. The
        # value is read from the user's installed Grok CLI, never pinned here.
        # The user agent is the exact string the CLI proxy requires; that is a
        # protocol requirement, not a claim that Nexus is the official client.
        headers[VERSION_HEADER] = self._client_version()
        headers[IDENTIFIER_HEADER] = IDENTIFIER_VALUE
        headers["user-agent"] = USER_AGENT
        if session_id:
            headers.setdefault(CONVERSATION_HEADER, session_id)
        return headers
