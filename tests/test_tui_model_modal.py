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


@pytest.mark.asyncio
async def test_modal_explains_when_workspace_has_no_configured_models():
    transport = ModelPickerTransport()
    transport.models = []
    app = NexusTextualApp(Client(transport), session="empty-models")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ModelPickerScreen)
        option = modal.query_one("#model-picker-options").get_option_at_index(0)
        assert "No selectable models" in str(option.prompt)
        assert "nexus.toml" in str(option.prompt)


@pytest.mark.asyncio
async def test_ctrl_r_refreshes_catalogue_in_place():
    transport = ModelPickerTransport()
    transport.models = [{"provider": "fake", "id": "old", "name": "Old"}]
    app = NexusTextualApp(Client(transport), session="refresh")
    fresh = [
        {"provider": "fake", "id": "old", "name": "Old"},
        {"provider": "fake", "id": "new", "name": "Brand New"},
    ]
    calls = []

    async def refresh():
        calls.append(1)
        return fresh

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        screen = ModelPickerScreen(list(transport.models), current="", current_effort=None,
                                   stored_override=None, effort_source=None,
                                   favorites=[], recent=[], on_favorites=lambda _r: None,
                                   on_refresh=refresh)
        app.push_screen(screen)
        await pilot.pause()
        assert [r["id"] for r in screen._visible_rows if r] == ["old"]
        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.pause()
        assert calls == [1]
        assert sorted(r["id"] for r in screen._visible_rows if r) == ["new", "old"]
        assert app.screen is screen


@pytest.mark.asyncio
async def test_search_is_fuzzy_ranks_best_first_and_highlights_matches():
    from textual.widgets import Input, OptionList

    transport = ModelPickerTransport()
    transport.models = [
        {"provider": "openai", "id": "gpt-6-luna", "name": "GPT-6 Luna"},
        {"provider": "anthropic", "id": "claude-opus-5-5", "name": "Claude Opus 5.5"},
        {"provider": "anthropic", "id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5"},
    ]
    app = NexusTextualApp(Client(transport), session="fuzzy")
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        picker = app.screen
        picker.query_one("#model-picker-search", Input).value = "cop55"
        await pilot.pause()
        options = picker.query_one(OptionList)
        prompts = [options.get_option_at_index(i).prompt for i in range(options.option_count)]
        assert str(prompts[0]).startswith("Best matches · ")
        assert "Claude Opus 5.5" in str(prompts[1]) and "GPT-6" not in " ".join(map(str, prompts))
        # Matched letters are styled in the label.
        assert any("underline" in str(span.style) for span in prompts[1].spans)
        picker.query_one("#model-picker-search", Input).value = "zzz"
        await pilot.pause()
        assert str(options.get_option_at_index(0).prompt) == "No matching models"
