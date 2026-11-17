"""`nexus bench`: the synthetic fixture generator and the in-process driver (plan 50, Phase 0). Small scale only."""
import os

import pytest

from nexus.bench import enable_mock_provider, fixture
from nexus.bench import run as bench_run
from nexus.store import Store


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    enable_mock_provider()


def test_small_fixture_has_the_manifest_shape(tmp_path):
    manifest = fixture.generate(tmp_path / "f" / "small.db", "small")
    assert manifest["scale"] == "small"
    assert manifest["counts"]["session"] >= 40
    assert manifest["counts"]["turn"] >= manifest["big_turns"]
    store = Store(tmp_path / "f" / "small.db")
    try:
        page = store.turn_summaries(manifest["big_session"], limit=200)
        assert len(page["turns"]) == 120 and manifest["big_turns"] == 120
        assert store.messages_page(manifest["big_session"], limit=50)
    finally:
        store.db.close()


def test_same_seed_same_counts(tmp_path):
    a = fixture.generate(tmp_path / "a" / "a.db", "small", seed=3)
    b = fixture.generate(tmp_path / "b" / "b.db", "small", seed=3)
    assert a["counts"] == b["counts"]


def test_refuses_to_overwrite(tmp_path):
    fixture.generate(tmp_path / "f" / "small.db", "small")
    with pytest.raises(FileExistsError):
        fixture.generate(tmp_path / "f" / "small.db", "small")


async def test_run_reports_routes_and_restores_env():
    before = {k: os.environ.get(k) for k in ("NEXUS10_HOME", "HOME")}
    result = await bench_run.run(scale="small", seconds=2, maintenance=False)
    assert {k: os.environ.get(k) for k in before} == before
    assert result["idle"]["routes"] and result["load"]["routes"]
    assert "loop_lag" in result["load"]
