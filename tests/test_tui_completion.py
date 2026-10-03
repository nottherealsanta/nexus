"""Inline command and file-token completion in the TUI composer."""

from __future__ import annotations

import asyncio

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.host import protocol as p
from nexus.ui.cli import commands
from nexus.ui.cli.client import ClientError
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.widgets import ChatEditor, CompletionPopup


def _popup_names(popup) -> list[str]:
    """Command names from the popup rows (each row also shows a summary)."""
    return [str(option.prompt.plain).split()[0] for option in popup.options if option.prompt.plain.strip()]


def _popup_text(popup) -> str:
    return "\n".join(option.prompt.plain for option in popup.options)

@pytest.mark.asyncio
async def test_slash_completion_uses_registry_and_accepts_token_without_sending():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "please /mo"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause()

        popup = app.query_one(CompletionPopup)
        assert popup.display
        assert "/model" in _popup_text(popup)
        assert popup.region.y + popup.region.height <= editor.region.y
        assert popup.region.x == editor.region.x
        assert popup.region.right == editor.region.right
        # Taui-style list uses full width rows with no accent fill.
        assert popup.styles.outline_left[0] != "outer"
        assert popup.styles.padding.left == 1
        selected = popup.get_option_at_index(0).prompt.plain
        assert selected.lstrip().startswith("/model")
        await pilot.press("tab")
        await pilot.pause()

        assert editor.text == "please /model"
        assert not popup.display
        assert app.focused is editor
        assert "start_turn" not in transport.trace


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/s", "/n", "/N"])
async def test_slash_prefix_suggests_every_command_starting_with_prefix(prefix):
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = prefix
        editor.move_cursor((0, len(editor.text)))
        composer = app.query_one("#chat-input")
        composer.refresh_completion()
        await pilot.pause()

        expected = sorted(
            spec.name
            for spec in commands.SPECS
            if not spec.hidden and spec.name.casefold().startswith(prefix.casefold())
        )
        assert composer._completion_items == expected
        assert _popup_names(app.query_one(CompletionPopup)) == expected


@pytest.mark.asyncio
async def test_typing_slash_n_then_enter_selects_new_session_command():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        await pilot.press("/", "n")
        await pilot.pause()

        popup = app.query_one(CompletionPopup)
        assert editor.text == "/n"
        assert app.query_one("#chat-input")._completion_items == ["/new"]
        assert popup.display
        assert _popup_names(popup) == ["/new"]
        # The completion is painted in the visible row immediately above the
        # editor; matching the item alone would not catch a clipped popup.
        assert popup.region.height == 3  # one row plus the top and bottom rules
        assert 0 <= popup.region.y
        assert popup.region.y + popup.region.height <= editor.region.y
        assert popup.region.y + popup.region.height <= app.size.height
        assert app.focused is editor

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert app.controller.session != "s"
        assert app.controller.session.startswith("session-")
        assert not popup.display
        assert "start_turn" not in transport.trace


@pytest.mark.asyncio
async def test_partial_slash_completion_enter_executes_selected_command():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "/mo"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause()

        assert app.query_one(CompletionPopup).display
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert editor.text == ""
        assert not app.query_one(CompletionPopup).display
        assert "start_turn" not in transport.trace
        assert "ModelsList" in transport.trace
        from nexus.ui_support.tui_model_picker import ModelPickerScreen
        assert isinstance(app.screen, ModelPickerScreen)


@pytest.mark.asyncio
async def test_model_arguments_do_not_open_a_second_completion_list():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "/model "
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause()
        assert not app.query_one(CompletionPopup).display
        await pilot.press("enter")
        await pilot.pause(0.1)
        from nexus.ui_support.tui_model_picker import ModelPickerScreen
        assert isinstance(app.screen, ModelPickerScreen)


@pytest.mark.asyncio
async def test_empty_slash_token_suggests_registry_but_only_at_cursor():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "/"
        editor.move_cursor((0, 1))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        assert popup.display
        assert popup.option_count == len([spec for spec in commands.SPECS if not spec.hidden])
        assert "/new" in _popup_text(popup)
        assert popup.region.height == 8
        assert popup.region.right <= app.size.width

        editor.move_cursor((0, 0))
        app.query_one("#chat-input").refresh_completion()
        assert not app.query_one(CompletionPopup).display


