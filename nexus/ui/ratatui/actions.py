"""Host-backed native shell commands (Ratatui feasibility §7).

Transient panels contain labelled values; mutations remain daemon commands.
Unsupported UI journeys report an explicit error and never become model input.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from collections import OrderedDict
import uuid

from ..cli.commands import help_text, parse
from ...ui_support.tool_details import flatten


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


class ShellActions:
    """Session-scoped commands, attachments and transient inspection panels."""

    def __init__(self, controller):
        self.controller = controller
        self.panel_title = ""
        self.panel_lines = []
        self.panel_tones = []
        self.attachments = []
        self.attachment_labels = {}
        self.generation = 0
        self.items = []
        self.preview = None
        self.notice = ""
        self.composer_restore = ""
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
        self.sessions = []
        self.workspace = ""
        self.tabs = []
        self.breadcrumb = ""
        self.model_sort = "updated"  # model picker order: updated|name (Ctrl+S)
        self.update_notice = ""  # "<version> available: <command>" shown in the footer
        self.sessions_truncated = False  # the host list hit its cap
        self.archived_label = ""  # "Archived · N" under the sessions list, "" when none
        self.seen_seq: dict[str, int] = {}  # last sequence viewed per session, for "finished" status
        self.open_files: set[str] = set()  # modified files expanded in the details sidebar
        self.reconnect = None
        from .preferences import Preferences
        from .workflows import Workflows
        from .voice import Voice
        from .logs import Logs
        self.preferences = Preferences()
        self.workflows = Workflows(self)
        self.voice = Voice(self)
        self.logs = Logs(self)

    @property
    def client(self):
        return self.controller.client

    def show(self, title, value):
        self.items = []
        if hasattr(self, "workflows"):
            self.workflows.form = None
        self.panel_title = title
        self.panel_tones = []
        self.panel_lines = value.splitlines() if isinstance(value, str) else labelled(value)

    def show_styled(self, title, lines, tones):
        """A panel whose lines carry tones (``ui_support.tool_details.styled_lines``)."""
        self.show(title, "")
        self.panel_lines, self.panel_tones = list(lines), list(tones)

    def picker(self, title, rows, command, key):
        self.show(title, rows)
        self.items = [{"label": str(row.get("title") or row.get("name") or row.get(key)),
                       "command": f"{command} {row[key]}"} for row in rows if row.get(key)]

    async def switch_project(self, workspace, session):
        if workspace != self.workspace:
            result = await self.client.open_project_session(workspace, session)
            from ..cli import open_client
            client = await open_client(workspace, socket_path=result.socket_path)
            previous = self.controller.replace_client(client)
            self.workspace = workspace
            from pathlib import Path
            from ..cli import open_client
            self.reconnect = lambda: open_client(Path(workspace))
            await previous.aclose()
        await self.switch(session)

    async def switch(self, session):
        await self.voice.discard()
        self.workflows.form = None
        self.workflows.stack.clear()
        self.turn_cache.clear()
        self.turn_cache_bytes = 0
        self.generation += 1
        self.logs.reset_session()
        if not any(row["id"] == session and row["workspace"] == self.workspace for row in self.tabs):
            self.tabs.append({"id": session, "title": session, "workspace": self.workspace, "state": "idle"})
        self.attachments.clear(); self.attachment_labels.clear()
        self.items = []
        await self.controller.switch_session(session)
        await self.controller.bootstrap()
        await self.refresh_preview(session)
        self.panel_title = ""
        self.panel_lines = []

    async def refresh_preview(self, session: str | None = None) -> bool:
        """Reload the next-turn context preview; an active session has none yet.

        The host refuses a preview while a turn runs. That is expected, not an
        error: the header keeps its placeholders and the poll loop retries.
        """
        try:
            self.preview = await self.client.inspect_context(session or self.controller.session)
            return True
        except Exception as exc:  # noqa: BLE001 - only the "active" refusal is expected
            if "while the session is active" not in str(exc):
                raise
            self.preview = None
            return False

    async def cancel(self):
        result = await self.controller.cancel()
        self.composer_restore = "\n\n".join(result.returned_messages)

    async def submit(self, text, mode="queue"):
        command = parse(text)
        if command:
            return await self.command(command.name, command.args)
        try:
            await self.client.enqueue(self.controller.session, text, mode=mode,
                attachments=[item.attachment_id for item in self.attachments],
                attachment_labels=[self.attachment_label(index) for index in range(len(self.attachments))])
        except Exception:
            self.composer_restore = text
            raise
        from ...ui_support.tui_history import append_history
        append_history(text)
        self.attachments.clear(); self.attachment_labels.clear()
        return True

    def attachment_label(self, index):
        item = self.attachments[index]
        if item.attachment_id not in self.attachment_labels:
            kind = "image" if item.kind == "image" else "document"
            number = 1 + sum(label.startswith(f"{kind} ") for label in self.attachment_labels.values())
            self.attachment_labels[item.attachment_id] = f"{kind} {number}"
        return self.attachment_labels[item.attachment_id]

    async def command(self, name, args):
        session = self.controller.session
        argument = " ".join(args)
        if name == "/exit":
            return False
        if name == "/help":
            from ..cli.commands import SPECS
            self.items = []
            self.workflows.menu("Commands", [(spec.name + " · " + spec.summary, {"kind": "chat_command", "command": spec.name}) for spec in SPECS], help_text().splitlines())
        elif name == "/hotkeys":
            from ...ui_support.shortcuts import KEYBOARD_SHORTCUTS
            self.show("Keyboard shortcuts", "\n".join(KEYBOARD_SHORTCUTS))
        elif name == "/new":
            session_id = argument or uuid.uuid4().hex[:12]
            self.workflows.menu("New session · choose root agent", [(row["name"], {"kind": "new_session", "id": session_id, "agent": row["name"]}) for row in await self.client.list_agents()])
        elif name == "/sessions":
            if argument:
                rows = await self.client.list_sessions()
                matched = next((row for row in rows if row.id == args[0] or row.id.startswith(args[0])), None)
                if matched is None:
                    self.notice = f"No session matching {args[0][:80]}"
                else:
                    await self.switch(matched.id)
            else:
                await self.workflows.sessions()
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
                rows = [row for row in recent_models(await self.client.list_models(selectable_only=True))
                        if row.get("provider") and row.get("id")]
                for row in rows:
                    row["ref"] = f"{row['provider']}/{row['id']}"
                favorites = self.preferences.values["model_favorites"]
                recent = self.preferences.values["model_recent"]
                current = f"{self.controller.provider}/{self.controller.model}" if self.controller.provider and self.controller.model else ""
                groups, _ = model_groups(rows, sort_mode=self.model_sort, favorites=favorites, recent=recent)
                rows = []
                for title, group in groups:
                    for row in group:
                        mark = "★ " if row["ref"] in favorites else ""
                        here = " ◀" if row["ref"] == current else ""
                        rows.append({**row, "group": title, "name": f"{mark}{row.get('name') or row['ref']} · {row['ref']}{here}"})
                self.picker(f"Models · {'Updated ↓' if self.model_sort == 'updated' else 'Name A–Z'} · Ctrl+S sort · Ctrl+F favorite · Ctrl+R refresh", rows, "/model", "ref")
                for item, row in zip(self.items, rows):
                    item["group"] = row["group"]
                    state = dict(current=current, current_effort=self.controller.reasoning_effort, stored_override=self.controller.stored_override)
                    from ...ui_support.model_choice import preselected_effort, selection_effort
                    keep, commit = selection_effort(row, effort_source=self.controller.reasoning_effort_source, pending=None, touched=False, **state)
                    item["operation"] = {"kind": "model_choose", "ref": row["ref"], "levels": list(row.get("supported_efforts") or []),
                                         "selected": preselected_effort(row, **state), "keep": keep, "commit": commit}
        elif name == "/effort":
            if argument:
                await self.client.select_reasoning_effort(session, None if argument == "default" else argument)
                await self.controller.refresh_agent_metadata()
            else:
                self.picker("Reasoning effort", [{"name": level, "id": level} for level in ["default", *self.controller.supported_levels]], "/effort", "id")
        elif name == "/agent":
            if argument == "reset":
                await self.controller.reset_agent()
            elif argument == "current":
                self.show("Current agent", await self.client.current_agent(session))
            elif argument and argument != "list":
                await self.controller.select_agent(argument)
                await self.refresh_preview(session)
            else:
                self.picker("Agents", await self.client.list_agents(), "/agent", "name")
        elif name == "/context":
            self.show("Context", await self.client.inspect_context(session))
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
            self.workflows.menu("Background tasks", [(f"{agent.id} · {agent.status} · {agent.task or agent.description}", {"kind": "agent_page", "id": agent.id}) for agent in self.controller.view.agents.values()])
        elif name in {"/mcp", "/skills"}:
            await self.workflows.context_extensions(name[1:])
        elif name == "/cost":
            self.show("Usage", self.controller.view.usage.to_dict())
        elif name == "/usage":
            self.show("Provider usage", await self.client.providers_usage())
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
            await self.workflows.settings(argument if argument in {"project", "global"} else "global")
        elif name == "/reload":
            self.show("Extensions reloaded", await self.client.reload_extensions(trigger="chat"))
            self.mcp_due = 0.0
        elif name == "/attach":
            if argument == "clear":
                self.attachments.clear(); self.attachment_labels.clear()
            elif argument:
                if len(self.attachments) >= 8:
                    raise ValueError("At most eight attachments per message")
                generation = self.generation
                item = await self.client.prepare_attachment(path=argument.strip("\"'"))
                if generation != self.generation or len(self.attachments) >= 8:
                    return True  # the session changed while converting; never attach to another session
                self.attachments.append(item)
                self.composer_insert = self.attachment_label(len(self.attachments)-1)
                if item.kind == "markdown":  # Textual opens the converted preview immediately
                    self.workflows.menu("Attachment · " + item.name, [("Remove attachment", {"kind": "attachment_remove", "id": item.attachment_id})], labelled(item.preview))
            else:
                self.workflows.menu("Attachments", [(self.attachment_label(i) + " · " + item.name, {"kind": "attachment_preview", "id": item.attachment_id}) for i,item in enumerate(self.attachments)], labelled(self.attachments))
        elif name in {"/review", "/commit"}:
            await self.client.enqueue(session, "Review the current workspace changes for correctness and report findings with file references." if name == "/review" else "Review the current workspace changes, then create a commit for the completed work. Follow normal tool permissions.")
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
