"""Host-backed native shell commands (Ratatui feasibility §7).

Transient panels contain labelled values; mutations remain daemon commands.
Unsupported UI journeys report an explicit error and never become model input.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from collections import OrderedDict
from ...ui_support.completion import root_agents

import uuid
import asyncio
import contextlib
import time
from collections import deque

from ..cli.commands import help_text, parse
from ...ui_support.tool_details import flatten
from ...ui_support.usage import usage_lines


#: ``/restart`` waits at most polls x seconds for the old daemon to exit.
_RESTART_POLLS = 100
_RESTART_POLL_SECONDS = 0.1


def plain(value):
    if is_dataclass(value):
        value = asdict(value)
    elif hasattr(value, "__struct_fields__"):
        value = {key: getattr(value, key) for key in value.__struct_fields__}
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def labelled(value) -> list[str]:
    return [f"{row.label}: {row.value}" for row in flatten(plain(value))]


_SUCCESS = ("Copied", "Saved", "Theme saved", "Reset", "Deleted", "Reconnected", "Restarted", "Speech model is ready")
_WARNING = ("No session matching", "UI background actions busy", "Session changed", "Permission already",
            "Question already", "Context details are unavailable", "Subagent transcript is unavailable", "Usage:",
            "Showing the last successful")
_ERROR = ("Display update failed", "Save failed", "Conflict", "Error")


def notice_level(text: str) -> str:
    """Toast level for a legacy notice string (plan §8.6); explicit levels win."""
    if text.startswith(_ERROR) or "could not be shown" in text:
        return "error"
    if text.startswith(_SUCCESS) or text.endswith(" reset to default"):
        return "success"
    if text.startswith(_WARNING):
        return "warning"
    return "info"


def split_toast(text: str) -> tuple[str, str]:
    """Title and body of a toast: long messages split at the first natural break."""
    text = " ".join(str(text).split())
    if len(text) <= 48:
        return text, ""
    for sep in (" · ", ": ", ". ", "; "):
        head, found, rest = text.partition(sep)
        if found and 8 <= len(head) <= 80:
            return head, rest
    cut = text.rfind(" ", 0, 60)
    cut = cut if cut > 20 else 60
    return text[:cut], text[cut:].strip()


class ProjectOpenError(RuntimeError):
    """Another project's session could not be opened; the toast already said why."""


