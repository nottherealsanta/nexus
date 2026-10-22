"""Strict, bounded reader for Underdog Bark ``.bark`` weight files.

Feeds ``engine.BarkEngine`` (the local dictation model). The decoding
method is reproduced from the reviewed upstream ``bark_loader.py`` (ConwayResearch/
Underdog-Bark-3.0-Clean, CC-BY-4.0). No upstream code is downloaded or executed.
The file is untrusted data: every size, offset and dimension is validated before
it is used, and decompression is capped at the size the index promises.

Layout: 8-byte little-endian header length, JSON header, then one payload per
index entry (``levels2``, ``ansintN`` or ``raw``), with no trailing bytes.
"""
from __future__ import annotations

import json
import lzma
import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMATS = ("bark-levels-v2", "bark-levels-v3")
MAX_FILE_BYTES = 192 * 1024 * 1024
MAX_HEADER_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_NAME_CHARS = 256
MAX_RANK = 4
MAX_TOTAL_ELEMENTS = 800_000_000
NC = 16  # column classes per levels2 tensor
_RAW_DTYPES = {"torch.int64": 8, "torch.int32": 4}  # anything else is fp16


class BarkFormatError(ValueError):
    """The file is malformed, oversized or not a supported Bark format."""


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    shape: tuple[int, ...]
    fields: dict[str, int]
    flags: dict[str, bool]
    dtype: str
    bits: int
    offset: int  # start of this entry's payload


