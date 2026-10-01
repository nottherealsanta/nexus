"""Textual shell. All runtime/session work is delegated to the host Client."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from importlib.resources import files
from pathlib import Path
from typing import ClassVar

from textual import constants, events, on
from textual.app import App, ComposeResult
from textual.command import Provider
from textual.widgets import Button, Input, OptionList, Static, TextArea

from ...client.protocol import Client, ClientError
from ...events import Event
from ...host import protocol as p
from ...ui_support.context import context_measure, thinking_status
from ...ui_support.tui_command_palette import (
    KEYBOARD_SHORTCUTS,
    SHORTCUTS,
    ChatCommandProvider,
    ShortcutsCommandProvider,
    ShortcutsScreen,
    model_display_name,
    model_reference,
)
from ...ui_support.tui_context_header import ContextBlock, ContextHeader, ContextModal
from ...ui_support.tui_model_picker import ModelPickerScreen
from ...ui_support.tui_panels import DetailsSidebar, SessionSidebar, TuiPreferences
from ...ui_support.tui_setup import block_unconfigured_turn, open_first_run_setup
from ...ui_support.tui_voice import VoiceController
from ..cli import commands
from ..cli.details import detail_lines
from ..cli.render import sanitize
from .agent_picker import AgentPicker, AgentPickerPanel, _display_name
from .agent_transcript import AgentTranscriptScreen
from .controller import TuiController
from .extras import ExtraCommandsMixin
from .keys import NexusDriver
from .messages import (
    AgentOpenRequested,
    AgentPickerRequested,
    CancelRequested,
    EventReceived,
    InputSubmitted,
    PermissionRequested,
    StreamDisconnected,
    TurnFinished,
)
from .keychord import LeaderKeys
from .attachments import AttachmentsMixin
from .new_session import apply_agent_choice, open_new_session_picker
from .panels import MainLayout, PanelsMixin, TopBar
from .permission import ListPrompt, PermissionScreen, ask_pending_question
from .theme import NEXUS_THEMES
from .timeline import ConversationTimeline
from .widgets import (
    ActivityProgress,
    ChatInput,
    ConnectionStatus,
    ContextDetailsScreen,
    ContextPreview,
    LogsDrawer,
    RootAgentBar,
    WorktreesScreen,
    context_detail_usage,
    context_usage,
)


class NexusTextualApp(AttachmentsMixin, ExtraCommandsMixin, PanelsMixin, App[int]):
    """Full-screen interactive client of the existing Nexus daemon protocol."""

    TITLE = "Nexus"
    COMMANDS: ClassVar[set[Callable[[], type[Provider]]]] = {
        lambda: ChatCommandProvider,
        lambda: ShortcutsCommandProvider,
    }
    ENABLE_COMMAND_PALETTE = True
    SHORTCUT_TABLE = SHORTCUTS
    LOGS_POLL_INTERVAL = 1.0
    CSS = files(__package__).joinpath("app.tcss").read_text(encoding="utf-8")
    #: Derived from :data:`SHORTCUTS`, the single source for the key reference.
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        (key, action, description)
        for key, action, description in SHORTCUTS
        if action
    ]

    def __init__(
        self,
        client: Client,
        *,
        session: str = "default",
        reconnect: Callable[[], Awaitable[Client]] | None = None,
        preferences_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.prefs = TuiPreferences(preferences_path)
        self._health: dict | None = None
        self._health_error: str | None = None
        self._last_archive: tuple[str, str] | None = None
        self.controller = TuiController(client, session)
        self.controller.on_stream_end = self._sync_activity
        self._agents: list[dict] = []
        self._reconnect_factory = reconnect
        self._pending_permission_id: str | None = None
        self._permission_result: asyncio.Future[str | None] | None = None
        self._reasoning_effort_in_flight = False
        self.voice = VoiceController(self)
        self.leader = LeaderKeys(self)
        self._inline_picker_kind: str | None = None
        self._picker_restore_focus = None
        self._status_error = False
        self._status_text = ""
        self._update_notice = ""
        self._last_ctrl_c = 0.0
        self._logs_open = False
        self._logs_generation = 0
        self._logs_daemon_cursor: str | None = None
        self._logs_session_cursor: int | None = None
        self._logs_poll_task: asyncio.Task | None = None
        self._logs_restore_focus = None
        self._logs_saved_scroll_y: int | None = None
        self._context_preview_generation = 0
        self._context_preview_task: asyncio.Task[None] | None = None
        self._context_preview_session: str | None = None
        self._context_preview: p.ContextInspectResult | None = None
        self._context_preview_error: str | None = None
        self._context_preview_loading = False
        for theme in NEXUS_THEMES:
            self.register_theme(theme)
        names = {theme.name for theme in NEXUS_THEMES}
        self.theme = self.prefs["theme"] if self.prefs["theme"] in names else NEXUS_THEMES[0].name

    def get_driver_class(self):
        """Install the terminal key-protocol driver on POSIX terminals.

        The driver adds xterm ``modifyOtherKeys`` decoding on top of Textual's
        built-in Kitty CSI-u negotiation (``TEXTUAL_DISABLE_KITTY_KEY`` still
        applies through it). An explicit ``TEXTUAL_DRIVER`` is honored instead;
        Windows keeps Textual's own driver.
        """
        if NexusDriver is not None and constants.DRIVER is None:
            return NexusDriver
        return super().get_driver_class()

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical

        yield TopBar(id="top-bar")
        with MainLayout(id="main-layout"):
            yield SessionSidebar(id="session-sidebar")
            with Vertical(id="main-column"):
                yield ContextPreview(id="context-preview")
                yield ConversationTimeline(id="conversation")
                yield ConnectionStatus("Connecting to Nexus daemon…", id="connection-status")
                yield AgentPickerPanel(id="inline-picker")
                yield ChatInput(id="chat-input")
                yield Static("", id="leader-hint", markup=False)
                yield ActivityProgress(id="activity-progress")
            yield DetailsSidebar(id="details-sidebar")
            yield LogsDrawer(id="logs-drawer")

    async def on_mount(self) -> None:
        self._sync_panels()
        try:
            await self.controller.bootstrap()
            self._agents = await self.controller.client.list_agents()
            self._sync_agent_colors()
            self._sync_status("")
            self._sync_agent()
            await self._sync_timeline()
            self.call_after_refresh(self.query_one("#chat-editor", TextArea).focus)
            await ask_pending_question(self)
            self._setup_required = await open_first_run_setup(self, self.controller.client)
            self._start_context_preview()
            self._sync_panels()
            self.set_interval(2.0, self._poll_sessions)
            self.set_interval(30.0, self._poll_health)
            self.set_interval(1.0, self.voice.refresh_status)
            self.run_worker(self._poll_sessions(), group="sessions")
            self.run_worker(self._poll_health(), group="health")
            await self.voice.refresh_status()
        except ClientError as exc:
            self._sync_status(f"Disconnected · {exc}", error=True)

    async def on_event(self, event) -> None:
        if isinstance(event, events.Key) and not event.is_forwarded:
            if await self.leader.intercept_navigation(event):
                return
            if self.leader.intercept(event):
                return
        await super().on_event(event)

    async def on_key(self, event) -> None:
        if event.key in {"ctrl+space", "ctrl+@", "ctrl+nul", "ctrl+0"} and self._is_main_screen():
            event.stop()
            event.prevent_default()
            self.run_worker(self.voice.toggle(), group="voice", exclusive=True)
        elif event.key == "escape" and self.voice.transcribing:
            event.stop()
            event.prevent_default()
            self.run_worker(self.voice.cancel(), group="voice", exclusive=True)
        elif event.key == "ctrl+e" and self._is_main_screen():
            event.stop()
            event.prevent_default()
            self.action_toggle_logs()
        elif event.key == "escape" and self._logs_open and self._is_main_screen():
            event.stop()
            event.prevent_default()
            self.action_close_logs()
        elif event.key == "escape" and isinstance(self.screen, AgentTranscriptScreen):
            event.stop()
            self.action_back_from_agent()
        elif event.key == "shift+tab" and isinstance(self.focused, TextArea):
            # TextArea normally owns Tab for indentation; this global shortcut
            # intentionally selects the daemon-owned root agent instead.
            event.stop()
            event.prevent_default()
            asyncio.create_task(self._cycle_root_agent())
        elif event.key == "ctrl+t" and isinstance(self.focused, TextArea) and self._is_main_screen():
            event.stop()
            event.prevent_default()
            self.run_worker(self.action_cycle_reasoning_effort(), group="reasoning-effort")
        elif (
            event.key.lower() == "a"
            and not isinstance(self.focused, (TextArea, Input))
            and not isinstance(self.screen, AgentPicker)
        ):
            event.stop()
            self.post_message(AgentPickerRequested())

    def _is_main_screen(self) -> bool:
        from .agent_picker import AgentPicker
        return self.screen is self.screen_stack[0] and not isinstance(
            self.screen, (AgentPicker, ListPrompt)
        )

    def action_toggle_logs(self) -> None:
        if not self._is_main_screen():
            return
        if self._logs_open:
            self.action_close_logs()
            return
        self._logs_open = True
        self._logs_generation += 1
        self._logs_daemon_cursor = None
        self._logs_session_cursor = None
        drawer = self.query_one(LogsDrawer)
        drawer.reset_poll_state()
        drawer.set_session(self.controller.session)
        self._logs_restore_focus = self.focused
        timeline = self.query_one("#conversation", ConversationTimeline)
        self._logs_saved_scroll_y = timeline.scroll_offset.y
        drawer.display = True
        self._sync_panels()
        self._sync_logs_layout()
        self._sync_main_width()
        self._start_logs_polling()

    def action_close_logs(self) -> None:
        if not self._logs_open:
            return
        self._logs_open = False
        self._logs_generation += 1
        self._cancel_logs_polling()
        self.query_one(LogsDrawer).display = False
        self._sync_panels()
        self._sync_logs_layout()
        self._sync_main_width()
        self._restore_logs_scroll()
        target = self._logs_restore_focus
        self._logs_restore_focus = None
        if target is not None and target.is_mounted:
            target.focus()

    def _sync_logs_layout(self) -> None:
        if not self.is_mounted:
            return
        width = self.size.width
        if width < 72:
            drawer_width = max(1, min(32, width // 2))
            self.query_one("#logs-drawer", LogsDrawer).styles.width = drawer_width
            self.query_one("#logs-drawer", LogsDrawer).styles.height = "1fr"
            self.query_one("#logs-drawer", LogsDrawer).styles.dock = "right"
        else:
            self.query_one("#logs-drawer", LogsDrawer).styles.width = min(48, max(30, width // 3))
            self.query_one("#logs-drawer", LogsDrawer).styles.height = "1fr"
            self.query_one("#logs-drawer", LogsDrawer).styles.dock = "right"

    def _sync_main_width(self) -> None:
        if not self.is_mounted:
            return
        # Side panels share the row, so the chat column always takes the rest.
        self.query_one("#main-column").styles.width = "1fr"

    def _restore_logs_scroll(self) -> None:
        if self._logs_saved_scroll_y is None or not self.is_mounted:
            return
        y = self._logs_saved_scroll_y
        self._logs_saved_scroll_y = None
        timeline = self.query_one("#conversation", ConversationTimeline)
        self.call_after_refresh(lambda: timeline.scroll_to(y=y, animate=False))

    def _start_logs_polling(self) -> None:
        if not self._logs_open or not self.is_mounted:
            return
        if self._logs_poll_task is None or self._logs_poll_task.done():
            generation = self._logs_generation
            self._logs_poll_task = asyncio.create_task(self._poll_logs(generation))

    def _cancel_logs_polling(self) -> None:
        task = self._logs_poll_task
        self._logs_poll_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _poll_logs(self, generation: int) -> None:
        task = asyncio.current_task()
        try:
            while (
                generation == self._logs_generation
                and self._logs_open
                and self.is_mounted
            ):
                session = self.controller.session
                try:
                    reader = getattr(self.controller.client, "logs_read", None)
                    if not callable(reader):
                        reader = self.controller.client.read_logs
                    result = await reader(
                        session=session,
                        daemon_cursor=self._logs_daemon_cursor,
                        session_cursor=self._logs_session_cursor,
                        limit=50,
                    )
                    if (
                        generation != self._logs_generation
                        or session != self.controller.session
                        or not self._logs_open
                        or not self.is_mounted
                    ):
                        return
                    self._logs_daemon_cursor = result.daemon.next_cursor
                    self._logs_session_cursor = result.session.next_cursor
                    self.query_one(LogsDrawer).add_page(result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - display transport failures in drawer
                    if (
                        generation == self._logs_generation
                        and session == self.controller.session
                        and self._logs_open
                        and self.is_mounted
                    ):
                        self.query_one(LogsDrawer).set_error(str(exc))
                await asyncio.sleep(self.LOGS_POLL_INTERVAL)
        finally:
            if self._logs_poll_task is task:
                self._logs_poll_task = None

    async def on_resize(self, event) -> None:
        self._sync_panels()
        self._sync_logs_layout()
        self._sync_main_width()
        if self._logs_open and self._logs_saved_scroll_y is not None:
            self.call_after_refresh(
                lambda: self.query_one("#conversation", ConversationTimeline).scroll_to(
                    y=self._logs_saved_scroll_y or 0, animate=False
                )
            )

    async def action_open_model_picker(self) -> None:
        if self._is_main_screen():
            await self._push_model_picker()

    async def action_start_voice(self) -> None:
        if self._is_main_screen() and not self.voice.recording:
            await self.voice.start_or_confirm()

    def action_show_shortcuts(self) -> None:
        self.push_screen(ShortcutsScreen(KEYBOARD_SHORTCUTS))

    def action_toggle_voice(self) -> None:
        self.run_worker(self.voice.toggle(), group="voice", exclusive=True)

    @on(Button.Pressed, "#logs-close")
    def _logs_close_pressed(self, _: Button.Pressed) -> None:
        self.action_close_logs()

    @on(InputSubmitted)
    async def _input_submitted(self, message: InputSubmitted) -> None:
        stripped = message.content.strip()
        parsed = commands.parse(stripped) if stripped.startswith("/") else None
        if parsed is not None and parsed.name in commands.BY_NAME:
            await self._dispatch_chat_command(stripped)
            return
        if block_unconfigured_turn(self, message.content): return
        if await self.send_attachments(message):
            return
        if self.controller.running or self.controller.view.active_turn is not None:
            try:
                await self.controller.client.enqueue(self.controller.session, message.content, mode=message.mode)
                self._sync_status({"queue": "Message queued", "steer": "Steering queued", "interrupt": "Interrupt requested"}[message.mode])
                if not self.controller.running:
                    self.controller.resume(self._post_event)
            except ClientError as exc:
                self.query_one("#chat-editor", TextArea).text = message.content
                self._sync_status(str(exc), error=True)
            return
        try:
            self._sync_status("Turn running · Ctrl+C cancels")
            self.controller.start_turn(message.content, self._post_event)
        except ClientError as exc:
            self._sync_status(f"Disconnected · {exc}", error=True)

    async def _dispatch_chat_command(self, raw: str) -> None:
        parsed = commands.parse(raw)
        if parsed is None:
            return
        args = parsed.args
        try:
            if parsed.name == "/attach":
                await self.attach_file(raw)
            elif parsed.name == "/help":
                await self._show_notice(commands.help_text())
            elif parsed.name == "/exit":
                await self.action_quit_shell()
            elif parsed.name == "/cancel":
                await self.action_cancel_turn()
            elif parsed.name == "/reconnect":
                await self.action_reconnect()
            elif parsed.name == "/new":
                await open_new_session_picker(self, args[0] if args else f"session-{uuid.uuid4().hex[:8]}")
            elif parsed.name == "/hotkeys":
                self.push_screen(ShortcutsScreen(KEYBOARD_SHORTCUTS))
            elif parsed.name == "/theme":
                requested = args[0].casefold() if args else ("light" if self.theme == "nexus-dark" else "dark")
                theme = next((item for item in NEXUS_THEMES if item.name.endswith(requested)), None)
                if theme is None:
                    await self._show_notice("Use /theme dark or /theme light")
                else:
                    self.theme = theme.name
                    self.prefs.set("theme", theme.name)
            elif parsed.name == "/settings":
                self.action_open_settings()
            elif parsed.name == "/verbose":
                await self._show_notice("Tool details open in a modal when selected")
            elif parsed.name in {"/mcp", "/skills"}:
                block = self.query_one("#context-mcp" if parsed.name == "/mcp" else "#context-skills", ContextBlock)
                self.push_screen(ContextModal(block.label, block.detail, category="mcp" if parsed.name == "/mcp" else "skills"))
            elif parsed.name in {"/copy", "/cost", "/diff", "/tasks", "/reload", "/review", "/commit", "/mock"}:
                await self._dispatch_extra_command(parsed.name, args)
            elif parsed.name == "/sessions":
                summaries = await self.controller.client.list_sessions()
                if args:
                    matched = next((row for row in summaries if row.id == args[0] or row.id.startswith(args[0])), None)
                    if matched is None:
                        await self._show_notice(f"No session matching {sanitize(args[0], 80)}")
                    else:
                        await self._switch_session(matched.id)
                else:
                    self._open_sessions_dialog()
            elif parsed.name == "/archived":
                self._open_archived_dialog()
            elif parsed.name == "/effort":
                await self._effort_command(args)
            elif parsed.name == "/model":
                if args:
                    if args[0] == "list":
                        models = await self.controller.client.list_models(
                            selectable_only=True,
                            search=args[1] if len(args) > 1 else None,
                        )
                        await self._show_notice("\n".join(
                            f"{model_display_name(row)} "
                            f"[{model_reference(row) or '?'}] "
                            f"[{row.get('tier', '')}]"
                            for row in models
                        ))
                    else:
                        await self._model_command(args)
                else:
                    await self._push_model_picker()
            elif parsed.name == "/agent":
                if args:
                    await self._agent_command(args)
                else:
                    await self._agent_command(("list",))
            elif parsed.name == "/tools":
                await self._show_notice("\n".join(
                    f"{tool.name} [{tool.status}]" for tool in self.controller.view.tools
                ) or "No tools used in this transcript")
            elif parsed.name == "/details":
                await self._show_notice("\n".join(detail_lines(self.controller.session, self.controller.view)))
            elif parsed.name == "/voice":
                await self.voice.command(args)
            elif parsed.name == "/context":
                await self.action_open_context()
            elif parsed.name == "/worktrees":
                await self._open_worktrees()
            elif parsed.name == "/fork":
                at_seq = int(args[0]) if args and args[0].isdigit() else None
                summary = await self.controller.client.fork(self.controller.session, at_seq)
                await self._switch_session(summary.id)
            elif parsed.name == "/export":
                fmt = args[0] if args else "markdown"
                content = await self.controller.client.export(self.controller.session, format=fmt)
                await self._show_notice(content)
            else:
                await self._show_notice(f"Unknown chat command: {sanitize(parsed.name, 64)}")
        except ClientError as exc:
            self._sync_status(f"Command failed · {exc}", error=True)

    async def _open_worktrees(self) -> None:
        if not self._is_main_screen():
            return
        self.push_screen(WorktreesScreen(self.controller.client))


    async def _show_notice(self, text: str) -> None:
        # Command feedback is chrome, never synthetic conversation history.
        self._sync_status(sanitize(text, 240))

    async def _switch_session(self, session: str) -> None:
        await self.voice.cancel()
        self.clear_attachments()
        self._context_preview_generation += 1
        preview = self.query_one("#context-preview", ContextPreview)
        preview.display = False
        self._context_preview_session = None
        self._context_preview = None
        self._context_preview_error = None
        self._context_preview_loading = False
        self.query_one(ContextHeader).set_unavailable()
        self._logs_generation += 1
        self._cancel_logs_polling()
        self._logs_session_cursor = None
        await self.controller.switch_session(session)
        self.query_one(LogsDrawer).set_session(session)
        await self.controller.bootstrap()
        if session != self.controller.session:
            return
        await self._sync_timeline()
        self._sync_agent()
        # Session identity belongs in the picker, not a transient status line;
        # keep the status widget mounted as the existing spacer row.
        self._sync_status("")
        self._start_context_preview()
        await self.voice.refresh_status(force=True)
        if self._logs_open:
            self._start_logs_polling()

    async def _model_command(self, args: tuple[str, ...]) -> None:
        await self.controller.select_model(args[0])
        self._remember_model(args[0])
        self._sync_agent()
        self._refresh_context_after_selection(self.controller.session)
        self._sync_status("")

    async def action_cycle_reasoning_effort(self) -> None:
        """Cycle the host-supported reasoning effort for the root session."""
        editor = self.query_one("#chat-editor", TextArea)
        if self.focused is not editor or self.screen is not self.screen_stack[0]:
            return
        if self._reasoning_effort_in_flight:
            return

        session = self.controller.session
        metadata_revision = self.controller._agent_metadata_revision
        self._reasoning_effort_in_flight = True
        try:
            current = await self.controller.client.current_agent(session)
            if (
                session != self.controller.session
                or metadata_revision != self.controller._agent_metadata_revision
            ):
                return
            levels = list(getattr(current, "supported_levels", ()) or ())
            if not levels:
                return

            effective = getattr(current, "reasoning_effort", None)
            try:
                index = levels.index(effective)
            except ValueError:
                next_effort = levels[0]
            else:
                next_effort = levels[(index + 1) % len(levels)]

            await self.controller.client.select_reasoning_effort(session, next_effort)
            if (
                session != self.controller.session
                or metadata_revision != self.controller._agent_metadata_revision
            ):
                return
            await self.controller.refresh_agent_metadata()
            if (
                session != self.controller.session
                or metadata_revision != self.controller._agent_metadata_revision
            ):
                return
            self._sync_agent()
            self._sync_status("")
        except ClientError as exc:
            if (
                session == self.controller.session
                and metadata_revision == self.controller._agent_metadata_revision
            ):
                self._sync_status(f"Reasoning effort selection failed · {exc}", error=True)
        finally:
            self._reasoning_effort_in_flight = False

    async def _push_model_picker(self) -> None:
        models = await self.controller.client.list_models(selectable_only=True)
        current_ref = f"{self.controller.provider}/{self.controller.model}" if self.controller.provider and self.controller.model else ""
        session = self.controller.session
        def selected(choice: tuple[str, str | None, bool] | None) -> None:
            if choice is not None and session == self.controller.session:
                self.run_worker(self._apply_model_selection(choice[0], choice[1],
                                commit_effort=choice[2]), group="model-selection")
        self.push_screen(ModelPickerScreen(
            [row for row in models if model_reference(row)], current=current_ref,
            current_effort=self.controller.reasoning_effort,
            stored_override=self.controller.stored_override,
            effort_source=self.controller.reasoning_effort_source,
            favorites=self.prefs["model_favorites"], recent=self.prefs["model_recent"],
            on_favorites=lambda refs: self.prefs.set("model_favorites", refs),
            on_refresh=self._refresh_model_catalogue,
        ), callback=selected)

    async def _refresh_model_catalogue(self) -> list[dict]:
        await self.controller.client.refresh_models()
        models = await self.controller.client.list_models(selectable_only=True)
        return [row for row in models if model_reference(row)]

    def _remember_model(self, ref: str) -> None:
        self.prefs.set("model_recent", [ref, *(value for value in self.prefs["model_recent"]
                                           if value != ref)][:20])

    async def _open_picker(self, kind: str) -> None:
        if kind == "agent":
            await self._agent_picker_requested(AgentPickerRequested())
        elif kind == "model":
            await self._push_model_picker()
        elif kind == "effort":
            await self._open_effort_picker()

    async def _agent_command(self, args: tuple[str, ...]) -> None:
        action = args[0] if args else "list"
        if action == "list":
            self.post_message(AgentPickerRequested())
            return
        if action == "current":
            result = await self.controller.client.current_agent(self.controller.session)
            await self._show_notice(f"{_display_name(result.name)} ({result.source})")
            return
        if action == "reset":
            await self.controller.reset_agent()
        else:
            await self.controller.select_agent(action)
        self._sync_agent()
        self._refresh_context_after_selection(self.controller.session)
        self._sync_status("")

    def _post_event(self, event: Event | None) -> asyncio.Future[None] | None:
        if event is None:
            self.post_message(StreamDisconnected())
            return None
        handled = asyncio.get_running_loop().create_future()
        self.post_message(EventReceived(event, handled))
        return handled

    @on(EventReceived)
    async def _event_received(self, message: EventReceived) -> None:
        event = message.event
        changed, _ = self.controller.ingest(event)
        if event.type == "error":
            detail = event.data.get("message") or event.data.get("error") or "Session error"
            self._sync_status(f"Error · {sanitize(detail, 180)}", error=True)
        if event.type == "permission.requested":
            asyncio.create_task(
                self._permission_requested(
                    PermissionRequested(dict(event.data)), message.handled
                )
            )
            if message.handled is not None and not message.handled.done():
                message.handled.set_result(None)
            return
        elif event.type == "permission.resolved":
            if event.data.get("id") == self._pending_permission_id:
                self._pending_permission_id = None
                if self._permission_result is not None and not self._permission_result.done():
                    self._permission_result.set_result(None)
                if isinstance(self.screen, PermissionScreen):
                    self.screen.dismiss(None)
        if event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
            self.post_message(TurnFinished(event))
        if changed:
            if event.type != "error":
                self._sync_status()
            await self._sync_timeline()
            self._refresh_open_inspector()
            await ask_pending_question(self)
        if message.handled is not None and not message.handled.done():
            message.handled.set_result(None)

    @on(TurnFinished)
    def _turn_finished(self, message: TurnFinished) -> None:
        self._sync_status("")
        self._sync_agent()

    @on(StreamDisconnected)
    def _stream_disconnected(self, _: StreamDisconnected) -> None:
        self._sync_status("Disconnected · turn may continue on daemon", error=True)

    async def _sync_timeline(self) -> None:
        if not self.is_mounted:
            return
        await self.query_one("#conversation", ConversationTimeline).set_view(self.controller.view)
        self.query_one(SessionSidebar).set_current_running(self.controller.view.phase == "running")
        self._sync_topbar()
        usage = context_usage(self.controller.view)
        thinking = thinking_status(self.controller.view)
        self.query_one("#context-usage", Static).update(
            " · ".join(filter(None, (usage, thinking)))
        )

        queue = self.controller.view.input_queue
        preview = self.query_one("#input-queue-preview", Static)
        preview.display = bool(queue)
        preview.update("\n".join(
            f"{item.mode.title() if item.mode != 'queue' else 'Queued'} · " +
            sanitize("".join(block.get("text", "") for block in item.content if isinstance(block, dict)), 160)
            for item in queue[:3]
        ) + (f"\n+{len(queue) - 3} more queued" if len(queue) > 3 else ""))
        self.query_one("#message-send-hint", Static).display = self.controller.running

        self._sync_activity()
        if self.controller.session and self._context_preview_session != self.controller.session:
            self._start_context_preview()
        self._sync_details()
        preview = self.query_one("#context-preview", ContextPreview)
        active = self.controller.view.active_turn is not None
        preview.display = self._preview_visible()
        if active:
            pass  # keep the last assembled context visible during a turn
        elif self._context_preview_session == self.controller.session:
            preview.set_preview(
                self._context_preview,
                loading=self._context_preview_loading,
                error=self._context_preview_error,
            )
        elif self.controller.session:
            self._start_context_preview()

    def _start_context_preview(self) -> None:
        """Inspect the current assembled request for the active session."""
        if not self.is_mounted:
            return
        preview = self.query_one("#context-preview", ContextPreview)
        view = self.controller.view
        if view.active_turn is not None:
            return
        session = self.controller.session
        self._context_preview_generation += 1
        generation = self._context_preview_generation
        self._context_preview_session = session
        self._context_preview = None
        self._context_preview_error = None
        self._context_preview_loading = True
        preview.display = self._preview_visible()
        preview.set_preview(None, loading=True)
        previous = self._context_preview_task
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self._load_context_preview(session, generation))
        self._context_preview_task = task

        def clear_task(completed: asyncio.Task[None]) -> None:
            if self._context_preview_task is completed:
                self._context_preview_task = None

        task.add_done_callback(clear_task)

    async def _load_context_preview(self, session: str, generation: int) -> None:
        try:
            inspect = getattr(self.controller.client, "inspect_context", None)
            if not callable(inspect):
                raise ClientError("context inspection is not supported by this host")
            result = await inspect(session)
            if not isinstance(result, p.ContextInspectResult):
                raise ClientError("host returned an invalid context preview")
            if (
                session != self.controller.session
                or generation != self._context_preview_generation
                or not self.is_mounted
            ):
                return
            self._context_preview = result
            self._context_preview_error = None
            self._context_preview_loading = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - unsupported and active sessions are shown inline
            if (
                session != self.controller.session
                or generation != self._context_preview_generation
                or not self.is_mounted
            ):
                return
            self._context_preview = None
            self._context_preview_error = sanitize(str(exc), 1000)
            self._context_preview_loading = False
        preview = self.query_one("#context-preview", ContextPreview)
        if self.controller.view.active_turn is None:
            preview.set_preview(self._context_preview, loading=self._context_preview_loading, error=self._context_preview_error)
        header = self.query_one(ContextHeader)
        if self._context_preview is not None:
            header.set_data(self._context_preview, color=self._agent_color_for(
                str(self._context_preview.agent.get("name") or self.controller.agent_name)))
        elif self._context_preview_error:
            header.set_unavailable()

    async def action_open_context(self) -> None:
        """Show the host-generated current request and its accounting."""
        session = self.controller.session
        try:
            if self._context_preview_session == session and self._context_preview is not None:
                self.push_screen(ContextDetailsScreen(
                    self._context_preview,
                    context_detail_usage(self.controller.view),
                    session=session,
                ))
                return
            inspect = getattr(self.controller.client, "inspect_context", None)
            if not callable(inspect):
                raise ClientError("context inspection is not supported by this host")
            result = await inspect(session)
            if session != self.controller.session:
                return
            if not isinstance(result, p.ContextInspectResult):
                raise ClientError("host returned an invalid context preview")
            self.push_screen(
                ContextDetailsScreen(
                    result,
                    context_detail_usage(self.controller.view),
                    session=session,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - host may refuse inspection while a turn is active
            if session != self.controller.session:
                return
            self.push_screen(
                ContextDetailsScreen(
                    None,
                    context_detail_usage(self.controller.view),
                    error=sanitize(str(exc), 1000),
                    session=session,
                )
            )

    async def _agent_open_requested(self, message: AgentOpenRequested) -> None:
        try:
            # Resolve through the host contract (the same sanitized child view
            # available to remote inspectors); rendered state remains the live
            # canonical reducer projection maintained by the event bridge.
            remote = await self.controller.get_agent(message.agent_id)
            agent = self.controller.find_agent(message.agent_id)
            if agent is None or remote is None:
                self._sync_status("Agent transcript is not available", error=True)
                return
            self.push_screen(AgentTranscriptScreen(agent, remote.get("context")))
        except ClientError as exc:
            self._sync_status(f"Agent transcript failed · {exc}", error=True)

    async def action_open_agent(self, agent_id: str) -> None:
        """Open a nested agent from the inspector by its canonical id."""
        await self._agent_open_requested(AgentOpenRequested(agent_id))

    async def on_agent_open_requested(self, message: AgentOpenRequested) -> None:
        await self._agent_open_requested(message)

    def _refresh_open_inspector(self) -> None:
        screen = self.screen
        if isinstance(screen, AgentTranscriptScreen):
            agent = self.controller.find_agent(screen.agent.id)
            if agent is not None:
                screen.refresh_agent(agent)

    async def _permission_requested(
        self,
        message: PermissionRequested,
        handled: asyncio.Future[None] | None = None,
    ) -> None:
        request_id = str(message.data.get("id") or "")
        if not request_id:
            self._sync_status("Invalid permission request · denying", error=True)
            if handled is not None and not handled.done():
                handled.set_result(None)
            return
        self._pending_permission_id = request_id
        decision_future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._permission_result = decision_future

        def answered(decision: str | None) -> None:
            if not decision_future.done():
                decision_future.set_result(decision or "deny_once")

        self.push_screen(PermissionScreen(message.data), callback=answered)
        try:
            decision = await decision_future
            if decision is not None:
                await self._resolve_permission(request_id, decision)
        finally:
            self._permission_result = None
            if handled is not None and not handled.done():
                handled.set_result(None)

    async def _resolve_permission(self, request_id: str, decision: str) -> None:
        try:
            resolved = await self.controller.client.resolve_permission(
                self.controller.session, request_id, decision
            )
            self._sync_status(
                f"Permission {decision}" if resolved else "Another view answered first"
            )
            if not resolved and self._pending_permission_id == request_id:
                self._pending_permission_id = None
                self.pop_screen()
        except ClientError as exc:
            self._sync_status(f"Permission response failed · {exc}", error=True)

    @on(AgentPickerRequested)
    async def _agent_picker_requested(self, _: AgentPickerRequested) -> None:
        if self.controller.view.turns:
            self._sync_status("Agents are locked after the first turn to preserve the prompt cache. Start a new session to change agents.")
            return
        try:
            self._agents = await self.controller.client.list_agents()
            self._show_inline_picker(
                "agent",
                [
                    {**row, "_label": _display_name(row.get("name"))}
                    for row in self._agents
                ],
            )
        except ClientError as exc:
            self._sync_status(f"Agent selection failed · {exc}", error=True)

    def _show_inline_picker(
        self, kind: str, rows: list[dict], *, current: str | None = None
    ) -> None:
        panel = self.query_one("#inline-picker", AgentPickerPanel)
        self._picker_restore_focus = self.focused
        self._inline_picker_kind = kind
        panel.kind = kind
        panel.set_agents(
            rows,
            current=self.controller.agent_name
            if current is None else current,
        )
        panel.configure(
            kind,
            current_model=(
                f"{self.controller.provider}/{self.controller.model}"
                if self.controller.provider and self.controller.model else ""
            ),
            current_effort=self.controller.reasoning_effort,
            stored_override=self.controller.stored_override,
            effort_source=self.controller.reasoning_effort_source,
        )
        panel.display = True
        self.query_one("#chat-input", ChatInput).close_completion()
        self.query_one("#chat-editor", TextArea).disabled = True
        self.call_after_refresh(lambda: panel.query_one("#agent-options", OptionList).focus())

    def _close_inline_picker(self) -> None:
        if self._inline_picker_kind is None:
            return
        self._inline_picker_kind = None
        panel = self.query_one("#inline-picker", AgentPickerPanel)
        panel.display = False
        panel._filter = ""
        self.query_one("#chat-editor", TextArea).disabled = False
        target = self._picker_restore_focus
        self._picker_restore_focus = None
        self.call_after_refresh(
            target.focus if target is not None and target.is_mounted
            else self.query_one("#chat-editor", TextArea).focus
        )

    async def _open_effort_picker(self) -> None:
        try:
            current = await self.controller.client.current_agent(self.controller.session)
            levels = list(getattr(current, "supported_levels", ()) or ())
            if not levels:
                await self._show_notice("Reasoning effort unavailable for this model")
                return
            current_effort = getattr(current, "reasoning_effort", None)
            rows = [
                {
                    "name": str(level),
                    "_value": str(level),
                    "_label": f"{level}{' · current' if level == current_effort else ''}",
                    "description": "Applies to the next turn",
                }
                for level in levels
            ]
            self._show_inline_picker("effort", rows, current=str(current_effort or ""))
        except ClientError as exc:
            self._sync_status(f"Reasoning effort selection failed · {exc}", error=True)

    @on(AgentPickerPanel.Selected)
    async def _inline_picker_selected(self, message: AgentPickerPanel.Selected) -> None:
        kind = self._inline_picker_kind
        self._close_inline_picker()
        if kind == "agent":
            await apply_agent_choice(self, message.value)
        elif kind == "model":
            await self._apply_model_selection(
                message.value, message.effort, commit_effort=message.commit_effort
            )
        elif kind == "effort":
            await self._apply_effort_selection(message.value)

    async def _apply_effort_selection(self, effort: str) -> None:
        session = self.controller.session
        revision = self.controller._agent_metadata_revision
        self._reasoning_effort_in_flight = True
        try:
            await self.controller.client.select_reasoning_effort(session, effort)
            if session != self.controller.session or revision != self.controller._agent_metadata_revision:
                return
            await self.controller.refresh_agent_metadata()
            if session != self.controller.session or revision != self.controller._agent_metadata_revision:
                return
            self._sync_agent()
            self._sync_status("")
            self._refresh_context_after_selection(session)
        except ClientError as exc:
            self._sync_status(f"Reasoning effort selection failed · {exc}", error=True)
        finally:
            self._reasoning_effort_in_flight = False

    async def _effort_command(self, args: tuple[str, ...]) -> None:
        """``/effort`` opens the effort picker; ``/effort LEVEL`` selects directly."""
        if not args:
            await self._open_effort_picker()
            return
        current = await self.controller.client.current_agent(self.controller.session)
        levels = [str(level) for level in getattr(current, "supported_levels", ()) or ()]
        level = args[0].casefold()
        if level in levels:
            await self._apply_effort_selection(level)
        elif levels:
            await self._show_notice(f"Use /effort {'|'.join(levels)}")
        else:
            await self._show_notice("Reasoning effort unavailable for this model")

    @on(AgentPickerPanel.Cancelled)
    def _inline_picker_cancelled(self, _: AgentPickerPanel.Cancelled) -> None:
        self._new_session_id = None
        self._close_inline_picker()

    async def _apply_agent_selection(self, name: str) -> None:
        try:
            await self.controller.select_agent(name)
            self._sync_agent()
            self._refresh_context_after_selection(self.controller.session)
            self._sync_status("")
        except ClientError as exc:
            self._sync_status(f"Agent selection failed · {exc}", error=True)

    async def _apply_model_selection(
        self, name: str, effort: str | None = None, *, commit_effort: bool = True
    ) -> None:
        session = self.controller.session
        try:
            if commit_effort:
                await self.controller.select_model_and_effort(name, effort)
            else:
                await self.controller.select_model(name)
            if session != self.controller.session:
                return
            self._remember_model(name)
            self._sync_agent()
            self._sync_status("")
            self._refresh_context_after_selection(session)
        except ClientError as exc:
            if session != self.controller.session:
                return
            self._sync_agent()
            prefix = "Model selected, but effort update failed" if getattr(exc, "model_selected", False) else "Model selection failed"
            self._sync_status(f"{prefix} · {exc}", error=True)
            self._refresh_context_after_selection(session)

    def _refresh_context_after_selection(self, session: str) -> None:
        if (session == self.controller.session
                and self.controller.view.active_turn is None):
            self._start_context_preview()

    @on(CancelRequested)
    async def _cancel_requested(self, _: CancelRequested) -> None:
        await self.action_cancel_turn()

    async def action_cancel_turn(self) -> None:
        if not self.controller.running:
            editor = self.query_one("#chat-editor", TextArea)
            if editor.text:
                editor.clear()
                self._last_ctrl_c = 0.0
                self._sync_status("")
                return
            now = time.monotonic()
            if now - self._last_ctrl_c <= 1.5:
                await self.action_quit_shell()
                return
            self._last_ctrl_c = now
            self._sync_status("Press ctrl+c again to quit")
            self.set_timer(1.5, lambda: self._sync_status("") if self._last_ctrl_c == now else None)
            return
        self._last_ctrl_c = 0.0
        try:
            result = await self.controller.cancel()
            editor = self.query_one("#chat-editor", TextArea)
            restored = [text for text in result.returned_messages if text]
            if restored:
                editor.text = "\n\n".join([*restored, *([editor.text] if editor.text else [])])
                editor.move_cursor(editor.document.end)
                editor.focus()
            self._sync_status(
                f"Cancel requested · cancelled={result.cancelled} · returned to composer={len(restored)}"
            )
        except ClientError as exc:
            self._sync_status(f"Cancel failed · {exc}", error=True)

    async def action_reconnect(self) -> None:
        try:
            if self._reconnect_factory is not None:
                client = await self._reconnect_factory()
                previous = self.controller.replace_client(client)
                if previous is not client:
                    with contextlib.suppress(ClientError, OSError):
                        await previous.aclose()
                await client.handshake()
            await self.controller.bootstrap()
            await self._sync_timeline()
            self._sync_agent()
            active = self.controller.view.active_turn is not None
            if active:
                self.controller.resume(self._post_event)
                self._sync_status("Reconnected · following active turn")
            else:
                self._sync_status("Reconnected · ready")
        except ClientError as exc:
            self._sync_status(f"Disconnected · {exc}", error=True)

    async def action_pick_agent(self) -> None:
        await self._agent_picker_requested(AgentPickerRequested())

    async def action_new_session(self) -> None:
        await self._dispatch_chat_command("/new")

    async def action_list_sessions(self) -> None:
        self._open_sessions_dialog()

    async def action_fork_session(self) -> None:
        await self._dispatch_chat_command("/fork")

    async def action_quit_shell(self) -> None:
        await self.voice.close()
        await self.controller.close()
        self.exit(0)

    def action_back_from_agent(self) -> None:
        if isinstance(self.screen, AgentTranscriptScreen):
            parent_id = self.screen.parent_agent_id
            if parent_id and parent_id != self.controller.session:
                parent = self.controller.find_agent(parent_id)
                if parent is not None:
                    self.run_worker(self.action_open_agent(parent.id), group="agent-open")
                    return
            self.screen.dismiss(None)


    def _agent_color_for(self, name: str) -> str | None:
        """The host-declared color of agent ``name`` (the ``color:`` in its file)."""
        for row in self._agents:
            if str(row.get("name", "")).casefold() == name.casefold() and isinstance(row.get("color"), str):
                return row["color"] or None
        return self.controller.agent_color if name.casefold() == self.controller.agent_name.casefold() else None

    def _sync_agent_colors(self) -> None:
        colors = {
            str(row.get("name", "")).casefold(): row["color"]
            for row in self._agents
            if isinstance(row.get("color"), str) and row.get("color")
        }
        self.query_one("#conversation", ConversationTimeline).agent_colors.update(colors)
        if self._context_preview is not None:
            self.query_one(ContextHeader).set_data(self._context_preview, color=self._agent_color_for(
                str(self._context_preview.agent.get("name") or self.controller.agent_name)))

    def _sync_agent(self) -> None:
        status = self.controller.view.phase
        bar = self.query_one("#root-agent", RootAgentBar)
        bar.set_effort_metadata(
            supported_levels=(self.controller.supported_levels
                              if self.controller.agent_metadata_known else None),
            stored_override=self.controller.stored_override,
            effort_source=self.controller.reasoning_effort_source,
        )
        bar.set_agent(
            _display_name(self.controller.agent_name), self.controller.agent_source, status,
            color=self.controller.agent_color,
            provider=self.controller.provider, model=self.controller.model,
            reasoning_effort=self.controller.reasoning_effort,
            thinking_budget=self.controller.thinking_budget)
        self._sync_details()
        self._sync_activity()

    def _sync_activity(self) -> None:
        if not self.is_mounted:
            return
        used, window, _measured = context_measure(self.controller.view)
        self.query_one(ActivityProgress).set_state(
            used=used or 0,
            budget=window or 0,
            # The durable view is authoritative: once it has no active turn
            # (completed, failed, cancelled), stop animating even if the live
            # stream has not wound down yet.
            running=self.controller.running and self.controller.view.active_turn is not None,
            loading=self._status_text.startswith(("Connecting", "Reconnecting")),
            color=self.controller.agent_color or "$nx-accent",
        )

    async def _cycle_root_agent(self) -> None:
        """Select the next root-capable agent through the host contract."""
        if self.controller.view.turns:
            self._sync_status("Agents are locked after the first turn to preserve the prompt cache. Start a new session to change agents.")
            return
        try:
            self._agents = await self.controller.client.list_agents()
            eligible = [
                str(row.get("name", ""))
                for row in self._agents
                if row.get("name") and ("contexts" not in row or "root" in row.get("contexts", ()))
            ]
            canonical = [name for name in ("build", "orchestrator") if name in eligible]
            custom = sorted(
                (name for name in eligible if name not in {"build", "orchestrator"}),
                key=str.casefold,
            )
            ordered = [*canonical, *custom]
            if not ordered:
                return
            try:
                index = ordered.index(self.controller.agent_name)
            except ValueError:
                index = -1
            await self._apply_agent_selection(ordered[(index + 1) % len(ordered)])
        except ClientError as exc:
            self._sync_status(f"Agent selection failed · {exc}", error=True)

    def _sync_status(self, text: str | None = None, *, error: bool = False) -> None:
        if text is None:
            if self._status_error:
                text, error = self._status_text, True
            else:
                text = ""
        if (
            text == "Turn running"
            or (text.startswith("Turn running ·") and text != "Turn running · message not sent")
            or text in {
            "Connected · ready",
            "Reconnected · ready",
            }
        ):
            text = ""
            error = False
        self._status_error = error
        self._status_text = text
        # The release notice only fills an otherwise empty status line.
        self.query_one("#connection-status", ConnectionStatus).set_status(
            text or self._update_notice, error=error
        )
        self._sync_activity()

    async def on_unmount(self) -> None:
        self._context_preview_generation += 1
        preview_task = self._context_preview_task
        self._context_preview_task = None
        if preview_task is not None and preview_task is not asyncio.current_task():
            preview_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await preview_task
        self._logs_open = False
        self._logs_generation += 1
        self._cancel_logs_polling()
        if self._permission_result is not None and not self._permission_result.done():
            self._permission_result.set_result(None)
        await self.voice.close()
        await self.controller.close()

__all__ = [
    "KEYBOARD_SHORTCUTS",
    "SHORTCUTS",
    "ChatCommandProvider",
    "NexusTextualApp",
    "ShortcutsCommandProvider",
    "ShortcutsScreen",
]
