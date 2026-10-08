"""Prompt recall stays bounded and durable."""
from nexus.ui_support.prompt_history import MAX_FILE_BYTES, MAX_PROMPT, append_history, load_history

def test_prompt_history_bounds_and_round_trip(tmp_path):
    path = tmp_path / "prompt_history"
    for index in range(505):
        append_history(f"prompt {index}", path)
    rows = load_history(path)
    assert len(rows) == 500
    assert rows[0] == "prompt 5" and rows[-1] == "prompt 504"
    append_history("x" * (MAX_PROMPT + 1), path)
    assert load_history(path) == rows


def test_long_prompts_are_recalled_within_the_file_budget(tmp_path):
    path = tmp_path / "prompt_history"
    long_prompt = "pasted line\n" * 50_000
    append_history("short", path)
    append_history(long_prompt, path)
    assert load_history(path) == ["short", long_prompt]
    for index in range(40):
        append_history(f"{index}" + "y" * (MAX_PROMPT - 10), path)
    rows = load_history(path)
    assert path.stat().st_size <= MAX_FILE_BYTES
    assert rows and rows[-1].startswith("39") and "short" not in rows