def _int(value: Any, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise BarkFormatError(f"invalid {what}")
    return value


def parse_layout(raw: bytes) -> tuple[dict[str, Any], list[Entry]]:
    """Validate the header and every payload extent; return (config, entries)."""
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) > MAX_FILE_BYTES:
        raise BarkFormatError("bark file too large")
    raw = bytes(raw)
    if len(raw) < 8:
        raise BarkFormatError("bark file truncated")
    n = int.from_bytes(raw[:8], "little")
    if n == 0 or n > MAX_HEADER_BYTES or 8 + n > len(raw):
        raise BarkFormatError("invalid bark header size")
    try:
        head = json.loads(raw[8:8 + n])
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BarkFormatError("invalid bark header") from exc
    if not isinstance(head, dict) or head.get("format") not in FORMATS:
        raise BarkFormatError("unsupported bark format")
    index = head.get("index")
    if not isinstance(index, list) or not 0 < len(index) <= MAX_ENTRIES:
        raise BarkFormatError("invalid bark index")
    config_text = head.get("config")
    try:
        config = json.loads(config_text) if isinstance(config_text, str) else None
    except (ValueError, RecursionError):
        config = None
    if not isinstance(config, dict):
        raise BarkFormatError("invalid bark config")

    pos, seen, total = 8 + n, set(), 0
    entries: list[Entry] = []
    for item in index:
        if not isinstance(item, dict):
            raise BarkFormatError("invalid bark index entry")
        name = item.get("n")
        if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME_CHARS or name in seen:
            raise BarkFormatError("invalid bark tensor name")
        seen.add(name)
        shape = item.get("shape")
        if not isinstance(shape, list) or not 0 < len(shape) <= MAX_RANK:
            raise BarkFormatError("invalid bark tensor shape")
        dims = tuple(_int(d, 1, MAX_TOTAL_ELEMENTS, "bark dimension") for d in shape)
        count = math.prod(dims)
        total += count
        if count > MAX_TOTAL_ELEMENTS or total > MAX_TOTAL_ELEMENTS:
            raise BarkFormatError("bark tensors exceed the element budget")
        kind = item.get("k")
        if not isinstance(kind, str):
            raise BarkFormatError("invalid bark tensor kind")

        def size(key: str) -> int:
            return _int(item.get(key), 0, MAX_FILE_BYTES, f"bark {key} size")

        fields: dict[str, int] = {}
        flags: dict[str, bool] = {}
        dtype, bits, extent = "", 0, 0
        if kind == "levels2":
            k = _int(item.get("K"), 1, 127, "bark level count")  # codes are uint8
            fields = {"K": k, "lev": size("lev"), "meta": size("meta"), "b": size("b")}
            flags = {"lev": bool(item.get("zl"))}
            cols = math.prod(dims[1:])
            if fields["lev"] != 0 and not flags["lev"] and fields["lev"] != dims[0] * k * 2:
                raise BarkFormatError("invalid bark levels size")
            if fields["meta"] != (cols * 4 + 7) // 8 + NC * (2 * k + 1) * 4:
                raise BarkFormatError("invalid bark metadata size")
            if fields["b"] % 4 or not fields["b"]:
                raise BarkFormatError("invalid bark code size")
            extent = fields["lev"] + fields["meta"] + fields["b"]
        elif kind.startswith("ansint"):
            digits = kind[6:]
            bits = int(digits) if digits.isdigit() and len(digits) == 1 else 0
            if not 2 <= bits <= 8:
                raise BarkFormatError("invalid bark integer width")
            fields = {"s": size("s"), "meta": size("meta"), "b": size("b")}
            flags = {"s": bool(item.get("zs"))}
            if not fields["s"] or fields["meta"] % 4 or not 4 <= fields["meta"] <= 4 * 256:
                raise BarkFormatError("invalid bark scale or model size")
            if fields["b"] % 4 or not fields["b"]:
                raise BarkFormatError("invalid bark code size")
            if not flags["s"] and (fields["s"] % 2 or count % (fields["s"] // 2)):
                raise BarkFormatError("invalid bark scale size")
            extent = fields["s"] + fields["meta"] + fields["b"]
        elif kind == "raw":
            dtype = item.get("dtype")
            if not isinstance(dtype, str) or len(dtype) > 32:
                raise BarkFormatError("invalid bark raw dtype")
            fields = {"b": size("b")}
            flags = {"raw": bool(item.get("zr"))}
            if not flags["raw"] and fields["b"] != count * _RAW_DTYPES.get(dtype, 2):
                raise BarkFormatError("invalid bark raw size")
            extent = fields["b"]
        else:
            raise BarkFormatError("unsupported bark tensor kind")
        if pos + extent > len(raw):
            raise BarkFormatError("bark file truncated")
        entries.append(Entry(name, kind, dims, fields, flags, dtype, bits, pos))
        pos += extent
    if pos != len(raw):
        raise BarkFormatError("unexpected trailing bark bytes")
    return config, entries


def bounded_unlzma(blob: bytes, limit: int) -> bytes:
    """LZMA-decompress, refusing output beyond ``limit`` bytes."""
    decoder = lzma.LZMADecompressor()
    try:
        out = decoder.decompress(blob, max_length=limit + 1)
    except lzma.LZMAError as exc:
        raise BarkFormatError("invalid bark compressed block") from exc
    if len(out) > limit or not decoder.eof or decoder.unused_data:
        raise BarkFormatError("invalid bark compressed block")
    return out


def _side(raw: bytes, start: int, length: int, zipped: bool, limit: int, exact: int | None) -> bytes:
    blob = raw[start:start + length]
    data = bounded_unlzma(blob, limit) if zipped else blob
    if exact is not None and len(data) != exact:
        raise BarkFormatError("bark block has the wrong size")
    return data


def open_bark(path: Path | str) -> tuple[dict[str, Any], Iterator[tuple[str, Any]]]:
    """Validate the whole layout now; return (model_config, lazy (name, ndarray) iterator).

    Tensors decode one at a time, so a caller that copies each into the model
    holds only one decoded tensor beyond the model itself. Needs numpy and constriction.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise BarkFormatError("bark file missing or too large")
    raw = path.read_bytes()
    config, entries = parse_layout(raw)
    import numpy as np
    import constriction

    def tensors() -> Iterator[tuple[str, Any]]:
        for e in entries:
            try:
                value = _decode(raw, e, np, constriction)
            except BarkFormatError:
                raise
            except Exception as exc:  # corrupt entropy streams surface as arbitrary errors
                raise BarkFormatError(f"cannot decode bark tensor {e.name!r}") from exc
            yield e.name, value

    return config, tensors()


def load_state_dict(path: Path | str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ({name: float32/int ndarray}, model_config). Needs numpy and constriction."""
    config, tensors = open_bark(path)
    return dict(tensors), config


def _ans(constriction: Any, np: Any, words: bytes) -> Any:
    return constriction.stream.stack.AnsCoder(np.frombuffer(words, np.uint32).copy())


def _model(constriction: Any, counts: Any) -> Any:
    total = counts.sum()
    if not total > 0:
        raise BarkFormatError("invalid bark symbol counts")
    return constriction.stream.model.Categorical(counts / total, perfect=False)


def _decode(raw: bytes, e: Entry, np: Any, constriction: Any) -> Any:
    p, shape, count = e.offset, e.shape, math.prod(e.shape)
    f = e.fields
    if e.kind == "levels2":
        k, rows, cols = f["K"], shape[0], math.prod(shape[1:])
        lev = np.frombuffer(
            _side(raw, p, f["lev"], e.flags["lev"], rows * k * 2, rows * k * 2), np.float16,
        ).reshape(rows, k)
        p += f["lev"]
        meta = raw[p:p + f["meta"]]
        p += f["meta"]
        nb = (cols * 4 + 7) // 8
        bits = np.unpackbits(np.frombuffer(meta[:nb], np.uint8))[:cols * 4].reshape(cols, 4)
        cls = np.packbits(np.concatenate([np.zeros((cols, 4), np.uint8), bits], 1), axis=1)[:, 0]
        counts = np.frombuffer(meta[nb:], np.uint32).reshape(NC, 2 * k + 1).astype(np.float64)
        dec = _ans(constriction, np, raw[p:p + f["b"]])
        codes = np.empty((rows, cols), np.uint8)
        for c in range(NC):
            idx = np.nonzero(cls == c)[0]
            if len(idx):
                model = _model(constriction, counts[c])
                codes[:, idx] = dec.decode(model, rows * len(idx)).reshape(len(idx), rows).T
        levf = lev.astype(np.float32)
        mag = np.where(codes == 0, 0, np.where(codes <= k, codes, codes - k)).astype(np.int64)
        table = np.concatenate([np.zeros((rows, 1), np.float32), levf], 1)
        val = np.take_along_axis(table, mag, 1)
        return np.where(codes > k, -val, val).reshape(shape)
    if e.kind.startswith("ansint"):
        scale = np.frombuffer(
            _side(raw, p, f["s"], e.flags["s"], count * 2, None), np.float16,
        ).astype(np.float32)
        p += f["s"]
        if not len(scale) or count % len(scale):
            raise BarkFormatError("invalid bark scale size")
        counts = np.frombuffer(raw[p:p + f["meta"]], np.uint32).astype(np.float64)
        p += f["meta"]
        sym = _ans(constriction, np, raw[p:p + f["b"]]).decode(_model(constriction, counts), count)
        q = (sym.astype(np.int64) - (2 ** (e.bits - 1) - 1)).astype(np.float32)
        return (q.reshape(len(scale), -1) * scale[:, None]).reshape(shape)
    dtype = {"torch.int64": np.int64, "torch.int32": np.int32}.get(e.dtype, np.float16)
    width = np.dtype(dtype).itemsize
    data = _side(raw, p, f["b"], e.flags["raw"], count * width, count * width)
    return np.frombuffer(data, dtype).copy().reshape(shape)
