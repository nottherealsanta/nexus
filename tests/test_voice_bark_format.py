"""Bounds and malformed-input checks for the .bark reader (no numpy needed)."""
import json
import lzma

import pytest

from nexus.voice import bark


def build(index, payload=b"", fmt="bark-levels-v3", config=None):
    head = json.dumps({
        "format": fmt, "index": index,
        "config": json.dumps({} if config is None else config),
    }).encode()
    return len(head).to_bytes(8, "little") + head + payload


RAW = {"n": "w", "k": "raw", "dtype": "torch.float16", "shape": [2, 3], "b": 12}


def test_valid_raw_roundtrip_layout():
    config, entries = bark.parse_layout(build([RAW], bytes(12), config={"a": 1}))
    assert config == {"a": 1}
    assert entries[0].name == "w" and entries[0].shape == (2, 3)


def test_scalar_raw_layout_counts_one_element():
    entry = dict(RAW, dtype="torch.int64", shape=[], b=8)
    _, entries = bark.parse_layout(build([entry], bytes(8)))
    assert entries[0].shape == ()


@pytest.mark.parametrize("size", [0, 7, 9, 16])
def test_scalar_raw_rejects_wrong_payload_size(size):
    entry = dict(RAW, dtype="torch.int64", shape=[], b=size)
    with pytest.raises(bark.BarkFormatError, match="raw size"):
        bark.parse_layout(build([entry], bytes(size)))


@pytest.mark.parametrize("mutate", [
    lambda: b"short",
    lambda: (10**9).to_bytes(8, "little") + b"{}",
    lambda: (0).to_bytes(8, "little") + b"x",
    lambda: build([RAW], bytes(12), fmt="other"),
    lambda: build([RAW], bytes(13)),            # trailing bytes
    lambda: build([RAW], bytes(11)),            # truncated
    lambda: build([RAW, RAW], bytes(24)),       # duplicate name
    lambda: build([dict(RAW, shape=[0, 3])], b""),
    lambda: build([dict(RAW, shape=[10**6, 10**6])], bytes(12)),
    lambda: build([dict(RAW, shape=[1] * 5)], bytes(2)),
    lambda: build([dict(RAW, b=-1)], b""),
    lambda: build([dict(RAW, b=13)], bytes(13)),  # size != shape * dtype
    lambda: build([dict(RAW, k="mystery")], bytes(12)),
    lambda: build([dict(RAW, shape=[True, 3])], bytes(12)),
    lambda: build([dict(RAW, shape=None)], bytes(12)),
    lambda: build([{"n": "l", "k": "levels2", "shape": []}], b""),
    lambda: build([{"n": "q", "k": "ansint9", "shape": [2, 2], "s": 4, "meta": 8, "b": 4}], bytes(16)),
    lambda: build([{"n": "l", "k": "levels2", "K": 200, "shape": [2, 2],
                    "lev": 1, "meta": 1, "b": 4}], bytes(6)),
    lambda: build([], b""),
])
def test_malformed_files_rejected(mutate):
    with pytest.raises(bark.BarkFormatError):
        bark.parse_layout(mutate())


def test_bad_config_rejected():
    head = json.dumps({"format": "bark-levels-v3", "index": [RAW], "config": "[1]"}).encode()
    with pytest.raises(bark.BarkFormatError):
        bark.parse_layout(len(head).to_bytes(8, "little") + head + bytes(12))


def test_open_bark_decodes_raw_tensors_lazily(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("constriction")
    weights = np.arange(6, dtype=np.float16).reshape(2, 3)
    counter = np.array([7], dtype=np.int64)
    entries = [
        RAW,
        {"n": "n", "k": "raw", "dtype": "torch.int64", "shape": [1], "b": 8},
    ]
    path = tmp_path / "model.bark"
    path.write_bytes(build(entries, weights.tobytes() + counter.tobytes(), config={"a": 1}))
    config, tensors = bark.open_bark(path)
    assert config == {"a": 1}
    decoded = dict(tensors)
    assert decoded["w"].tolist() == weights.tolist() and decoded["w"].dtype == np.float16
    assert decoded["n"].tolist() == [7] and decoded["n"].dtype == np.int64
    state, _ = bark.load_state_dict(path)
    assert sorted(state) == ["n", "w"]


@pytest.mark.parametrize("compressed", [False, True])
def test_open_bark_decodes_scalar_batch_counter(tmp_path, compressed):
    np = pytest.importorskip("numpy")
    pytest.importorskip("constriction")
    counter = np.array(7, dtype=np.int64)
    payload = counter.tobytes()
    if compressed:
        payload = lzma.compress(payload)
    entry = {
        "n": "encoder.layers.0.conv.norm.num_batches_tracked",
        "k": "raw", "dtype": "torch.int64", "shape": [],
        "b": len(payload), "zr": compressed,
    }
    path = tmp_path / "model.bark"
    path.write_bytes(build([entry], payload))
    _, tensors = bark.open_bark(path)
    name, value = next(tensors)
    assert name == entry["n"]
    assert value.shape == () and value.dtype == np.int64
    assert value.item() == 7


def test_open_bark_rejects_bad_files_before_decoding(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("constriction")
    bad = tmp_path / "bad.bark"
    bad.write_bytes(build([RAW], bytes(11)))
    with pytest.raises(bark.BarkFormatError):
        bark.open_bark(bad)  # truncated: rejected eagerly, not on first tensor
    with pytest.raises(bark.BarkFormatError):
        bark.open_bark(tmp_path / "missing.bark")
    link = tmp_path / "link.bark"
    link.symlink_to(bad)
    with pytest.raises(bark.BarkFormatError):
        bark.open_bark(link)


def test_compressed_raw_is_bounded():
    bomb = lzma.compress(bytes(10_000))
    assert bark.bounded_unlzma(lzma.compress(bytes(12)), 12) == bytes(12)
    with pytest.raises(bark.BarkFormatError):
        bark.bounded_unlzma(bomb, 12)
    with pytest.raises(bark.BarkFormatError):
        bark.bounded_unlzma(b"not lzma", 12)
    with pytest.raises(bark.BarkFormatError):
        bark.bounded_unlzma(lzma.compress(bytes(12))[:-8], 12)