class ShellActions:
    """Session-scoped commands, attachments and transient inspection panels."""

    def __init__(self, controller):
        self.controller = controller
        self.panel_title = ""
        self.panel_layout = "modal"
        self.panel_format = "plain"
        self.panel_toggle = None
        self.panel_loading = False
        self.panel_hint = ""
        self.panel_revision = 0
        self.usage_cache = None
        self.usage_task = None
        self.usage_request = 0
        self.on_update = None
        self.panel_lines = []
        self.panel_tones = []
        self.attachments = []
        self.attachment_labels = {}
        self.attachment_label_counters = {"image": 0, "document": 0}
        self.attachment_drafts = {}
        self.attachment_draft_key = None
        self.marker_attachments = {}
        self.generation = 0
        self.items = []
        self.preview = None
        self.preview_at = None
        self.preview_image = b""
        self.preview_image_media = ""
        self.preview_image_attachment_id = ""
        self.agent_definitions = {}
        self._notice = ""  # the last legacy notice string (also the Disconnected banner state)
        self.toasts = deque(maxlen=20)  # newest bounded list, sent in every snapshot
        self._toast_id = int(time.time() * 1000)  # monotonic across restarts: the client shows each id once
        self._last_toast = ("", "", 0.0)
        self.notifications_open_seq = 0  # bumped by /notifications; the native client opens its drawer on change
        self.composer_restore = ""
        # The restore text that applies only to an empty draft (a cancelled steer message),
        # with its status-row note; any other restore text prepends as before.
        self.composer_restore_if_empty = ""
        self.composer_restore_note = ""
        self.composer_restore_attachments = 0  # draft attachments restored with that text
        self.composer_insert = ""
        self.composer_auto_send = False
        self.composer_insert_kind = ""
        self.expanded = set()
        self.collapsed_turns = set()
        self.verbose = False
        self.mcp_report = None
        self.mcp_error = None
        self.mcp_due = 0.0
        self.turn_cache = OrderedDict()
        self.turn_cache_bytes = 0
        self.preview_task: asyncio.Task | None = None
        self.sessions = []
        self.workspace = ""
        self.tabs = []
        self.last_completion_cue = 0
        self.breadcrumb = ""
        self.worktree = ""  # linked-worktree name of the workspace ("" in a main checkout)
        self.model_names: dict[tuple[str, str], str] = {}  # (provider, id) -> catalogue display name
        self.settings_nav: str | None = None  # selected Settings area while Settings is open
        self.model_sort = "updated"  # model picker order: updated|name (Ctrl+S)
        self.update_notice = ""  # "<version> available: <command>" for /update; toasted once per release
        self.sessions_truncated = False  # the host list hit its cap
        self.archived_label = ""  # "Archived · N" under the sessions list, "" when none
        self.seen_seq: dict[str, int] = {}  # completion sequence viewed per session
        self.open_files: set[str] = set()  # modified files expanded in the details sidebar
        self.reconnect = None
        from .preferences import Preferences
        from .workflows import Workflows
        from .voice import Voice
        from .live import Live
        from .logs import Logs
        self.preferences = Preferences()
        self.workflows = Workflows(self)
        self.voice = Voice(self)
        self.live = Live(self)
        self.logs = Logs(self)
        self.logs.open = self.preferences.values["details_sidebar"] and self.preferences.values["details_tab"] == "Logs"

    @property
    def client(self):
        return self.controller.client

    @property
    def notice(self) -> str:
        return self._notice

    @notice.setter
    def notice(self, value: str) -> None:
        self._notice = value
        if value and not value.startswith("Disconnected"):  # a disconnect is a banner, not an event
            self.toast(value)

    def flash(self, text: str, level: str | None = None, **options) -> None:
        """Set the legacy notice string and raise a toast with an explicit level."""
        self._notice = str(text)
        self.toast(text, level, **options)

    def toast(self, text, level: str | None = None, *, key: str = "", body: str = "", action: dict | None = None) -> None:
        """Queue a dismissible toast and mirror it to the Logs tab (plan §8.6)."""
        from ...ui_support.text import escape_controls
        text = str(text).strip()
        if not text:
            return
        level = level or notice_level(text)
        now = time.monotonic()
        if self._last_toast[:2] == (text, level) and now - self._last_toast[2] < 5:
            return  # a refresh loop must not repeat the same toast
        self._last_toast = (text, level, now)
        title, rest = split_toast(text)
        self._toast_id += 1
        self.toasts.append({
            "id": self._toast_id, "level": level, "title": escape_controls(title)[:200],
            "body": escape_controls(body or rest)[:400], "key": key, "action": action,
        })
        logs = getattr(self, "logs", None)
        if logs is not None:
            logs.add_notice(level, text)

    def refresh_session_tabs(self, rows):
        """Notify once when a known background tab finishes, never on discovery."""
        self.sessions = rows
        for row in rows:
            existing = next((tab for tab in self.tabs if tab["id"] == row["id"] and tab["workspace"] == row["workspace"]), None)
            if existing:
                if (row["id"] != self.controller.session and row["status"] == "done"
                        and existing.get("status") != "done"):
                    self.controller.completion_bell += 1
                existing.update(row)
            elif row["state"] in {"running", "awaiting_input", "awaiting_permission"}:
                self.tabs.append(dict(row))

    def notify_completion(self):
        """Play the shared generated cue once per new completion counter."""
        if self.controller.completion_bell > self.last_completion_cue:
            self.last_completion_cue = self.controller.completion_bell
            from nexus.ui_support.voice_capture import play_cue
            play_cue("complete")

    def show(self, title, value, *, layout="modal", format="plain"):
        self.panel_revision += 1
        self.panel_layout = layout
        self.panel_format = format
        self.panel_toggle = None
        self.panel_loading = False
        self.panel_hint = ""
        self.items = []
        if hasattr(self, "workflows"):
            self.workflows.form = None
            self.workflows.close_dashboard()  # any panel, picker or detail replaces the dashboard
        self.panel_title = title
        self.panel_lines = value.splitlines() if isinstance(value, str) else labelled(value)
        # Labelled values read as dim `label: ` then the value; plain text stays plain.
        self.panel_tones = [] if isinstance(value, str) else ["kv"] * len(self.panel_lines)

    def show_styled(self, title, lines, tones):
        """A panel whose lines carry tones (``ui_support.tool_details.styled_lines``)."""
        self.show(title, "")
        self.panel_lines, self.panel_tones = list(lines), list(tones)

    def picker(self, title, rows, command, key, layout="drawer"):
        self.show(title, rows, layout=layout)
        self.items = [{"label": str(row.get("title") or row.get("name") or row.get(key)),
                       "command": f"{command} {row[key]}"} for row in rows if row.get(key)]

    def open_usage(self):
        """Open immediately; one bounded background fetch refreshes the visible modal."""
        if self.usage_task:
            self.usage_task.cancel()
        self.usage_request += 1
        request = self.usage_request
        self.show_styled("Provider usage", *(usage_lines(self.usage_cache) if self.usage_cache is not None
                                            else (["No cached usage yet."], ["dim"])))
        self.panel_lines.append("r refresh · Esc close")
        self.panel_tones.append("dim")
        self.panel_loading = True
        revision, generation, client = self.panel_revision, self.generation, self.client

        async def refresh():
            try:
                result = await asyncio.wait_for(client.providers_usage(), 30)
                if request != self.usage_request or generation != self.generation or client is not self.client:
                    return
                self.usage_cache = result
                lines, tones = usage_lines(result)
                lines.append("r refresh · Esc close")
                tones.append("dim")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lines, tones = list(self.panel_lines), list(self.panel_tones)
                lines.append(f"Refresh failed: {type(exc).__name__}: {exc}")
                tones.append("bad")
            if revision != self.panel_revision or generation != self.generation or self.panel_title != "Provider usage":
                return
            self.panel_lines, self.panel_tones = lines, tones
            self.panel_loading = False
            if self.on_update:
                try:
                    await self.on_update()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The terminal closed while the read-only refresh completed.

        self.usage_task = asyncio.create_task(refresh())

    async def switch_project(self, workspace, session):
        """Open ``session`` in ``workspace``, starting that project's daemon when needed.

        A failed start (a stale install's ``ConfigError``, a daemon that never became ready, a
        timeout) is shown as an error toast with the redacted reason and the Sessions dashboard
        stays open on the old session; the dashboard closes only once the switch succeeded
        (``switch`` hides it). The error is re-raised so callers can restore a submitted draft.
        """
        if self.attachment_draft_key is None:
            self.attachment_draft_key = (self.workspace, self.controller.session)
        if workspace != self.workspace:
            self.usage_cache = None
            if self.usage_task:
                self.usage_task.cancel()
            try:
                result = await self.client.open_project_session(workspace, session)
                from ..cli import open_client
                client = await open_client(workspace, socket_path=result.socket_path)
            except Exception as exc:
                from ...host_support.errors import error_text
                reason = error_text(exc)
                self.flash(f"Could not open the session in {workspace}: {reason}", "error")
                raise ProjectOpenError(reason) from exc
            previous = self.controller.replace_client(client)
            self.workspace = workspace
            from pathlib import Path
            from ..cli import open_client
            self.reconnect = lambda: open_client(Path(workspace))
            await previous.aclose()
        await self.switch(session)

    def mark_current_seen(self):
        """Acknowledge both replayed snapshots and streamed events for this session."""
        current = self.controller.session
        if current:
            self.seen_seq[current] = max(
                self.seen_seq.get(current, 0),
                getattr(self.controller, "completion_seq", 0),
            )

    async def switch(self, session):
        self.mark_current_seen()  # a reply watched live must not read as unseen after leaving
        self.workflows.close_dashboard()  # a replaced session never keeps a dashboard over it
        await self.voice.discard()
        self.workflows.form = None
        self.workflows.stack.clear()
        self.settings_nav = None
        self.workflows.agent_page_id = None
        self.workflows.agent_context = {}
        self.workflows.agent_parents.clear()
        # The turn cache is keyed by session and bounded, so open tabs keep their rows.
        self.generation += 1
        self.logs.reset_session()
        if not any(row["id"] == session and row["workspace"] == self.workspace for row in self.tabs):
            self.tabs.append({"id": session, "title": "New Session", "workspace": self.workspace, "state": "idle"})
        old_key = self.attachment_draft_key or (self.workspace, self.controller.session)
        self.attachment_drafts[old_key] = (self.attachments, self.attachment_labels,
            self.attachment_label_counters, self.marker_attachments)
        self.attachment_draft_key = (self.workspace, session)
        (self.attachments, self.attachment_labels, self.attachment_label_counters,
            self.marker_attachments) = self.attachment_drafts.pop(self.attachment_draft_key,
                ([], {}, {"image": 0, "document": 0}, {}))
        self.items = []
        self.preview = None
        self.preview_at = None
        await self.controller.switch_session(session)
        await self.controller.bootstrap()
        self.mark_current_seen()
        # The context preview is a host round-trip the first paint does not need.
        if self.preview_task:
            self.preview_task.cancel()
        self.preview_task = asyncio.create_task(self._refresh_preview_later(session))
        self.panel_title = ""
        self.panel_loading = False
        self.panel_revision += 1
        if self.usage_task:
            self.usage_task.cancel()
        self.panel_lines = []

    async def _refresh_preview_later(self, session: str) -> None:
        try:
            if await self.refresh_preview(session) and self.on_update:
                await self.on_update()
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError):
            pass  # The terminal closed while the read-only refresh completed.
        except Exception as exc:  # noqa: BLE001 - surfaced like any other action failure
            self.flash(str(exc), "error")

    async def refresh_preview(self, session: str | None = None) -> bool:
        """Reload the next-turn context preview; an active session has none yet.

        The host refuses a preview while a turn runs. That is expected, not an
        error: the header retains the last successful preview and its timestamp.
        """
        try:
            from datetime import datetime
            generation, selected = self.generation, session or self.controller.session
            preview = await self.client.inspect_context(selected)
            if generation != self.generation or selected != self.controller.session:
                return False
            self.preview = preview
            self.preview_at = datetime.now().astimezone()
            return True
        except Exception as exc:  # noqa: BLE001 - only the "active" refusal is expected
            if "while the session is active" not in str(exc):
                raise
            return False

    def context_popover(self):
        """Live occupancy plus the last successful, timestamped request breakdown."""
        from .prototype import _context_display_view, _context_tiers
        from ...ui_support.context import context_measure, context_groups, context_pricing_note
        used, window, _ = context_measure(_context_display_view(self.controller.view, self))
        stamp = self.preview_at.strftime("%H:%M:%S") if self.preview_at else "unavailable"
        lines = [f"Used          {used:,} tokens" if used is not None else "Used          unavailable"]
        for index, limit in enumerate(_context_tiers(_context_display_view(self.controller.view, self))):
            label = "Window" if limit == window else f"Tier {index + 1}"
            lines.append(f"{label:<14}" + (f"{used / limit * 100:.1f}% of {limit:,}" if used is not None else f"{limit:,} tokens"))
        turns = self.controller.view.turns
        if turns:
            usage = turns[-1].usage
            lines.append(f"Last turn     in {usage.input_tokens:,} · out {usage.output_tokens:,} · cache read {usage.cache_read_tokens:,}")
        if self.preview is not None:
            if getattr(self.controller, "running", False):
                lines.append("Breakdown from before the running turn; occupancy is live.")
            lines.extend(f"{group.title} · ~{group.tokens:,} tokens · {group.detail}" for group in context_groups(self.preview))
            pricing = context_pricing_note(_context_display_view(self.controller.view, self))
            if pricing:
                lines.append(pricing)
        else:
            lines.append("Breakdown unavailable; open /context after the turn finishes.")
        lines.append("Enter open full /context view · Esc close")
        self.show(f"Context · as of {stamp}", "\n".join(lines), layout="drawer")


    async def select_agent(self, name):
        """All native selection paths refresh identity and request context together."""
        await self.controller.select_agent(name)
        rows = await self.client.list_agents()
        self.agent_definitions = {row["name"]: row for row in rows}
        await self.refresh_preview()

    async def cancel(self):
        result = await self.controller.cancel()
        self.composer_restore = "\n\n".join(result.returned_messages)

    async def close_tab(self, workspace, session):
        """Use the native tab-close policy without deleting or cancelling work."""
        index = next((i for i, row in enumerate(self.tabs)
                      if row["id"] == session and row["workspace"] == workspace), None)
        if index is not None and len(self.tabs) == 1:
            await self.command("/new", ())
        elif index is not None:
            self.tabs.pop(index)
            self.controller.forget(session)
            if self.controller.session == session and self.tabs:
                row = self.tabs[min(index, len(self.tabs) - 1)]
                await self.switch_project(row["workspace"], row["id"])

    async def submit(self, text, mode="steer", session=None):
        command = parse(text)
        if command:
            return await self.command(command.name, command.args)
        target = self.controller.session
        dashboard = getattr(getattr(self, "workflows", None), "dashboard", None)
        row = None
        if isinstance(session, str) and session and dashboard is not None and dashboard.visible:
            row = dashboard.row(session)
        if row is not None and row.get("id"):
            workspace = row.get("workspace") or ""
            if workspace and workspace != self.workspace:
                if self.attachments:
                    self.composer_restore = text
                    self.flash("Open that session to send attachments", "warning")
                    return True
                try:
                    await self.switch_project(workspace, row["id"])
                except Exception:
                    self.composer_restore = text
                    raise
                target = self.controller.session
            else:
                target = row["id"]
        if target != self.controller.session and self.attachments:
            self.composer_restore = text
            self.flash("Open that session to send attachments", "warning")
            return True
        if text.startswith("!"):
            if target == self.controller.session:
                return await self.run_shell(text)
            command_text = text[1:].strip()
            if not command_text:
                self.composer_restore = text
                raise ValueError("Type a command after ! to run it with bash")
            try:
                await self.client.shell(target, command_text)
            except Exception:
                self.composer_restore = text
                raise
            from ...ui_support.prompt_history import append_history
            append_history(text)
            if dashboard is not None and dashboard.visible:
                await dashboard.refresh()
            return True
        try:
            import uuid
            identity = (target, text, mode, tuple(item.attachment_id for item in self.attachments))
            pending = getattr(self, "_pending_submission", None)
            if pending is None or pending[0] != identity:
                pending = (identity, uuid.uuid4().hex)
                self._pending_submission = pending
            kwargs = {"client_request_id": pending[1]}
            if target == self.controller.session:
                kwargs["attachments"] = [item.attachment_id for item in self.attachments]
                kwargs["attachment_labels"] = [
                    self.attachment_label(index) for index in range(len(self.attachments))
                ]
            await self.client.enqueue(target, text, mode=mode, **kwargs)
            self._pending_submission = None
        except Exception:
            self.composer_restore = text
            raise
        from ...ui_support.prompt_history import append_history
        append_history(text)
        if target == self.controller.session:
            self.attachments.clear()
            self.attachment_labels.clear()
            self.marker_attachments.clear()
        if dashboard is not None and dashboard.visible:
            await dashboard.refresh()
        return True

    async def run_shell(self, text):
        """``!command``: run it with bash; the output joins the context, no turn starts."""
        command = text[1:].strip()
        if not command:
            self.composer_restore = text
            raise ValueError("Type a command after ! to run it with bash")
        try:
            await self.client.shell(self.controller.session, command)
        except Exception:
            self.composer_restore = text
            raise
        from ...ui_support.prompt_history import append_history
        append_history(text)
        return True

    def attachment_label(self, index):
        item = self.attachments[index]
        if item.attachment_id not in self.attachment_labels:
            kind = "image" if item.kind == "image" else "document"
            self.attachment_label_counters[kind] += 1
            number = self.attachment_label_counters[kind]
            self.attachment_labels[item.attachment_id] = f"{kind} {number}"
        return self.attachment_labels[item.attachment_id]

    def attachment_marker(self, index):
        item = self.attachments[index]
        marker = f"[{self.attachment_label(index)}]"
        self.marker_attachments[item.attachment_id] = (item, marker)
        return marker

    def reconcile_attachment_markers(self, text):
        """Deleted references do not send hidden images; undo can restore them."""
        unmanaged = [item for item in self.attachments
            if item.attachment_id not in self.marker_attachments]
        self.attachments = unmanaged + [item for item, marker in self.marker_attachments.values()
            if marker in text]

    async def restart_daemon(self):
        """Stop the workspace daemon, start a fresh one and reattach this client.

        Lifecycle stays on the client side (the same steps as ``nexus daemon
        restart``); the daemon is never asked to restart itself. Running turns
        end with the old process; durable state replays on the new one.
        """
        from pathlib import Path
        from ...host import daemon as daemon_mod
        if not self.workspace:
            raise ValueError("The workspace is unknown; run `nexus daemon restart` and relaunch")
        workspace = Path(self.workspace)
        session = self.controller.session
        # The follow task would auto-reconnect (opening its own client and a new
        # daemon) while the old one shuts down; hold it until we have reattached.
        await self.controller.pause()
        try:
            report = await daemon_mod.status(workspace)
            old_pid = report.get("pid") if report.get("running") else None
            stopped = await daemon_mod.stop(workspace)
            if old_pid and not stopped:
                raise RuntimeError("The daemon did not accept the stop request; run `nexus daemon stop` and relaunch")
            # Shutdown is acknowledged before the process exits, and ``status``
            # reads "not running" once the socket is gone even while the old
            # process still holds the lock: wait for the process itself.
            if not await daemon_mod.wait_stopped(workspace, old_pid, polls=_RESTART_POLLS, interval=_RESTART_POLL_SECONDS):
                raise RuntimeError("The daemon did not stop; run `nexus daemon stop` and relaunch")
            from ..cli import open_client
            client = await (self.reconnect() if self.reconnect else open_client(workspace))
            previous = self.controller.replace_client(client)
            with contextlib.suppress(Exception):
                await previous.aclose()
            await self.controller.switch_session(session)
            await self.controller.bootstrap()
            self.generation += 1
            self.notice = "Restarted the daemon and reconnected"
        finally:
            self.controller.unpause()  # on failure this resumes following (and auto-reconnect) as before

    async def command(self, name, args):
        session = self.controller.session
        argument = " ".join(args)
        if name == "/exit":
            return False
        if name == "/help":
            from ..cli.commands import SPECS
            self.items = []
            self.workflows.menu("Commands", [(spec.name + " · " + spec.summary, {"kind": "chat_command", "command": spec.name}) for spec in SPECS], help_text().splitlines())
            self.panel_layout = "drawer"
            for label, command in (("Ctrl+I · Context usage", "/context"), ("Ctrl+U · Provider usage", "/usage"),
                                   ("Ctrl+X M · Models", "/model"),
                                   ("Ctrl+X G · Agents", "/agent")):
                self.items.append({"label": label, "command": command})
        elif name == "/hotkeys":
            await self.workflows.open_page("keys")  # the same page as Settings → Keyboard
        elif name == "/notifications":
            if args:
                raise ValueError("Usage: /notifications")
            # The history is client-side (the Rust drawer): a changed counter asks it to open.
            self.notifications_open_seq += 1
        elif name == "/close":
            if args:
                raise ValueError("Usage: /close")
            await self.close_tab(self.workspace, session)
        elif name == "/new":
            session_id = argument or uuid.uuid4().hex[:12]
            agent = self.controller.agent_name
            await self.switch(session_id)
            await self.client.select_agent(session_id, agent)
            await self.controller.bootstrap()
        elif name in {"/session", "/sessions"}:
            if argument:
                rows = await self.client.list_sessions()
                matched = next((row for row in rows if row.id == args[0] or row.id.startswith(args[0])), None)
                if matched is None:
                    self.notice = f"No session matching {args[0][:80]}"
                else:
                    await self.switch(matched.id)
            else:
                await self.workflows.sessions()
        elif name == "/handoff":
            if argument == "read":
                result = await self.client.read_handoff(session)
                self.notice = result.get("text", "No handoff yet")
            elif not argument:
                await self.client.handoff(session, request_id=uuid.uuid4().hex)
                await self.controller.bootstrap()
                self.notice = "Context handed off; session history is preserved"
            else:
                raise ValueError("Usage: /handoff [read]")
        elif name == "/fork":
            result = await self.client.fork(session, int(args[0]) if args and args[0].isdigit() else None)
            await self.switch(result.id)
        elif name == "/reconnect":
            if self.reconnect:
                previous = self.controller.replace_client(await self.reconnect())
                await previous.aclose()
            await self.controller.switch_session(session)
            await self.controller.bootstrap()
            self.notice = ""
        elif name == "/restart":
            if args:
                raise ValueError("Usage: /restart")
            await self.restart_daemon()
        elif name == "/cancel":
            await self.cancel()
        elif name == "/model":
            if argument and argument != "list":
                await self.controller.select_model(argument)
                await self.refresh_preview(session)
                recent = self.preferences.values["model_recent"]
                self.preferences.set("model_recent", [argument, *[ref for ref in recent if ref != argument]])
            else:
                from ...ui_support.model_choice import model_groups, recent_models
                rows = [row for row in recent_models(await self.client.list_models(selectable_only=True, scope="main"))
                        if row.get("provider") and row.get("id")]
                for row in rows:
                    row["original_metadata"] = dict(row)
                    row["ref"] = f"{row['provider']}/{row['id']}"
                favorites = self.preferences.values["model_favorites"]
                recent = self.preferences.values["model_recent"]
                current = f"{self.controller.provider}/{self.controller.model}" if self.controller.provider and self.controller.model else ""
                groups, _ = model_groups(rows, sort_mode=self.model_sort, favorites=favorites, recent=recent)
                rows = []
                for title, group in groups:
                    for row in group:
                        rows.append({**row, "group": title, "name": str(row.get("name") or row["ref"])})
                self.picker("Select model", rows, "/model", "ref", layout="modal")
                sort = "Updated ↓" if self.model_sort == "updated" else "Name A–Z"
                self.panel_hint = f"Details ctrl+i  Sort ctrl+s {sort}  Favorite ctrl+f  Refresh ctrl+r"
                for item, row in zip(self.items, rows):
                    item.update(label=f"{row['original_metadata'].get('name') or row['id']} · {row['provider']}",
                                group=row["group"], detail="", current=row["ref"] == current)
                    self.workflows.model_item_metadata(item, row["original_metadata"])
                    state = dict(current=current, current_effort=self.controller.reasoning_effort, stored_override=self.controller.stored_override)
                    from ...ui_support.model_choice import preselected_effort, selection_effort
                    keep, commit = selection_effort(row, effort_source=self.controller.reasoning_effort_source, pending=None, touched=False, **state)
                    item["operation"] = {"kind": "model_choose", "ref": row["ref"], "levels": list(row.get("supported_efforts") or []),
                                         "selected": preselected_effort(row, **state), "keep": keep, "commit": commit,
                                         "remembered": "remembered_effort" in row}
        elif name == "/effort":
            if argument:
                await self.client.select_reasoning_effort(session, None if argument == "default" else argument)
                await self.controller.refresh_agent_metadata()
            else:
                self.picker("Reasoning effort", [{"name": level, "id": level} for level in ["default", *self.controller.supported_levels]], "/effort", "id")
        elif name == "/agent":
            if argument == "reset":
                await self.controller.reset_agent()
                await self.refresh_preview(session)
            elif argument == "current":
                self.show("Current agent", await self.client.current_agent(session))
            elif argument and argument != "list":
                await self.select_agent(argument)
            else:
                self.picker("Agents", root_agents(await self.client.list_agents()), "/agent", "name")
        elif name == "/context":
            from ...ui_support.context import context_summary, context_detail_usage, context_turn_usage, context_groups
            error = None
            try:
                preview = await self.client.inspect_context(session)
            except Exception as exc:
                preview = None
                error = str(exc)
            lines = [context_summary(preview, context_detail_usage(self.controller.view), error=error),
                     context_turn_usage(self.controller.view)]
            if preview is not None:
                from datetime import datetime
                self.preview_at = datetime.now().astimezone()
                self.preview = preview
                for group in context_groups(preview):
                    lines.extend(("", f"{group.title} · ~{group.tokens} tokens · {group.detail}"))
                    if group.key == "request":
                        lines.extend(labelled({"Accounting": preview.request_context or preview.budget,
                                               "Model parameters": preview.params, "System files": preview.system_files,
                                               "Display limitations": preview.omitted}))
                    else:
                        for entry in group.entries:
                            lines.extend(("", f"{entry.title} · ~{entry.tokens} tokens", entry.body))
            self.show("Context usage", "\n".join(lines))
        elif name == "/details":
            self.show("Session details", self.controller.view.to_dict())
        elif name == "/tools":
            if not self.controller.view.tools:
                self.notice = "No tools used in this transcript"
                return True
            self.workflows.menu("Tools", [(f"{tool.name} · {tool.status}", {"kind": "tool_page", "id": tool.call_id}) for tool in self.controller.view.tools])
        elif name == "/tasks":
            if not self.controller.view.agents:
                self.notice = "No background tasks"
                return True
            rows = []
            for agent in self.controller.view.agents.values():
                rows.append((f"{agent.id} · {agent.status} · {agent.task or agent.description}", {"kind": "agent_page", "id": agent.id}))
                if agent.status == "spawned":  # running: stop this one agent only, not the turn
                    rows.append((f"  ✕ Cancel {agent.id}", {"kind": "agent_cancel", "id": agent.id}))
            self.workflows.menu("Background tasks", rows)
        elif name in {"/mcp", "/skills"}:
            await self.workflows.context_extensions(name[1:])
        elif name == "/cost":
            self.show("Usage", self.controller.view.usage.to_dict())
        elif name == "/usage":
            self.open_usage()
        elif name == "/export":
            self.show("Session export", await self.client.export(session, format=argument or "markdown"))
        elif name == "/diff":
            refs = [arg for arg in args if arg != "--staged"]
            if len(refs) > 1:
                raise ValueError("Use /diff [--staged] [ref]")
            result = await self.client.git_diff(staged="--staged" in args, ref=refs[0] if refs else "")
            self.show("Git diff", (result.patch or "No changes") + ("\n\n[diff truncated]" if result.truncated else ""))
        elif name == "/worktrees":
            await self.workflows.operate({"kind": "worktrees"})
        elif name == "/archived":
            await self.workflows.operate({"kind": "archived", "query": argument})
        elif name == "/settings":
            self.workflows.settings_scope = argument if argument in {"project", "global"} else "global"
            await self.workflows.operate({"kind": "appearance"})
        elif name == "/reload":
            self.show("Extensions reloaded", await self.client.reload_extensions(trigger="chat"))
            self.mcp_due = 0.0
        elif name == "/attach":
            if argument == "clear":
                self.attachments.clear(); self.attachment_labels.clear()
                self.attachment_label_counters = {"image": 0, "document": 0}
            elif argument:
                if len(self.attachments) >= 8:
                    raise ValueError("At most eight attachments per message")
                generation = self.generation
                item = await self.client.prepare_attachment(path=argument.strip("\"'"))
                if generation != self.generation or len(self.attachments) >= 8:
                    return True  # the session changed while converting; never attach to another session
                self.attachments.append(item)
                self.composer_insert = self.attachment_marker(len(self.attachments)-1)
                if item.kind == "markdown":
                    self.workflows.menu("Attachment · " + item.name, [("Remove attachment", {"kind": "attachment_remove", "id": item.attachment_id})], labelled(item.preview))
            else:
                self.workflows.menu("Attachments", [(self.attachment_label(i) + " · " + item.name, {"kind": "attachment_preview", "id": item.attachment_id}) for i,item in enumerate(self.attachments)], labelled(self.attachments))
        elif name in {"/review", "/commit"}:
            await self.client.enqueue(session, "Review the current workspace changes for correctness and report findings with file references." if name == "/review" else "Review the current workspace changes, then create a commit for the completed work. Follow normal tool permissions.")
        elif name == "/speak":
            if args not in ((), ("download",)):
                self.notice = "Usage: /speak [download]"
            else:
                await self.workflows.speak_command(download_only=bool(args))
        elif name == "/voice":
            from ...ui_support.voice_settings import set_voice_config
            if argument in {"on", "off"}:
                await set_voice_config(self.client, enabled=argument == "on")
                if argument == "off":
                    await self.voice.discard()
                    self.notice = "Voice off"
                else:
                    await self.voice.open()
            elif argument == "status":
                self.show("Voice status", await self.client.voice_status())
            elif argument in {"", "download"}:
                await self.voice.open(download=argument == "download")
            else:
                raise ValueError("Use /voice [status|download|on|off]")
        elif name == "/live":
            await self.live.command(args)
        elif name == "/theme":
            requested = args[0].casefold() if args else "light" if self.preferences.values["theme"] == "nexus-dark" else "dark"
            if requested not in {"dark", "light"}:
                raise ValueError("Use /theme dark or /theme light")
            self.preferences.set("theme", "nexus-" + requested)
        elif name == "/verbose":
            self.verbose = not self.verbose
            self.notice = "Full tool output previews " + ("on" if self.verbose else "off")
        elif name == "/copy":
            import json
            from .desktop import copy_text
            if self.preview is None:
                raise ValueError("Context is unavailable")
            await copy_text(json.dumps({field: getattr(self.preview, field) for field in self.preview.__struct_fields__}, ensure_ascii=False, default=str))
            self.notice = "Copied context JSON"
        elif name == "/mock":
            if argument in {"", "list"}:
                self.show("Mock scenarios", await self.client.mock_list())
            elif argument == "clean":
                self.show("Mock cleanup", await self.client.mock_clean())
            else:
                result = await self.client.mock_start(args[0], speed=float(args[args.index("--speed")+1]) if "--speed" in args else 1, seed=int(args[args.index("--seed")+1]) if "--seed" in args else 0)
                await self.switch(result.session)
        else:
            raise ValueError(f"{name} is not implemented in the native shell yet")
        return True
