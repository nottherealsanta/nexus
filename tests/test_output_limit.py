"""Shell output limit: 2,000 lines or 50 KiB, the rest saved to a temp file."""
from __future__ import annotations

import pytest

from nexus.tools.builtin import _output_limit as ol


@pytest.fixture(autouse=True)
def spill(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_OUTPUT_DIR", str(tmp_path / "spill"))
    return tmp_path / "spill"


def test_small_output_is_unchanged(spill):
    result = ol.limit_output("a\nb\n")
    assert result.text == "a\nb\n" and not result.truncated and result.path is None
    assert not spill.exists()


def test_too_many_lines_keeps_head_and_tail_and_saves_everything(spill):
    text = "".join(f"line {n}\n" for n in range(5000))
    result = ol.limit_output(text, label="bash-x")
    assert result.truncated and result.total_lines == 5000
    assert result.text.startswith("line 0\n") and result.text.endswith("line 4999\n")
    assert len(result.text.splitlines()) <= ol.MAX_LINES + 1
    assert open(result.path).read() == text
    assert ol.saved_path(result.text) == result.path
    assert (spill.stat().st_mode & 0o777) == 0o700


def test_too_many_bytes_is_bounded():
    text = ("x" * 1000 + "\n") * 200  # 200 lines, ~200 KB
    result = ol.limit_output(text)
    assert result.truncated
    assert len(result.text.encode()) <= ol.MAX_BYTES + 512


def test_spill_directory_is_bounded(spill, monkeypatch):
    monkeypatch.setattr(ol, "MAX_SPILL_FILES", 3)
    for _ in range(5):
        ol.limit_output("y\n" * 3000)
    assert len(list(spill.iterdir())) == 3
