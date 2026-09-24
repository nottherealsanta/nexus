"""Textual shell. All runtime/session work is delegated to the host Client."""

from __future__ import annotations

import asyncio
import uuid
from importlib.resources import files
from collections.abc import Awaitable, Callable

from textual import on
from textual.app import App, ComposeResult
from textual.command import Provider, Hit, Hits
from textual.containers import Horizontal
from textual.widgets import Input, Static, TextArea

from ...events import Event
from ...view import initial_state
from ..cli.client import Client, ClientError
from .agent_picker import AgentPicker
from .agent_transcript import AgentTranscriptScreen
from .controller import TuiController
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
from .permission import PermissionScreen
from .theme import NEXUS_DARK
from .timeline import ConversationTimeline
from .widgets import ChatInput, ConnectionStatus, RootAgentBar
from ..cli import commands
from ..cli.details import detail_lines
from ..cli.render import sanitize


class ChatCommandProvider(Provider):
    """Textual command palette backed by the established chat command specs."""

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        for spec in commands.SPECS:
            label = (
                f"Quit chat — {spec.summary}"
                if spec.name == "/exit"
                else f"{spec.name} {spec.usage} — {spec.summary}".strip()
            )
            raw = "/exit" if spec.name == "/exit" else spec.name
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    lambda command=raw: app.run_worker(
                        app._dispatch_chat_command(command),
                        group="chat-command",
                        exclusive=True,
                    ),
                    help=spec.summary,
                )