@pytest.mark.asyncio
async def test_unmatched_slash_query_hides_previous_completion_list():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        popup = app.query_one(CompletionPopup)
        composer = app.query_one("#chat-input")

        editor.text = "/mo"
        editor.move_cursor((0, len(editor.text)))
        composer.refresh_completion()
        await pilot.pause()
        assert popup.display
        assert "/model" in _popup_text(popup)

        editor.text = "/no-such-command"
        editor.move_cursor((0, len(editor.text)))
        composer.refresh_completion()
        await pilot.pause()
        assert not popup.display


@pytest.mark.asyncio
async def test_slash_enter_executes_selected_command_instead_of_inserting_it():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(48, 18)) as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        for key in "/":
            await pilot.press(key)
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        assert popup.display
        assert popup.option_count == len([spec for spec in commands.SPECS if not spec.hidden])
        assert popup.region.y + popup.region.height <= editor.region.y

        # Navigation follows alphabetical command order through a scrolled list.
        for _ in range(app.query_one("#chat-input")._completion_items.index("/details")):
            await pilot.press("down")
        assert app.query_one("#chat-input").selected_completion == "/details"
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert editor.text == ""
        assert "start_turn" not in transport.trace
        assert "session s" in app.query_one("#connection-status").render().plain


@pytest.mark.asyncio
async def test_picker_typeahead_filters_without_search_widget_and_backspace_restores_rows():
    from nexus.ui.tui.agent_picker import AgentPickerPanel

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(42, 18)) as pilot:
        await pilot.pause()
        rows = [
            {"name": f"model-{index}", "_value": f"p/model-{index}", "_label": f"Model {index}"}
            for index in range(12)
        ]
        app._show_inline_picker("model", rows)
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        options = panel.query_one("#agent-options")
        editor = app.query_one(ChatEditor)
        assert app.focused is options
        assert not app.query("#agent-search")
        assert panel.region.y + panel.region.height <= editor.region.y
        assert panel.region.right <= app.size.width
        assert options.region.height == 8
        assert options.option_count == 12

        await pilot.press("m", "o", "d", "e", "l", "-", "1")
        await pilot.pause()
        assert options.option_count == 3
        assert [row["name"] for row in panel._visible_agents] == [
            "model-1", "model-10", "model-11"
        ]
        await pilot.press("backspace")
        await pilot.pause()
        assert options.option_count == 12
        assert options.region.height == 8
        assert app.focused is options
        assert editor.text == ""


@pytest.mark.asyncio
async def test_inline_picker_tab_accepts_highlighted_item():
    from nexus.ui.tui.agent_picker import AgentPickerPanel

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._show_inline_picker(
            "agent", [{"name": "general", "contexts": ["root"]}], current="general"
        )
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert not panel.display


@pytest.mark.asyncio
async def test_unmatched_slash_enter_submits_as_free_text():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        for key in "/nothing":
            await pilot.press(key)
        await pilot.pause()
        assert not app.query_one(CompletionPopup).display
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert transport.last_input == "/nothing"


@pytest.mark.asyncio
async def test_file_completion_handles_nested_tokens_and_is_literal_on_accept():
    transport = FakeTransport()
    client = _client(transport)
    searched: list[tuple[str, int]] = []

    async def search_files(query: str, limit: int = 30) -> list[str]:
        searched.append((query, limit))
        return ["workspace/src/main.py", "workspace/src/map.py"]

    client.search_files = search_files
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "read @workspace/src/ma"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause(0.2)

        assert searched == [("workspace/src/ma", 30)]
        assert "@workspace/src/main.py" in _popup_text(app.query_one(CompletionPopup))
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        assert editor.text == "read @workspace/src/map.py"
        assert app.focused is editor
        assert "start_turn" not in transport.trace
        assert not app.query_one(CompletionPopup).display


@pytest.mark.asyncio
async def test_active_completion_navigation_selects_suggestions_without_moving_editor():
    client = _client(FakeTransport())
    queries: list[str] = []

    async def search_files(query: str, limit: int = 30) -> list[str]:
        queries.append(query)
        return ["workspace/first file.txt", "workspace/second.txt"]

    client.search_files = search_files
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "include @workspace/"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause(0.2)

        assert queries == ["workspace/"]
        assert app.focused is editor
        original_cursor = editor.cursor_location
        popup = app.query_one(CompletionPopup)
        assert popup.display
        assert popup.region.y + popup.region.height <= editor.region.y

        await pilot.press("down")
        await pilot.pause()

        assert app.focused is editor
        assert editor.cursor_location == original_cursor
        assert app.query_one("#chat-input")._completion_selected == 1
        await pilot.press("enter")
        await pilot.pause()

        assert editor.text == "include @workspace/second.txt"
        assert app.focused is editor
        assert not popup.display


