"""xAI (Grok) adapter: the OpenAI Responses wire with xAI's own rules.

Plan section 8 keeps one adapter per wire protocol. xAI speaks the OpenAI
Responses dialect (``POST https://api.x.ai/v1/responses``, standard
``response.reasoning_summary_text.*`` reasoning events) and Chat Completions,
so this module is a thin preset over :class:`~nexus.model.providers.openai.OpenAIProvider`
rather than a second parser. It only owns what differs, as observed in the
OpenCode reference implementation (``packages/ai/src/providers/xai.ts``,
``packages/core/src/plugin/provider/xai.ts``):

* **Endpoint and credential.** ``https://api.x.ai/v1`` and ``api = "responses"``.
  The runtime connects xAI only through the SuperGrok subscription sign-in
  (``auth = "xai_oauth"``, :mod:`nexus.auth.xai`), whose OAuth access token is
  the Bearer; there is no API-key route and no environment default.
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

from collections.abc import Mapping
from typing import Any

import msgspec

from ..message import Image, Message, Text
from ..request import ModelRequest
from .openai import API_RESPONSES, OpenAIProvider

__all__ = ["DEFAULT_BASE_URL", "SUPPORTED_IMAGE_TYPES", "XAIProvider"]

DEFAULT_BASE_URL = "https://api.x.ai/v1"
#: Image media types the xAI API accepts; anything else is ``invalid_image``.
SUPPORTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})
#: Header xAI uses to route one conversation's requests to the same cache.
CONVERSATION_HEADER = "x-grok-conv-id"


def _image_note(block: Image) -> Text:
    return Text(text=f"[image {block.media_type} omitted: xAI accepts only PNG, JPEG and WebP]")


def _supported(block: Image) -> bool:
    return block.media_type.strip().lower() in SUPPORTED_IMAGE_TYPES


def _filter_images(message: Message) -> Message:
    """Replace unsupported image blocks with a note. Tool-result images never
    reach the wire (the OpenAI dialects flatten tool results to text)."""
    if not any(isinstance(block, Image) and not _supported(block) for block in message.content):
        return message
    content = [_image_note(block) if isinstance(block, Image) and not _supported(block) else block
               for block in message.content]
    return msgspec.structs.replace(message, content=content)


class XAIProvider(OpenAIProvider):
    """A :class:`~nexus.model.provider.Provider` for xAI's Grok models."""

    name = "xai"
    DEFAULT_BASE_URL = DEFAULT_BASE_URL

    def __init__(self, *, api: str | None = None, **kwargs: Any) -> None:
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

    async def _headers(
        self, body: Mapping[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, str]:
        headers = await super()._headers(body, session_id)
        if session_id:
            headers.setdefault(CONVERSATION_HEADER, session_id)
        return headers
