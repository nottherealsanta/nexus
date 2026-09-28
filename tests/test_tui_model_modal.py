"""Model modal ordering, search, and persistent favorites."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from test_tui_model_picker_repro import ModelPickerTransport

from nexus.ui.cli.client import Client
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_model_picker import (
    ModelPickerScreen,
    recent_models,
    sort_models,
)


def test_models_sort_by_update_release_then_natural_name():
    rows = [
        {"provider": "p", "id": "z", "name": "Model 10", "last_updated": "2025-01-01"},
        {"provider": "p", "id": "a", "name": "Model 2", "last_updated": "2025-01-01"},
        {"provider": "p", "id": "b", "name": "New release", "last_updated": "2025-01-01", "release_date": "2025-02-01"},
        {"provider": "p", "id": "c", "name": "Old update", "last_updated": "2024-12-31", "release_date": "2026-01-01"},
        {"provider": "p", "id": "d", "name": "No dates"},
    ]
    assert [row["id"] for row in sort_models(rows)] == ["b", "a", "z", "c", "d"]
    assert [row["id"] for row in sort_models(rows, by="name")] == ["a", "z", "b", "d", "c"]


def test_six_calendar_month_filter_uses_update_then_release_and_keeps_unknown():
    rows = [
        {"id": "boundary", "last_updated": "2026-03-28"},
        {"id": "old", "last_updated": "2026-03-27"},
        {"id": "release", "release_date": "2026-04-01"},
        {"id": "old-release", "release_date": "2026-02-01"},
        {"id": "undated"},
        {"id": "updated", "last_updated": "2026-09-01", "release_date": "2020-01-01"},
    ]
    assert [row["id"] for row in recent_models(rows, today=date(2026, 9, 28))] == [
        "boundary", "release", "undated", "updated"
    ]
    assert [row["id"] for row in recent_models([
        {"id": "leap", "last_updated": "2024-02-29"},
        {"id": "before", "last_updated": "2024-02-28"},
    ], today=date(2024, 8, 31))] == ["leap"]


@pytest.mark.asyncio
async def test_modal_is_compact_and_sort_toggle_reorders_results():
    transport = ModelPickerTransport()
    transport.models = [
        {"provider": "fake", "id": "z", "name": "Zed", "last_updated": datetime.now(UTC).date().isoformat()},
        {"provider": "fake", "id": "a", "name": "Alpha"},
    ]
    app = NexusTextualApp(Client(transport), session="sort")
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ModelPickerScreen)
        search = modal.query_one("#model-picker-search")
        assert search.region.height == 1
        assert modal.query_one("#model-picker-dialog").region.width <= 90
        assert modal.query_one("#model-picker-dialog").region.height <= 29
        assert [row["id"] for row in modal._visible_rows if row] == ["z", "a"]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert modal.sort_mode == "name"
        assert [row["id"] for row in modal._visible_rows if row] == ["a", "z"]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert modal.sort_mode == "updated"
        assert [row["id"] for row in modal._visible_rows if row] == ["z", "a"]


@pytest.mark.asyncio
async def test_updated_sort_is_global_across_providers_and_shows_dates():
    now = datetime.now(UTC).date()
    transport = ModelPickerTransport()
    transport.models = [
        {"provider": "aaa", "id": "old", "name": "Alpha", "last_updated": (now - timedelta(days=20)).isoformat()},
        {"provider": "zzz", "id": "new", "name": "Zed", "last_updated": now.isoformat()},
        {"provider": "bbb", "id": "middle", "name": "Beta", "release_date": (now - timedelta(days=10)).isoformat()},
    ]
    app = NexusTextualApp(Client(transport), session="global-sort")
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ModelPickerScreen)
        assert [row["id"] for row in modal._visible_rows if row] == ["new", "middle", "old"]
        assert modal._visible_rows.count(None) == 1
        assert "Recently updated" in str(modal.query_one("#model-picker-options").get_option_at_index(0).prompt)
        assert now.isoformat() in str(modal.query_one("#model-picker-options").get_option_at_index(1).prompt)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert [row["id"] for row in modal._visible_rows if row] == ["old", "middle", "new"]


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