@pytest.mark.asyncio
async def test_file_completion_uses_client_file_search_protocol_end_to_end():
    transport = FakeTransport()
    requests: list[tuple[str, int]] = []
    request = transport.request

    async def request_with_file_search(command):
        if isinstance(command, p.FileSearch):
            requests.append((command.query, command.limit))
            return p.FileSearchResult(paths=["workspace/docs/guide.md"])
        return await request(command)

    transport.request = request_with_file_search
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "read @workspace/docs/gui"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause(0.2)

        popup = app.query_one(CompletionPopup)
        assert requests == [("workspace/docs/gui", 30)]
        assert popup.display
        assert "@workspace/docs/guide.md" in _popup_text(popup)
        assert app.focused is editor

        await pilot.press("tab")
        await pilot.pause()

        assert editor.text == "read @workspace/docs/guide.md"
        assert not popup.display
        assert "start_turn" not in transport.trace


@pytest.mark.asyncio
async def test_empty_file_token_queries_workspace_and_shift_enter_still_newlines():
    client = _client(FakeTransport())
    queries: list[str] = []

    async def search_files(query: str, limit: int = 30) -> list[str]:
        queries.append(query)
        return ["workspace/readme.md"]

    client.search_files = search_files
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "@"
        editor.move_cursor((0, 1))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause(0.2)
        assert queries == [""]
        assert app.query_one(CompletionPopup).display

        await pilot.press("shift+enter")
        await pilot.pause()
        assert editor.text == "@\n"


@pytest.mark.asyncio
async def test_file_completion_narrows_in_place_while_typing():
    client = _client(FakeTransport())
    release = asyncio.Event()

    async def search_files(query: str, limit: int = 30) -> list[str]:
        if query:
            await release.wait()
        return ["src/app.py", "src/main.py", "readme.md"]

    client.search_files = search_files
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        chat_input = app.query_one("#chat-input")
        editor.text = "@"
        editor.move_cursor((0, 1))
        chat_input.refresh_completion()
        await pilot.pause(0.2)
        await pilot.press("down")
        assert chat_input._completion_items[chat_input._completion_selected] == "@src/main.py"

        editor.text = "@src"
        editor.move_cursor((0, len(editor.text)))
        chat_input.refresh_completion()
        await pilot.pause()

        # The search for "src" is still pending: the list stays up, narrowed,
        # and the highlighted path is kept.
        popup = app.query_one(CompletionPopup)
        assert popup.display
        assert chat_input._completion_items == ["@src/app.py", "@src/main.py"]
        assert chat_input._completion_items[chat_input._completion_selected] == "@src/main.py"
        release.set()


@pytest.mark.asyncio
async def test_file_completion_client_failure_hides_suggestions_and_preserves_draft():
    client = _client(FakeTransport())
    searched: list[str] = []

    async def search_files(query: str, limit: int = 30) -> list[str]:
        searched.append(query)
        raise ClientError("temporary search failure")

    client.search_files = search_files
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "read @workspace/src/ma"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause(0.2)

        assert searched == ["workspace/src/ma"]
        assert editor.text == "read @workspace/src/ma"
        assert not app.query_one(CompletionPopup).display
        assert not app.query_one("#chat-input").completion_visible


@pytest.mark.asyncio
async def test_stale_file_search_cannot_replace_newer_token_suggestions():
    transport = FakeTransport()
    client = _client(transport)
    started = asyncio.Event()
    release = asyncio.Event()

    async def search_files(query: str, limit: int = 30) -> list[str]:
        if query == "old":
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Model a search implementation that completes despite cancel.
                await release.wait()
            return ["workspace/old.txt"]
        return ["workspace/new.txt"]

    client.search_files = search_files
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "@old"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await asyncio.wait_for(started.wait(), 1)

        editor.text = "@new"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause(0.2)
        release.set()
        await pilot.pause()

        visible = _popup_text(app.query_one(CompletionPopup))
        assert "workspace/new.txt" in visible
        assert "workspace/old.txt" not in visible
        assert "start_turn" not in transport.trace


@pytest.mark.asyncio
async def test_missing_search_method_and_escape_leave_normal_enter_behavior():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "@local"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause()
        assert not app.query_one(CompletionPopup).display

        editor.text = "/mo"
        editor.move_cursor((0, len(editor.text)))
        app.query_one("#chat-input").refresh_completion()
        await pilot.pause()
        assert app.query_one(CompletionPopup).display
        await pilot.press("escape")
        assert not app.query_one(CompletionPopup).display

        editor.text = "ordinary message"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert transport.last_input == "ordinary message"
