from nexus.voice import hf_cache, status

H = "a" * 40


def make(root, repo, files, ref="main", incomplete=False):
    d = root / ("models--" + repo.replace("/", "--"))
    (d / "refs").mkdir(parents=True)
    (d / "refs" / ref).write_text(H)
    (d / "blobs").mkdir()
    snap = d / "snapshots" / H
    snap.mkdir(parents=True)
    for f in files:
        (snap / f).write_bytes(b"x" * 10)
        (d / "blobs" / f).write_bytes(b"x" * 10)
    if incomplete:
        (d / "blobs" / "z.incomplete").write_bytes(b"")


def test_cache_dir_precedence(monkeypatch, tmp_path):
    for k in ("HF_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "x"))
    assert hf_cache.cache_dir() == tmp_path / "x" / "huggingface" / "hub"
    monkeypatch.setenv("HF_HOME", str(tmp_path / "h"))
    assert hf_cache.cache_dir() == tmp_path / "h" / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "c"))
    assert hf_cache.cache_dir() == tmp_path / "c"


def test_find(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert hf_cache.find("a/b", "main", ["f"]) is None
    make(tmp_path, "a/b", ["f"])
    assert hf_cache.find("a/b", "main", ["f"]) is not None
    assert hf_cache.find("a/b", H, ["f"]) is not None
    assert hf_cache.find("a/b", "main", ["f", "g"]) is None
    assert hf_cache.find("a/b", "other", ["f"]) is None
    assert hf_cache.size("a/b") == 10
    assert [c.repo for c in hf_cache.scan()] == ["a/b"]


def test_incomplete_is_not_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    make(tmp_path, "a/b", ["f"], incomplete=True)
    assert hf_cache.find("a/b", "main", ["f"]) is None


def test_status_states(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert status.status()["tts"]["state"] == "absent"
    make(tmp_path, "gradium/phonon-31m-en", status.TTS["repos"][0][2])
    monkeypatch.setattr(status, "_missing", lambda m: ["ptts"])
    tts = status.status()["tts"]
    assert tts["state"] == "runtime_missing" and "nexus voice init" in tts["error"]
    monkeypatch.setattr(status, "_missing", lambda m: [])
    assert status.status()["tts"]["state"] == "ready"
