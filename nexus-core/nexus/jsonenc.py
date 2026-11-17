"""JSON encoding for the HTTP API and event stream (docs/api.md#performance).

orjson (a Rust extension) encodes several times faster than the stdlib `json`; it is chosen over
msgspec because it takes the plain dicts, lists and dataclasses Core already builds, with no schema
types to maintain. The output is compact UTF-8 with non-ASCII characters left as they are, which is
what Starlette's `JSONResponse` already produced. Differences from the stdlib, all harmless to the
clients (serde_json, `JSON.parse`, Python `json.loads`):

* NaN and infinities encode as `null` (Starlette raised; stdlib `json.dumps` wrote invalid `NaN`).
* A float such as 1e16 is written `1e16`, not `1e+16`; both decode to the same number.
* SSE `data:` lines are compact UTF-8 instead of ASCII with `\\uXXXX` escapes and `", "` separators.
* Integers beyond 64 bits and lone surrogates (orjson refuses them) fall back to the stdlib encoder.

orjson holds the GIL while it encodes, so `dumps_async` does not make encoding free; it moves the
work to a thread so the event loop gets the GIL back every switch interval (~5 ms) instead of
waiting for the whole body. Use `ajson` for bodies that can be large.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import orjson
from starlette.responses import JSONResponse as _StarletteJSONResponse
from starlette.responses import Response

_OPTIONS = orjson.OPT_NON_STR_KEYS


def dumps(value: Any) -> bytes:
    """Compact UTF-8 JSON of `value`."""
    try:
        return orjson.dumps(value, option=_OPTIONS)
    except TypeError:
        # Beyond-64-bit ints and lone surrogates are valid for the stdlib encoder; anything else
        # (a set, an object) raises TypeError there too, as it always has.
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


async def dumps_async(value: Any) -> bytes:
    return await asyncio.to_thread(dumps, value)


class JSONResponse(_StarletteJSONResponse):
    def render(self, content: Any) -> bytes:
        return dumps(content)


class EncodedJSONResponse(Response):
    """A response whose body was already encoded (off the loop)."""

    media_type = "application/json"


async def ajson(content: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    """A JSON response encoded off the event loop, for large bodies."""
    return EncodedJSONResponse(await dumps_async(content), status_code=status_code, headers=headers)
