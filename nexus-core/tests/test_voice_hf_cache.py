from nexus.voice import hf_cache, status
from nexus.voice.store import EXPECTED_SIZE

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


def test_stt_status_reports_the_pinned_model_and_download_size(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setattr(status, "_platform_stt_supported", lambda: True)
    stt = status.status()["stt"]
    assert stt["repo"] == "moondream/parakeet-redux"
    assert stt["revision"] == "2bf128600aac4b16946f7ed8372e56117fe5e23b"
    assert stt["state"] == "absent"
    assert stt["size"] == EXPECTED_SIZE > 0


def test_stt_status_rejects_an_unreviewed_runtime_version(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    repo, revision, files = status.STT["repos"][0]
    make(tmp_path, repo, files)
    snapshots = tmp_path / ("models--" + repo.replace("/", "--")) / "snapshots"
    (snapshots / H).rename(snapshots / revision)
    monkeypatch.setattr(status, "_missing", lambda _modules: [])
    monkeypatch.setattr(
        status.metadata, "version",
        lambda name: "2.7.0" if name == "moondream" else "0.9.1",
    )
    stt = status.entry(status.STT, supported=True)
    assert stt["state"] == "runtime_missing"
    assert "moondream 2.7.0 is not supported" in stt["error"]
    assert "reviewed: 2.6.1" in stt["error"]


def test_stt_status_explains_the_bark_migration(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.toml").write_text(
        '[voice]\nstt_model = "ConwayResearch/Underdog-Bark-3.0-Clean"\n', encoding="utf-8"
    )
    monkeypatch.setattr(status, "_platform_stt_supported", lambda: True)
    stt = status.status()["stt"]
    assert stt["state"] == "unsupported"
    assert "no longer supported" in stt["error"]
    assert "moondream/parakeet-redux" in stt["error"]


def test_stt_platform_support_matches_the_native_wheels():
    assert status._stt_supported("Darwin", "arm64", "", "", "14.0")
    assert not status._stt_supported("Darwin", "arm64", "", "", "13.6")
    assert not status._stt_supported("Darwin", "x86_64", "", "", "14.0")
    assert status._stt_supported("Linux", "x86_64", "glibc", "2.28", "")
    assert not status._stt_supported("Linux", "x86_64", "glibc", "2.27", "")
    assert status._stt_supported("Linux", "aarch64", "glibc", "2.34", "")
    assert not status._stt_supported("Linux", "aarch64", "glibc", "2.31", "")
    assert not status._stt_supported("Linux", "x86_64", "musl", "1.2", "")
