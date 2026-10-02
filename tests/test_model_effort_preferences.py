import json

import pytest

from nexus.model.effort_preferences import ModelEffortStore


def test_exact_reference_and_explicit_default_round_trip(tmp_path):
    path = tmp_path / "efforts.json"
    store = ModelEffortStore(path)

    assert store.get("provider/model") is None
    store.put("provider/model", None)
    store.put("provider/other", "high")

    restored = ModelEffortStore(path)
    assert restored.get("provider/model") == (None,)
    assert restored.get("provider/other") == ("high",)
    assert restored.get("Provider/model") is None


def test_invalid_efforts_and_keys_are_rejected(tmp_path):
    store = ModelEffortStore(tmp_path / "efforts.json")
    with pytest.raises(ValueError):
        store.put("provider/model", "invalid")
    with pytest.raises(ValueError):
        store.put("", "high")
    with pytest.raises(ValueError):
        store.put("x" * 257, "high")


def test_store_is_bounded_to_256_recent_entries(tmp_path):
    path = tmp_path / "efforts.json"
    store = ModelEffortStore(path)
    for index in range(257):
        store.put(f"provider/model-{index}", "low")

    restored = ModelEffortStore(path)
    assert restored.get("provider/model-0") is None
    assert restored.get("provider/model-1") == ("low",)
    assert restored.get("provider/model-256") == ("low",)
    assert len(restored._entries) == 256


@pytest.mark.parametrize("contents", [b"not json", b"{}", b"x" * (64 * 1024 + 1)])
def test_malformed_or_oversized_files_load_empty(tmp_path, contents):
    path = tmp_path / "efforts.json"
    path.write_bytes(contents)
    assert ModelEffortStore(path).get("provider/model") is None


def test_loader_skips_invalid_entries_and_caps_entry_count(tmp_path):
    path = tmp_path / "efforts.json"
    entries = {f"provider/model-{index}": "medium" for index in range(260)}
    entries["provider/bad"] = "not-an-effort"
    entries[""] = None
    path.write_text(json.dumps({"version": 1, "entries": entries}))

    store = ModelEffortStore(path)
    assert len(store._entries) == 256
    assert store.get("provider/model-0") is None
    assert store.get("provider/model-259") == ("medium",)
    assert store.get("provider/bad") is None


@pytest.mark.parametrize("remembered, expected", [("high", "high"), (None, None), ("max", None)])
def test_picker_prefers_destination_model_memory(remembered, expected):
    from nexus.ui_support.model_choice import preselected_effort, selection_effort

    row = {"provider": "provider", "id": "b", "supported_efforts": ["low", "high"],
           "remembered_effort": remembered}
    state = {"current": "provider/a", "current_effort": "low", "stored_override": "low"}
    assert preselected_effort(row, **state) == expected
    assert selection_effort(row, **state, effort_source="session", pending=None, touched=False) == (expected, True)
