"""orjson encoding must decode to what the stdlib encoding decoded to (docs/api.md#performance)."""
import json
import math

import pytest

from nexus import jsonenc
from nexus.server import sse

PAYLOADS = [
    {"text": "héllo → 日本語 😀", "n": 1, "f": 0.1, "big": 2**53 + 1, "neg": -(2**63), "t": True, "none": None},
    {"floats": [1e16, 1.5e-7, 1e22, -0.0, 3.141592653589793, 123456789.123456789, 5e-324]},
    {"nested": {"z": 1, "a": [1, {"m": "x", "b": 2}], "k": {"y": 0, "x": 0}}},
    {"escapes": 'quote " backslash \\ newline \n tab \t nul \x00 del \x7f sep   '},
    {1: "int key", False: "bool key", None: "none key"},
    {"huge": 2**70, "tuple": (1, 2), "empty": {}, "list": []},
    {"lone": "bad \ud800 surrogate"},
    [{"type": "text", "text": "x" * 10000, "raw": {"a": [None] * 50}}],
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_decodes_like_the_stdlib(payload):
    expected = json.loads(json.dumps(payload))  # the old SSE encoder (ASCII escapes, any key type)
    assert json.loads(jsonenc.dumps(payload)) == expected


@pytest.mark.parametrize("payload", PAYLOADS[:6])
def test_key_order_is_kept_at_every_level(payload):
    old = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    assert json.loads(jsonenc.dumps(payload), object_pairs_hook=list) == json.loads(old, object_pairs_hook=list)


def test_non_ascii_is_raw_utf8_like_starlettes_json_response():
    assert "日本語".encode() in jsonenc.dumps({"t": "日本語"})


def test_exact_float_and_int_roundtrip():
    for value in (0.1, 1e16, 1.7976931348623157e308, 2**63 - 1, 2**64 - 1, -(2**63), 2**70):
        assert json.loads(jsonenc.dumps({"v": value}))["v"] == value


def test_nan_and_infinity_are_null():
    assert json.loads(jsonenc.dumps([math.nan, math.inf, -math.inf, 1.0])) == [None, None, None, 1.0]


def test_unsupported_values_still_raise_type_error():
    with pytest.raises(TypeError):
        jsonenc.dumps({"s": {1, 2}})


def test_response_is_compact_utf8():
    response = jsonenc.JSONResponse({"a": "é", "b": [1, 2]})
    assert response.body == '{"a":"é","b":[1,2]}'.encode()
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize("event", [
    {"id": 7, "cursor": "g:7", "type": "turn.delta", "text": "héllo 😀 \n line   sep \r", "n": 2**40},
    {"type": "stream.ping"},
    {"id": 1, "type": "x", "bad": "\ud800"},
])
def test_sse_frame_decodes_like_before(event):
    frame = sse(event)
    assert frame.endswith("\n\n")
    lines = frame.rstrip("\n").split("\n")
    # One `data:` line: JSON never contains a raw line break, whatever the text.
    assert sum(line.startswith("data: ") for line in lines) == 1
    assert "\r" not in frame
    data = json.loads(next(line for line in lines if line.startswith("data: "))[6:])
    assert data == json.loads(json.dumps(event))
    assert ("id: " in frame) == (event.get("id") is not None)


async def test_ajson_matches_sync_encoding():
    payload = {"rows": [{"i": i, "s": "ü" * 20} for i in range(1000)]}
    response = await jsonenc.ajson(payload, status_code=201, headers={"cache-control": "no-store"})
    assert response.body == jsonenc.dumps(payload)
    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
