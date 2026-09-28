"""Model modal ordering, search, and persistent favorites."""

from __future__ import annotations

import pytest
from test_tui_model_picker_repro import ModelPickerTransport

from nexus.ui.cli.client import Client
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_model_picker import ModelPickerScreen, sort_models


def test_models_sort_by_update_release_then_natural_name():
    rows = [
        {"provider": "p", "id": "z", "name": "Model 10", "last_updated": "2025-01-01"},
        {"provider": "p", "id": "a", "name": "Model 2", "last_updated": "2025-01-01"},
        {"provider": "p", "id": "b", "name": "New release", "last_updated": "2025-01-01", "release_date": "2025-02-01"},
        {"provider": "p", "id": "c", "name": "Old update", "last_updated": "2024-12-31", "release_date": "2026-01-01"},
        {"provider": "p", "id": "d", "name": "No dates"},
    ]
    assert [row["id"] for row in sort_models(rows)] == ["b", "a", "z", "c", "d"]


@pytest.mark.asyncio
async def test_modal_persists_favorite_and_recent_after_selection(tmp_path):
    transport = ModelPickerTransport()
    path = tmp_path / "tui.json"
    app = NexusTextualApp(Client(transport), session="favorites", preferences_path=path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ModelPickerScreen)
        await pilot.press("ctrl+f")
        assert app.prefs["model_favorites"] == ["fake/chosen"]
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.prefs["model_recent"] == ["fake/chosen"]
    restarted = NexusTextualApp(Client(ModelPickerTransport()), session="favorites", preferences_path=path)
    assert restarted.prefs["model_favorites"] == ["fake/chosen"]
    assert restarted.prefs["model_recent"] == ["fake/chosen"]
