"""Prompt recall stays bounded and durable."""
from nexus.ui_support.prompt_history import append_history, load_history

def test_prompt_history_bounds_and_round_trip(tmp_path):
    path = tmp_path / "prompt_history"
    for index in range(505):
        append_history(f"prompt {index}", path)
    rows = load_history(path)
    assert len(rows) == 500
    assert rows[0] == "prompt 5" and rows[-1] == "prompt 504"
    append_history("x" * 4097, path)
    assert load_history(path) == rows
