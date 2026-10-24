"""Bounded, JSON-only helper protocol; no pickle or interpreter snapshots.

Frames are a 4-byte big-endian length plus UTF-8 JSON. Host callbacks are
correlated by an integer ``id`` so several can be in flight at once (plan 03
section 3.6; contract I3). The helper imports this file as a top-level module,
so it must stay free of ``nexus`` imports.
"""
from __future__ import annotations

import json
import math
import struct

MAX_FRAME = 2 * 1024 * 1024
#: Snapshot frames only (plan code-mode-kernel §3.8): a ``load`` request or a ``dump`` reply carries
#: a base64 kernel snapshot of up to 8 MiB (about 10.7 MiB encoded). Every other frame keeps
#: ``MAX_FRAME``; ``frame_limit`` decides by the frame's own shape, so a stray large frame is refused.
MAX_SNAPSHOT_FRAME = 16 * 1024 * 1024
MAX_DEPTH = 32


def frame_limit(value):
    """The size ceiling for one frame: larger only for snapshot ``load`` requests and ``dump`` replies."""
    if type(value) is dict and (value.get('verb') == 'load' or value.get('snapshot') is True):
        return MAX_SNAPSHOT_FRAME
    return MAX_FRAME


def validate(value, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError('JSON nesting exceeds 32')
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        for item in value:
            validate(item, depth + 1)
        return value
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            validate(item, depth + 1)
        return value
    raise ValueError('only finite JSON values may cross sandbox boundary')


def encode(value):
    data = json.dumps(validate(value), ensure_ascii=True, allow_nan=False).encode()
    if len(data) > frame_limit(value):
        raise ValueError('IPC frame exceeds limit')
    return struct.pack('!I', len(data)) + data


async def receive(reader):
    size = struct.unpack('!I', await reader.readexactly(4))[0]
    if size > MAX_SNAPSHOT_FRAME:
        raise ValueError('IPC frame exceeds limit')
    value = validate(json.loads(await reader.readexactly(size)))
    if size > frame_limit(value):
        raise ValueError('IPC frame exceeds limit')
    return value