class NexusTextualApp(App[int]):
    """Full-screen interactive client of the existing Nexus daemon protocol."""

    TITLE = "Nexus"
    COMMANDS = {lambda: ChatCommandProvider}
    ENABLE_COMMAND_PALETTE = True
    CSS = files(__package__).joinpath("app.tcss").read_text(encoding="utf-8")
    BINDINGS = [
        ("ctrl+q", "quit_shell", "Quit"),
        ("ctrl+c", "cancel_turn", "Cancel"),
        ("ctrl+g", "pick_agent", "Root agent"),
        ("ctrl+r", "reconnect", "Reconnect"),
        ("ctrl+p", "command_palette", "Commands"),
        ("ctrl+n", "new_session", "New session"),
        ("ctrl+o", "list_sessions", "Sessions"),
        ("ctrl+f", "fork_session", "Fork"),
    ]

    def __init__(
        self,
        client: Client,
        *,
        session: str = "default",
        reconnect: Callable[[], Awaitable[Client]] | None = None,
    ) -> None:
        super().__init__()
        self.controller = TuiController(client, session)
        self._agents: list[dict] = []
        self._boot_error = ""
        self._reconnect_factory = reconnect
        self._pending_permission_id: str | None = None
        self._permission_result: asyncio.Future[str | None] | None = None
        self._command_busy = False
        self._active_tasks: list[asyncio.Task] = []
        self._new_session_index = 0
        self.register_theme(NEXUS_DARK)
        self.theme = NEXUS_DARK.name

    def compose(self) -> ComposeResult:
        with Horizontal(id="shell-header"):
            yield Static("NEXUS", id="app-title")
            yield RootAgentBar(id="root-agent")
        yield ConversationTimeline(id="conversation")
        yield ChatInput(id="chat-input")
        yield ConnectionStatus("Connecting to Nexus daemon…", id="connection-status")

    async def on_mount(self) -> None:
        try:
            await self.controller.bootstrap()
            self._agents = await self.controller.client.list_agents()
            self._sync_status("Connected · ready")
            self._sync_agent()
            await self._sync_timeline()
            self.query_one("#chat-editor", TextArea).focus()
        except ClientError as exc:
            self._boot_error = str(exc)
            self._sync_status(f"Disconnected · {exc} · Ctrl+R to reconnect", error=True)

    async def on_key(self, event) -> None:
        if event.key == "ctrl+n" and not isinstance(self.focused, TextArea):
            event.stop()
            asyncio.create_task(self._dispatch_chat_command("/new"))
        elif event.key == "ctrl+o" and not isinstance(self.focused, TextArea):
            event.stop()
            asyncio.create_task(self._dispatch_chat_command("/sessions"))
        elif event.key == "ctrl+f" and not isinstance(self.focused, TextArea):
            event.stop()
            asyncio.create_task(self._dispatch_chat_command("/fork"))
        elif event.key == "escape" and isinstance(self.screen, AgentTranscriptScreen):
            event.stop()
            self.action_back_from_agent()
        elif event.key == "ctrl+enter" and isinstance(self.focused, TextArea):
            event.stop()
            event.prevent_default()
            self.query_one("#chat-input", ChatInput)._submit()
        elif event.key == "shift+tab" and isinstance(self.focused, TextArea):
            # TextArea normally owns Tab for indentation; this global shortcut
            # intentionally selects the daemon-owned root agent instead.
            event.stop()
            event.prevent_default()
            asyncio.create_task(self._cycle_root_agent())
        elif (
            event.key.lower() == "a"
            and not isinstance(self.focused, (TextArea, Input))
            and not isinstance(self.screen, AgentPicker)
        ):
            event.stop()
            self.post_message(AgentPickerRequested())

    @on(InputSubmitted)
    async def _input_submitted(self, message: InputSubmitted) -> None:
        stripped = message.content.strip()
        if stripped.startswith("/"):
            await self._dispatch_chat_command(stripped)
            return
        if self.controller.running:
            self._sync_status("Turn running · Ctrl+C cancels", error=False)
            return
        try:
            self._sync_status("Turn running · Ctrl+C cancels")
            self.controller.start_turn(message.content, self._post_event)
        except ClientError as exc:
            self._sync_status(f"Disconnected · {exc} · Ctrl+R to reconnect", error=True)

    async def _dispatch_chat_command(self, raw: str) -> None:
        parsed = commands.parse(raw)
        if parsed is None:
            return
        args = parsed.args
        try:
            if parsed.name == "/help":
                await self._show_notice(
                    commands.help_text()
                    + "\nTextual controls: Ctrl+P commands · Ctrl+N new · Ctrl+O sessions · Ctrl+F fork · Shift+Tab root agent"
                )
            elif parsed.name == "/exit":
                await self.action_quit_shell()
            elif parsed.name == "/cancel":
                await self.action_cancel_turn()
            elif parsed.name == "/reconnect":
                await self.action_reconnect()
            elif parsed.name == "/new":
                session = args[0] if args else f"session-{uuid.uuid4().hex[:8]}"
                await self._switch_session(session)
            elif parsed.name == "/sessions":
                summaries = await self.controller.client.list_sessions()
                if args:
                    matched = next((row for row in summaries if row.id == args[0] or row.id.startswith(args[0])), None)
                    if matched is None:
                        await self._show_notice(f"No session matching {sanitize(args[0], 80)}")
                    else:
                        await self._switch_session(matched.id)
                else:
                    await self._show_notice("\n".join(
                        f"{row.id} [{row.state}] seq={row.last_seq} viewers={row.viewers} {row.title}"
                        for row in summaries
                    ) or "No sessions")
            elif parsed.name == "/model":
                if args:
                    if args[0] == "list":
                        models = await self.controller.client.list_models(
                            selectable_only=True,
                            search=args[1] if len(args) > 1 else None,
                        )
                        await self._show_notice("\n".join(
                            f"{row.get('provider', '?')}/{row.get('id', '?')} [{row.get('tier', '')}]"
                            for row in models
                        ))
                    else:
                        await self._model_command(args)
                else:
                    self._push_model_picker()
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


    async def _show_notice(self, text: str) -> None:
        # Command feedback is chrome, never synthetic conversation history.
        self._sync_status(sanitize(text, 240))

    async def _switch_session(self, session: str) -> None:
        await self.controller.switch_session(session)
        await self.controller.bootstrap()
        await self._sync_timeline()
        self._sync_agent()
        self._sync_status(f"Session {sanitize(session, 60)}")

    async def _model_command(self, args: tuple[str, ...]) -> None:
        result = await self.controller.select_model(args[0])
        await self._show_notice(f"model -> {result.provider}/{result.model} (applies next turn)")

    async def _push_model_picker(self) -> None:
        models = await self.controller.client.list_models(selectable_only=True)
        choices = [
            {"name": f"{row.get('provider', '?')}/{row.get('id', '?')}",
             "id": f"{row.get('provider', '?')}/{row.get('id', '?')}",
             "description": f"{row.get('tier', '')} · {row.get('context', '?')} ctx",
             "contexts": ["root"]}
            for row in models
        ]

        def selected(name: str | None) -> None:
            if name:
                asyncio.create_task(self._model_command((name,)))

        self.push_screen(AgentPicker(choices, current=""), callback=selected)

    async def _agent_command(self, args: tuple[str, ...]) -> None:
        action = args[0] if args else "list"
        if action == "list":
            await self.post_message(AgentPickerRequested())
            return
        if action == "current":
            result = await self.controller.client.current_agent(self.controller.session)
            await self._show_notice(f"{result.name} ({result.source})")
            return
        result = await (
            self.controller.client.reset_agent(self.controller.session)
            if action == "reset"
            else self.controller.client.select_agent(self.controller.session, action)
        )
        self.controller.agent_name = result.name
        self.controller.agent_source = result.source
        self._sync_agent()
        await self._show_notice(f"Agent {result.name} selected · applies next turn")

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
            self._sync_status()
            await self._sync_timeline()
            self._refresh_open_inspector()
        if message.handled is not None and not message.handled.done():
            message.handled.set_result(None)

    @on(TurnFinished)
    def _turn_finished(self, message: TurnFinished) -> None:
        self._sync_status(f"{message.event.type.removeprefix('turn.')} · ready")
        self._sync_agent()

    @on(StreamDisconnected)
    def _stream_disconnected(self, _: StreamDisconnected) -> None:
        self._sync_status("Disconnected · turn may continue on daemon · Ctrl+R to reconnect", error=True)

    async def _sync_timeline(self) -> None:
        if not self.is_mounted:
            return
        await self.query_one("#conversation", ConversationTimeline).set_view(self.controller.view)

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
            screen = AgentTranscriptScreen(agent)
            self.push_screen(screen)
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
        try:
            self._agents = await self.controller.client.list_agents()
            def selected(name: str | None) -> None:
                if name:
                    asyncio.create_task(self._apply_agent_selection(name))

            self.push_screen(
                AgentPicker(self._agents, current=self.controller.agent_name),
                callback=selected,
            )
        except ClientError as exc:
            self._sync_status(f"Agent selection failed · {exc}", error=True)

    async def _apply_agent_selection(self, name: str) -> None:
        try:
            result = await self.controller.select_agent(name)
            self._sync_agent()
            self._sync_status(f"Agent {result.name} selected · applies next turn")
        except ClientError as exc:
            self._sync_status(f"Agent selection failed · {exc}", error=True)

    @on(CancelRequested)
    async def _cancel_requested(self, _: CancelRequested) -> None:
        await self.action_cancel_turn()

    async def action_cancel_turn(self) -> None:
        try:
            cancelled, dropped = await self.controller.cancel()
            self._sync_status(f"Cancel requested · cancelled={cancelled} · dropped={dropped}")
        except ClientError as exc:
            self._sync_status(f"Cancel failed · {exc}", error=True)

    async def action_cancel_request(self) -> None:
        await self.action_cancel_turn()

    async def action_reconnect(self) -> None:
        try:
            if self._reconnect_factory is not None:
                client = await self._reconnect_factory()
                previous = self.controller.replace_client(client)
                if previous is not client:
                    try:
                        await previous.aclose()
                    except Exception:
                        pass
                await client.handshake()
            await self.controller.bootstrap()
            await self._sync_timeline()
            active = self.controller.view.active_turn is not None
            if active:
                self.controller.resume(self._post_event)
                self._sync_status("Reconnected · following active turn")
            else:
                self._sync_status("Reconnected · ready")
        except ClientError as exc:
            self._sync_status(f"Disconnected · {exc} · Ctrl+R to retry", error=True)

    async def action_pick_agent(self) -> None:
        await self._agent_picker_requested(AgentPickerRequested())

    async def action_new_session(self) -> None:
        await self._dispatch_chat_command("/new")

    async def action_list_sessions(self) -> None:
        await self._dispatch_chat_command("/sessions")

    async def action_fork_session(self) -> None:
        await self._dispatch_chat_command("/fork")

    async def action_quit_shell(self) -> None:
        await self.controller.close()
        self.exit(0)

    def action_back_from_agent(self) -> None:
        if isinstance(self.screen, AgentTranscriptScreen):
            parent_id = self.screen.parent_agent_id
            if parent_id and parent_id != self.controller.session:
                parent = self.controller.find_agent(parent_id)
                if parent is not None:
                    self.push_screen(AgentTranscriptScreen(parent))
                    return
            self.screen.dismiss(None)

    def _sync_agent(self) -> None:
        status = self.controller.view.phase
        self.query_one("#root-agent", RootAgentBar).set_agent(
            self.controller.agent_name, self.controller.agent_source, status
        )

    async def _cycle_root_agent(self) -> None:
        """Select the next root-capable agent through the host contract."""
        try:
            self._agents = await self.controller.client.list_agents()
            eligible = [
                str(row.get("name", ""))
                for row in self._agents
                if row.get("name") and ("contexts" not in row or "root" in row.get("contexts", ()))
            ]
            canonical = [name for name in ("general", "plan", "build", "explore") if name in eligible]
            custom = sorted(
                (name for name in eligible if name not in {"general", "plan", "build", "explore"}),
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
            status = self.controller.view.phase
            cursor = self.controller.cursor
            text = f"{self.controller.session} · {status} · seq {cursor}"
        self.query_one("#connection-status", ConnectionStatus).set_status(text, error=error)

    async def on_unmount(self) -> None:
        if self._permission_result is not None and not self._permission_result.done():
            self._permission_result.set_result(None)
        await self.controller.close()

    def on_exit_app(self) -> None:
        return None


__all__ = ["ChatCommandProvider", "NexusTextualApp"]
