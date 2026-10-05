"""Native interactive host workflows (Ratatui feasibility §7).

Picker operations are capabilities issued by Python, never arbitrary host calls.
Settings saves preserve hashes and keep unsaved bodies on conflict. All bodies
and previews come through the host; local files are used only for explicit export.
"""
from __future__ import annotations

from ...ui_support.completion import root_agents

import uuid

from ...ui_support.text import escape_controls
from .actions import labelled
from .speak_pages import SpeakPages
from .tier_pages import TierPages
from ...ui_support.tier_settings import TIERS_HELP, new_agent_template


def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


class Workflows(TierPages, SpeakPages):
    def __init__(self, shell):
        self.shell = shell
        self.form = None
        self.form_target = None
        self.login = None
        self.review = None
        self.review_files = []
        self.stack = []
        self.agent_page_id = None
        self.agent_context = {}
        self.agent_parents = []

    @property
    def client(self):
        return self.shell.client

    @property
    def selected_agent(self):
        def locate(body, depth=0):
            if depth > 8:
                return None
            for agent in body.agents.values():
                if agent.id == self.agent_page_id:
                    return agent
                found = locate(agent.body, depth + 1)
                if found is not None:
                    return found
            return None
        return locate(self.shell.controller.view) if self.agent_page_id else None

    @property
    def active_view(self):
        agent = self.selected_agent
        return agent.body if agent is not None else self.shell.controller.view

    def menu(self, title, rows, lines=(), layout="drawer"):
        if self.shell.settings_nav is not None:
            layout = "modal"
        if self.shell.panel_title and title != self.shell.panel_title:
            self.stack.append((self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form, self.form_target, self.shell.panel_layout, self.shell.panel_format, self.shell.panel_tones))
            self.stack = self.stack[-20:]
        self.shell.show(title, list(lines), layout=layout)
        self.shell.panel_lines = list(lines)
        self.shell.panel_tones = []
        self.shell.items = [{"label": label, "command": "", "operation": operation,
                             **operation.get("_presentation", {})}
                            for label, operation in rows]
        if self.shell.settings_nav is not None:
            for item in self.shell.items:
                item.setdefault("name", item["label"].split(" · ", 1)[0])
                item.setdefault("value", item["label"].partition(" · ")[2])
                item.setdefault("scope", "Local" if self.shell.settings_nav in {"appearance", "layout"} else self.settings_scope.title())
                item.setdefault("description", "")
                item.setdefault("status", "")
                item.setdefault("tone", "")
                item.setdefault("changed", False)
        self.form = None

    def edit(self, title, body, target, *, secret=False, autosave=False, replace=False):
        """Open an editor; Escape returns to the menu that opened it."""
        if self.shell.panel_title and not replace:
            self.stack.append((self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form, self.form_target, self.shell.panel_layout, self.shell.panel_format, self.shell.panel_tones))
            self.stack = self.stack[-20:]
        self.shell.show(title, "", layout="modal" if self.shell.settings_nav is not None else "page")
        self.form_target = target
        self.form = {"id": uuid.uuid4().hex, "body": body, "secret": secret,
                     "autosave": autosave, "status": "", "revision": 0, "saved": True}

    def back(self):
        if not self.shell.panel_title and self.agent_page_id:
            self.agent_page_id, self.agent_context = self.agent_parents.pop() if self.agent_parents else (None, {})
            return
        if self.stack:
            self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form, self.form_target, self.shell.panel_layout, self.shell.panel_format, self.shell.panel_tones = self.stack.pop()
            self.shell.panel_revision += 1
            self.shell.panel_loading = False
        else:
            self.shell.panel_revision += 1
            self.shell.panel_loading = False
            self.shell.panel_title = ""
            self.shell.panel_lines = []
            self.shell.items = []
            self.form = None
            self.shell.settings_nav = None

    async def settings_menu(self, scope="global", category=""):
        """Title, rows and lines of one Settings page (also used to refresh stale stack entries)."""
        if category == "speech":
            await self.speech_settings()
            return "Speech · local Kokoro", self.shell.items, self.shell.panel_lines
        if category == "agents":
            scope = "global"
        inventory = await self.client.settings_inventory(scope)
        if not category:
            rows = [(item.label, {"kind": "settings", "scope": scope, "category": item.key})
                    for item in inventory.categories if item.key not in {"config", "soul", "hooks"}]
            rows += [("Providers", {"kind": "providers"}), ("Models", {"kind": "models_settings"}),
                     ("Session titles", {"kind": "title_settings"}), ("Voice", {"kind": "voice_settings"}),
                     ("Speech · local Kokoro", {"kind": "speech_settings"}),
                     ("Appearance", {"kind": "appearance"}),
                     ("Layout", {"kind": "layout"}),
                     ("Keyboard", {"kind": "keyboard"}),
                     ("Switch to project" if scope == "global" else "Switch to global",
                      {"kind": "settings", "scope": "project" if scope == "global" else "global"})]
        else:
            items = [item for item in inventory.items if item.category == category]
            order = {"build": 0, "orchestrator": 1, "advisor": 2, "task": 3, "quick": 4}
            if category == "agents":  # build first, built-in subagents, then custom
                items.sort(key=lambda item: (order.get(item.id, 9), item.id.casefold()))
            rows = [(escape_controls(item.label) + (" · built-in" if item.builtin else " · edited" if getattr(item, "overrides_builtin", False) else ""),
                     {"kind": "settings_read", "scope": scope, "category": category, "id": item.id})
                    for item in items]
            if category == "mcp":
                preview = await self.client.inspect_context(self.shell.controller.session)
                server_rows = [row for row in preview.mcp_servers if row.get("scope") == scope]
                rows = [(f"{row['name']} · {row.get('config_tool_loading', 'search')} · {row.get('tool_count', 0)} tools · {scope} · {row.get('status', 'unknown')}",
                    {"kind": "settings_mcp_loading", "scope": scope, "name": row["name"], "tokens": row.get("schema_tokens", 0)}) for row in server_rows] + rows
            if category == "agents":
                rows.insert(0, ("New sessions start with…", {"kind": "default_agent"}))
            names = [item.id for item in items if not item.builtin and (category != "agents" or getattr(item, "overrides_builtin", False))]
            rows += [("New file", {"kind": "settings_new", "scope": scope, "category": category}),
                     *([] if category == "agents" else [("Switch to project" if scope == "global" else "Switch to global",
                       {"kind": "settings", "scope": "project" if scope == "global" else "global", "category": category})]),
                     ("Reset category…", {"kind": "confirm", "label": "Reset this category to default? Removed files move to trash.",
                      "lines": names, "next": {"kind": "settings_reset", "scope": scope, "category": category}})]
        from ...ui_support.settings_help import SETTINGS_HELP
        help_text = SETTINGS_HELP.get(category, "")
        return f"Settings · {scope} · {category or 'sections'}", rows, [inventory.root_display, *([help_text] if help_text else [])]

    async def settings(self, scope="global", category=""):
        self.shell.settings_nav = category
        self.settings_scope = scope
        title, rows, lines = await self.settings_menu(scope, category)
        self.menu(title, rows, lines)

    async def refresh_settings_pages(self, scope, category):
        """Rebuild stacked Settings pages so Back never shows deleted or missing files."""
        if category == "agents":
            scope = "global"
        for index, entry in enumerate(self.stack):
            if entry[0] == f"Settings · {scope} · {category}":
                title, rows, lines = await self.settings_menu(scope, category)
                self.stack[index] = (title, lines, [{"label": label, "command": "", "operation": op} for label, op in rows], None, None, "modal", "plain", [])

    async def return_to_settings(self, scope, category):
        """After a mutation, drop transient confirm/editor pages and show a fresh category list."""
        title = f"Settings · {'global' if category == 'agents' else scope} · {category}"
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == title:
                self.stack = self.stack[:index]
                break
        self.shell.panel_title = ""
        await self.settings(scope, category)

    async def providers(self):
        result = await self.client.providers_status()
        self.menu("Providers", [(row.get("label", row["id"]) + (" · connected" if row.get("connected") else " · not connected"),
            {"kind": "provider", "id": row["id"]}) for row in result.providers], ["Choose a provider to manage its connection and sign-in options.", "Credentials stay in the daemon; connect more than one provider to switch models."])

    async def voice_settings(self):
        """Configure local transcription without starting microphone capture."""
        status = await self.client.voice_status()
        rows = [
            (f"Voice input · {'on' if status.enabled else 'off'}", {"kind": "voice_setting", "key": "enabled", "value": not status.enabled}),
            (f"Send transcript automatically · {'on' if status.auto_send else 'off'}", {"kind": "voice_setting", "key": "auto_send", "value": not status.auto_send}),
            (f"Processing device · {status.configured_device}", {"kind": "voice_choices", "key": "device", "current": status.configured_device}),
            (f"Recording limit · {status.max_seconds} seconds", {"kind": "voice_choices", "key": "max_seconds", "current": status.max_seconds}),
        ]
        if status.state in {"loading", "downloading"}:
            rows.append(("Refresh model status", {"kind": "voice_settings"}))
        elif status.cached or status.state in {"ready", "transcribing"}:
            rows.append(("Load / prepare local model", {"kind": "voice_settings_prepare", "allow_download": False}))
        else:
            rows.append(("Download local model…", {"kind": "confirm", "label": "Download the local voice model (~179 MB)?", "next": {"kind": "voice_settings_prepare"}}))
        self.menu("Voice · local dictation", rows, [
            f"Model: {status.state} · {status.message or 'Audio is transcribed locally.'}",
            "Auto-send submits immediately; turn it off to review the transcript in the composer.",
            *labelled(status),
        ])
        for index, item in enumerate(self.shell.items):
            item["group"] = "Input and transcript" if index < 2 else "Local transcription" if index < 4 else "Model setup"

    async def speech_settings(self):
        """Show saved local Kokoro /speak configuration and the model's download state."""
        from ...ui_support import speech_download as sd
        from ...ui_support.speech_settings import read_speech_settings
        values = await read_speech_settings(self.client)
        status = await self.client.speech_status()
        rows = []
        for key, label in (("language", "Language"), ("voice", "Voice"), ("speed", "Speed"), ("device", "Device")):
            rows.append((f"{label} · {values[key]}", {"kind": "speech_choices", "key": key, "current": values[key]}))
        rows.append(("Reset to default", {"kind": "speech_reset"}))
        state = status.state
        if state in {"absent", "error"}:
            rows.append(("Download speech model…", {"kind": "confirm", "label": f"{sd.CONSENT_TITLE}. Download it now?",
                                                    "lines": [sd.CONSENT_PROMPT], "next": {"kind": "speak_settings_prepare"}}))
        elif state == "downloading":
            rows.append(("Refresh download status", {"kind": "speech_settings"}))
        model_line = (sd.progress_text(status) if state == "downloading" else sd.ready_text() if state == "ready"
                      else status.message or "The speech model is not downloaded yet.")
        self.menu("Speech · local Kokoro", rows, [
            "/speak generates speech locally using Kokoro. Settings are saved to nexus.toml [speech].",
            "No automatic downloads: the model is fetched only after you confirm the download.",
            f"Model: {model_line}",
        ])
        for item in self.shell.items:
            item["group"] = "Kokoro speech"

    #: Operation kinds that open or navigate Settings pages, and the area each selects.
    NAV_AREAS = {"appearance": "appearance", "layout": "layout", "keyboard": "keys",
                 "providers": "providers", "voice_settings": "voice", "speech_settings": "speech",
                 "models_settings": "models", "title_settings": "titles"}

    async def settings_area(self, key):
        """Switch Settings to ``key`` (the left list): a fresh page with Escape closing Settings."""
        self.stack.clear()
        self.shell.panel_title = ""
        operation = {"keys": {"kind": "keyboard"}, "providers": {"kind": "providers"}, "voice": {"kind": "voice_settings"}, "speech": {"kind": "speech_settings"},
                     "models": {"kind": "models_settings"}, "titles": {"kind": "title_settings"}}.get(
            key, {"kind": key} if key in ("appearance", "layout", "workspace") else {"kind": "settings", "scope": self.settings_scope, "category": key})
        await self.operate(operation)

    settings_scope = "global"

    async def operate(self, operation):
        kind = operation["kind"]
        if kind in self.NAV_AREAS:
            self.shell.settings_nav = self.NAV_AREAS[kind]
        elif kind == "settings":
            self.settings_scope = operation.get("scope", "global")
            self.shell.settings_nav = operation.get("category", "")
        if kind.startswith(("tier_", "models_", "title_", "agent_tier")) and await self.tier_operate(operation):
            return
        if kind.startswith("speak_") and await self.speak_operate(operation):
            return
        if kind == "close_panel":
            self.shell.settings_nav = None
            self.shell.panel_title = ""
            self.shell.panel_format = "plain"
            self.shell.preview_image = b""
            self.shell.preview_image_media = ""
        elif kind == "back":
            self.back()
        elif kind == "discard_form":
            self.stack.clear()
            self.shell.panel_title = ""
            self.shell.panel_lines = []
            self.shell.items = []
            self.form = None
        elif kind == "chat_command":
            self.shell.panel_title = ""
            self.shell.items = []
            return await self.shell.submit(operation["command"])
        elif kind == "model_choose":
            ref, levels, selected = operation["ref"], operation["levels"], operation.get("selected")
            if operation.get("remembered"):
                return await self.operate({"kind": "model_commit", "ref": ref, "effort": selected})
            if not levels:
                self.shell.panel_title = ""
                self.shell.items = []
                return await self.shell.submit("/model " + ref)
            mark = lambda value: " · selected" if value == selected else ""  # noqa: E731
            keep = ({"kind": "model_commit", "ref": ref, "effort": operation["keep"]} if operation.get("commit")
                    else {"kind": "chat_command", "command": "/model " + ref})
            self.menu("Model · reasoning effort", [
                ("Keep current effort", keep),
                ("Default effort" + mark(None), {"kind": "model_commit", "ref": ref, "effort": None}),
                *[(level + mark(level), {"kind": "model_commit", "ref": ref, "effort": level}) for level in levels]])
        elif kind == "model_commit":
            await self.shell.controller.select_model_and_effort(operation["ref"], operation["effort"])
            self.shell.preview = await self.client.inspect_context(self.shell.controller.session)
            recent = self.shell.preferences.values["model_recent"]
            self.shell.preferences.set("model_recent", [operation["ref"], *[ref for ref in recent if ref != operation["ref"]]])
            self.shell.panel_title = ""
            self.shell.items = []
        elif kind == "context_menu":
            self.menu("Context sections", [(label, {"kind": "context_show", "key": key}) for key,label in
                (("system","System prompt"),("tools","Tools"),("agents","AGENTS.md"),("skills","Skills"),("mcp","MCP"))])
        elif kind == "new_session":
            await self.shell.switch(operation["id"])
            await self.shell.select_agent(operation["agent"])

        elif kind == "archived":
            result = await self.client.list_archived_sessions(operation.get("query", ""))
            self.menu("Archived sessions", [(str(field(row, "title") or field(row, "id")), {"kind": "archived_preview", "id": field(row, "id")}) for row in result.sessions], labelled(result))
        elif kind == "archived_preview":
            result = await self.client.preview_session(operation["id"])
            self.menu("Archived preview", [("Resume", {"kind": "session_unarchive", "id": operation["id"]})], labelled(result))
        elif kind == "settings":
            await self.settings(operation.get("scope", "global"), operation.get("category", ""))
        elif kind == "settings_read":
            result = await self.client.settings_read(operation["scope"], operation["category"], operation["id"])
            overrides = bool(getattr(result, "overrides_builtin", False))
            target = {**operation, "sha256": result.sha256, "builtin": bool(result.builtin), "overrides_builtin": overrides}
            if operation["category"] == "agents" and result.body.lstrip("\ufeff").startswith("---"):
                self.agent_draft = {"target": target, "body": result.body, "path": result.rel_path}
                await self.load_tier_cache(result.body)
                self.agent_page()
            else:
                self.edit(result.rel_path, result.body, target, autosave=True)
                self.builtin_note()
        elif kind == "settings_new":
            self.edit("New file name · Enter/Control+S to continue", "", operation)
        elif kind == "settings_reset":
            await self.client.settings_reset(operation["scope"], operation["category"])
            await self.return_to_settings(operation["scope"], operation["category"])
            self.shell.notice = "Reset to default"
        elif kind == "settings_delete":
            if operation.get("builtin"):
                raise ValueError("Built-in defaults cannot be deleted; edit and save to override them")
            await self.client.settings_delete(operation["scope"], operation["category"], operation["id"])
            await self.return_to_settings(operation["scope"], operation["category"])
            self.shell.notice = "Reset to built-in default" if operation.get("overrides_builtin") else "Deleted to trash"
        elif kind == "confirm":
            label, lines = operation["label"], list(operation.get("lines") or [])
            following = operation["next"]
            if following.get("kind") == "settings_delete":  # Keep confirmation wording consistent
                if following.get("builtin"):
                    raise ValueError("Built-in defaults cannot be deleted; edit and save to override them")
                label = (f"Reset {following['id']} to the built-in default? Your edits move to trash."
                         if following.get("overrides_builtin") else f"Delete {following['id']} to trash?")
            self.menu(label, [("Cancel", {"kind": "back"}), ("Continue", following)], lines)
        elif kind == "providers":
            await self.providers()
        elif kind == "provider":
            await self.provider_page(operation["id"])
        elif kind == "provider_key":
            self.edit("API key · Control+S to save", "", operation, secret=True)
        elif kind == "provider_resume":
            self.login = login_result(operation["login"])
            await self.login_screen()
        elif kind == "provider_login":
            self.login = await self.client.provider_login(operation["id"], operation.get("method", ""))
            await self.login_screen()
        elif kind == "provider_poll":
            self.login = await self.client.provider_login_poll(self.login.login_id)
            if self.login.status != "pending":  # finished: show the host message, then fresh provider state
                await self.provider_page(self.login.provider, message=self.login.message or self.login.status)
            else:
                await self.login_screen()
        elif kind == "provider_code":
            self.edit("Paste sign-in code · Control+S to submit", "", operation, secret=True)
        elif kind == "provider_cancel":
            login = self.login
            try:
                await self.client.provider_login_cancel(login.login_id)
            finally:  # an already-finished sign-in is not an error worth stranding the screen for
                self.login = None
            await self.provider_page(login.provider, message="Sign-in cancelled")
        elif kind == "provider_logout":
            result = await self.client.provider_logout(operation["id"])
            await self.provider_page(operation["id"], message=str(field(result, "message", "") or "Signed out"))
        elif kind == "default_agent":
            current = await self.client.default_agent()
            self.menu("Default root agent", [(row["name"] + (" · current" if row["name"] == current else ""),
                {"kind": "default_agent_save", "name": row["name"]}) for row in root_agents(await self.client.list_agents())])
        elif kind == "default_agent_save":
            await self.client.set_default_agent(operation["name"], "global")
            await self.operate({"kind": "default_agent"})
        elif kind == "layout":
            labels = {"sessions_sidebar": ("Sessions sidebar", "ctrl+b"), "details_sidebar": ("Details sidebar", "ctrl+l"),
                      "context_preview": ("Show context header", "")}
            rows = [(f"{labels[key][0]}{'  ' + labels[key][1] if labels[key][1] else ''} · {'on' if self.shell.preferences.values[key] else 'off'}",
                     {"kind": "toggle_pref", "key": key}) for key in labels]
            rows.append(("Reset to default", {"kind": "reset_prefs", "keys": list(labels), "then": "layout"}))
            self.menu("Layout", rows, ["Panels hide automatically on narrow terminals."])
        elif kind == "toggle_pref":
            key = operation["key"]
            self.shell.preferences.set(key, not self.shell.preferences.values[key])
            await self.operate({"kind": "layout"})
        elif kind == "reset_prefs":
            for key in operation["keys"]:
                self.shell.preferences.set(key, self.shell.preferences.DEFAULTS[key])
            await self.operate({"kind": operation["then"]})
        elif kind == "keyboard":
            from ...ui_support.shortcuts import KEYBOARD_SHORTCUTS
            self.menu("Keyboard", [("Back", {"kind": "back"})], KEYBOARD_SHORTCUTS)
        elif kind == "workspace":
            self.shell.show("Workspace", await self.client.doctor())
        elif kind == "provider_open":
            import asyncio
            import webbrowser
            if self.login.url.startswith("https://"):
                await asyncio.to_thread(webbrowser.open, self.login.url)
        elif kind == "appearance":
            current = self.shell.preferences.values["theme"]
            self.menu("Appearance", [("Dark" + (" · selected" if current == "nexus-dark" else ""), {"kind": "theme", "value": "nexus-dark"}),
                                     ("Light" + (" · selected" if current == "nexus-light" else ""), {"kind": "theme", "value": "nexus-light"}),
                                     ("Reset to default", {"kind": "reset_prefs", "keys": ["theme"], "then": "appearance"})],
                      ["Theme for this terminal shell."])
        elif kind == "theme":
            self.shell.preferences.set("theme", operation["value"])
            self.shell.notice = "Theme saved"
            await self.operate({"kind": "appearance"})
        elif kind == "context_show":
            self.shell.settings_nav = None
            if self.agent_page_id:
                from ...host.protocol import ContextInspectResult
                fields = set(ContextInspectResult.__struct_fields__) - {"session"}
                preview = ContextInspectResult(session=self.agent_page_id, **{k: v for k, v in self.agent_context.items() if k in fields})
            else:
                fresh = await self.shell.refresh_preview()
                preview = self.shell.preview
                if preview is None:
                    self.shell.notice = "Context details are unavailable while a turn is running and no successful preview is cached"
                    return
                if not fresh:
                    self.shell.notice = "Showing the last successful context preview from before the running turn"
            key = operation["key"]
            if key == "system":
                from ...ui_support.context import header_system_prompt
                self.shell.show("System prompt · literal", header_system_prompt(preview) or "(empty)", layout="context")
            elif key == "agents":
                parts = [part for part in preview.included_parts if part.get("name") == "agents_md"]
                source = field(preview.system_files.get("agents", {}), "source", "AGENTS.md") or "AGENTS.md"
                body = "\n\n---\n\n".join(str(part.get("text") or "") for part in parts)
                if field(preview.system_files.get("agents", {}), "truncated", False):
                    body += "\n\n---\n\nContent truncated by the host."
                self.shell.show(f"AGENTS.md · {source}", body or "No AGENTS.md content is included in this request.", layout="context", format="markdown")
            elif key == "tools":
                from ...ui_support.context import tool_groups
                self.tools_expanded = {group.key for group in tool_groups(list(preview.tools))}
                self.tools_modal()
            elif key in {"skills", "mcp"}:
                await self.context_extensions(key)
            else:
                self.shell.show("Current request", preview)
        elif kind == "agent_edit_file":
            draft = self.agent_draft
            self.edit(draft["path"], draft["body"], draft["target"], autosave=True)
            self.builtin_note()
        elif kind == "agent_pick":
            await self.agent_model_picker(operation["field"], operation.get("index", 0))
        elif kind == "agent_mode":
            await self.agent_mode_switch(operation["mode"])
        elif kind == "agent_set":
            await self.agent_write(operation["field"], operation.get("index", 0), operation["ref"])
        elif kind == "agent_clear":
            await self.agent_write(operation["field"], operation.get("index", 0), "")
        elif kind == "tools_toggle":
            self.tools_expanded ^= {operation["key"]}
            self.tools_modal()
        elif kind == "tool_definition":
            from ...ui_support.context import _compact_tokens, tool_groups
            group = next(g for g in tool_groups(list(self.shell.preview.tools)) if g.key == operation["group"])
            entry = group.entries[operation["index"]]
            self.menu(f"Tool · {entry.title} · ~{_compact_tokens(entry.tokens)} tokens", [("Back", {"kind": "back"})], entry.body.splitlines(), layout="context")
        elif kind == "context_toggle":
            self.shell.preview = await self.client.select_context_extension(self.shell.controller.session,
                operation["category"], operation["name"], operation["enabled"])
            if operation["category"] == "tools":
                self.shell.panel_title = ""  # same dialog, new counts: replace it rather than stacking
                self.tools_modal()
            else:
                self.shell.panel_title = ""  # refresh without growing the Back stack
                await self.context_extensions(operation["category"])
        elif kind == "tool_definitions":
            from ...ui_support.context import tool_groups
            groups = tool_groups(list(self.shell.preview.tools))
            self.menu("Tool definitions", [(f"{g.title.removeprefix('MCP · ')} · {e.title}", {"kind": "tool_definition", "group": g.key, "index": i})
                                           for g in groups for i, e in enumerate(g.entries)], layout="context")
        elif kind == "context_extension_details":
            preview = self.shell.preview
            rows = preview.skills_index if operation["category"] == "skills" else preview.mcp_servers
            row = next((row for row in rows if (row.get("name") or row.get("id")) == operation["name"]), {})
            self.menu(str(operation["name"]), [("Back", {"kind": "back"})], labelled(row), layout="context")
        elif kind == "context_extensions":
            await self.context_extensions(operation["category"])
        elif kind == "agent_page":
            session, generation = self.shell.controller.session, self.shell.generation
            result = await self.client.agent_transcript(session, operation["id"])
            if self.shell.controller.session != session or self.shell.generation != generation:
                return
            if not result.get("found"):
                self.shell.notice = "Subagent transcript is unavailable"
                return
            self.agent_parents.append((self.agent_page_id, self.agent_context))
            self.agent_parents = self.agent_parents[-8:]
            self.agent_page_id = operation["id"]
            self.agent_context = result.get("context") or {}
            self.shell.panel_title = ""
            self.shell.items = []
        elif kind == "attachment_preview":
            item = next((item for item in self.shell.attachments if item.attachment_id == operation["id"]), None)
            if item is None:
                raise ValueError("Attachment is no longer attached")
            if item.kind == "image":
                preview = await self.client.preview_attachment(item.attachment_id)
                self.shell.preview_image = preview.data
                self.shell.preview_image_media = preview.media_type
                self.shell.preview_image_attachment_id = item.attachment_id
                self.shell.panel_title = "Attachment · " + item.name
                self.shell.panel_format = "image"
                self.shell.panel_lines = [f"Name: {item.name}", f"Media type: {preview.media_type}",
                    f"Size: {len(preview.data):,} bytes"]
                self.shell.items = [{"label": "Remove attachment", "command": "", "operation": {"kind": "attachment_remove", "id": item.attachment_id}}]
            else:
                self.menu("Attachment · " + item.name, [("Remove attachment", {"kind": "attachment_remove", "id": item.attachment_id})], labelled(item.preview))
        elif kind == "attachment_remove":
            # Labels stay assigned, so the remaining references (and their numbers) keep their meaning.
            self.shell.marker_attachments.pop(operation["id"], None)
            self.shell.attachments = [item for item in self.shell.attachments if item.attachment_id != operation["id"]]
            if self.shell.preview_image_attachment_id == operation["id"]:
                self.shell.preview_image = b""
                self.shell.preview_image_media = ""
                self.shell.preview_image_attachment_id = ""
            self.shell.panel_title = ""
            self.shell.attachment_labels.pop(operation["id"], None)
            self.stack.clear()
            await self.shell.command("/attach", ())
        elif kind == "submitted_image":
            from ...ui_support.native_images import decode_image_url
            message = next(message for turn in self.active_view.turns for message in turn.messages if message.id == operation["id"])
            value = decode_image_url(message.blocks[operation["index"]].image_url)
            if value is None:
                raise ValueError("Image preview is unavailable")
            media, data = value
            self.shell.show("Submitted image", {"Media type": media, "Size": f"{len(data):,} bytes"})
            self.shell.preview_image, self.shell.preview_image_media = data, media
            self.shell.panel_format = "image"
        elif kind == "message_page":
            message = next(message for turn in self.active_view.turns for message in turn.messages if message.id == operation["id"])
            from ...ui_support.native_images import decode_image_url
            body = message.to_dict()
            images = []
            for index, block in enumerate(message.blocks):
                if block.kind == "image" and (value := decode_image_url(block.image_url)):
                    media, data = value
                    images.append((index, media, data))
                    body["blocks"][index]["image_url"] = f"Embedded {media} · {len(data):,} bytes · preview available"
            self.shell.show("Submitted message · every content block", body)
            if images:
                _, media, data = images[0]
                self.shell.preview_image, self.shell.preview_image_media = data, media
                self.shell.panel_format = "image"
                self.shell.items = [{"label": f"Preview image {index + 1}", "command": "",
                    "operation": {"kind": "submitted_image", "id": message.id, "index": index}} for index, _, _ in images]
        elif kind == "turn_toggle":
            if operation["id"] in self.shell.collapsed_turns:
                self.shell.collapsed_turns.remove(operation["id"])
            else:
                self.shell.collapsed_turns.add(operation["id"])
        elif kind == "tool_page":
            from ...ui_support.tool_details import styled_lines, tool_detail_sections
            tool = next(tool for tool in self.active_view.tools if tool.call_id == operation["id"])
            self.shell.show_styled(tool.name, *styled_lines(tool_detail_sections(tool)))
        elif kind == "session_open":
            await self.shell.switch_project(operation["workspace"], operation["id"])
        elif kind == "session_archive":
            await self.client.archive_session(operation["id"])
            await self.sessions()
        elif kind == "session_unarchive":
            await self.client.unarchive_session(operation["id"])
            await self.shell.switch(operation["id"])
        elif kind == "session_delete":
            trash_id, _ = await self.client.delete(operation["id"])
            self.menu("Session moved to trash", [("Undo", {"kind": "session_restore", "id": trash_id})])
        elif kind == "session_restore":
            session = await self.client.restore(operation["id"])
            await self.shell.switch(session)
        elif kind == "session_actions":
            self.menu("Session actions", [("Open", {"kind": "session_open", **{key: operation[key] for key in ('id', 'workspace')}}),
                ("Archive", {"kind": "session_archive", "id": operation["id"]}),
                ("Move to trash…", {"kind": "confirm", "label": "Move session to trash?", "next": {"kind": "session_delete", "id": operation["id"]}})])
        elif kind == "worktrees":
            result = await self.client.list_worktrees()
            self.review = None
            self.shell.panel_title = ""  # the list is a root page; never stack stale confirmations behind it
            self.stack.clear()
            note = ["Host worktree list has more entries."] if result.has_more else [f"{len(result.worktrees)} daemon-owned worktree(s)."]
            self.menu("Worktrees", [(f"{row.get('lifecycle') or row.get('status') or 'unknown'} · {row.get('child_id') or row.get('id')}",
                {"kind": "worktree_actions", "id": row.get("child_id") or row.get("id")}) for row in result.worktrees],
                note + labelled(result))
        elif kind == "worktree_force_discard":
            result = await self.client.discard_worktree(operation["id"], force=True, confirmation_token=operation.get("token", ""))
            if result.status == "requires_confirmation":
                self.confirm_worktree("discard", result, force=True, files=[], proceed={**operation, "token": result.confirmation_token})
            else:
                await self.worktree_outcome(result)
        elif kind == "worktree_actions":
            inspection = await self.client.inspect_worktree(operation["id"])
            record = field(inspection, "record", {}) or {}
            warning = ["WARNING: child worktree contains uncommitted changes."] if record.get("dirty") else []
            self.menu("Worktree actions", [("Review changes", {"kind": "worktree_review", "id": operation["id"]}),
                ("Force discard…", {"kind": "worktree_force_discard", "id": operation["id"]})], warning + labelled(inspection))
        elif kind == "worktree_review":
            await self.review_pages(operation["id"])
        elif kind == "worktree_ack":
            r = self.review
            if r is None:
                raise ValueError("Review a finalized child first")
            result = await self.client.acknowledge_worktree(r.child_id, r.review_id, r.digest)
            self.menu("Reviewed worktree", [("Integrate…", {"kind": "worktree_mutate", "action": "integrate", **self.review_binding()}),
                ("Discard…", {"kind": "worktree_mutate", "action": "discard", **self.review_binding()})],
                [f"Acknowledged exact digest {field(result, 'digest', r.digest)} · review {field(result, 'review_id', r.review_id)}"])
        elif kind == "worktree_mutate":
            r = self.review
            if r is None or operation.get("review_id") != r.review_id or operation.get("digest") != r.digest or operation.get("child_id") != r.child_id:
                raise ValueError("Confirmation expired: selection or review changed; request a fresh preview")
            token = operation.get("token", "")
            result = await (self.client.integrate_worktree(r.child_id, r.review_id, r.digest, confirmation_token=token)
                if operation["action"] == "integrate" else self.client.discard_worktree(r.child_id, review_id=r.review_id, confirmation_token=token))
            if result.status == "requires_confirmation":
                if not result.confirmation_token or result.child_id != r.child_id or result.operation != operation["action"]:
                    raise ValueError("Host preview is missing a matching operation, child, or confirmation token")
                self.confirm_worktree(operation["action"], result, force=False, files=self.review_files,
                                      proceed={**operation, "token": result.confirmation_token})
            else:
                await self.worktree_outcome(result)
        elif kind == "voice_settings":
            await self.voice_settings()
        elif kind == "speech_settings":
            await self.speech_settings()
        elif kind == "speech_choices":
            from ...ui_support.speech_settings import SPEECH_CHOICES, compatible_voices, read_speech_settings
            key = operation["key"]
            choices = SPEECH_CHOICES[key]
            if key == "voice":
                choices = compatible_voices(str((await read_speech_settings(self.client))["language"]))
            self.menu(f"Speech · {key.title()}", [
                (f"{value}{' · selected' if value == operation['current'] else ''}", {"kind": "speech_setting", "key": key, "value": value})
                for value in choices
            ], ["Choose a local Kokoro setting. This does not download a model."])
        elif kind == "speech_setting":
            from ...ui_support.speech_settings import set_speech_config
            await set_speech_config(self.client, **{operation["key"]: operation["value"]})
            if operation["key"] == "language":
                self.stack.clear()
                self.shell.panel_title = ""
            await self.speech_settings()
        elif kind == "speech_reset":
            from ...ui_support.speech_settings import reset_speech_config
            await reset_speech_config(self.client)
            await self.speech_settings()
        elif kind == "voice_choices":
            key = operation["key"]
            values = ("auto", "cpu", "mps", "cuda") if key == "device" else (15, 30, 60, 90, 120)
            self.menu("Processing device" if key == "device" else "Recording limit", [
                (str(value) + (" · selected" if value == operation["current"] else ""),
                 {"kind": "voice_setting", "key": key, "value": value}) for value in values],
                ["Auto chooses an available accelerator; CPU works without one." if key == "device" else "Recording stops at this limit. Press any key to finish earlier."])
        elif kind == "voice_setting":
            from ...ui_support.voice_settings import set_voice_config
            await set_voice_config(self.client, **{operation["key"]: operation["value"]})
            self.stack.clear()
            self.shell.panel_title = ""
            await self.voice_settings()
        elif kind == "voice_settings_prepare":
            await self.client.voice_prepare(allow_download=operation.get("allow_download", True))
            await self.voice_settings()
        elif kind == "voice":
            await self.shell.voice.open()
        elif kind == "voice_enable":
            from ...ui_support.voice_settings import set_voice_config
            await set_voice_config(self.client, enabled=True)
            await self.shell.voice.open()
        elif kind == "voice_start":
            await self.shell.voice.start()
        elif kind == "voice_prepare":
            await self.client.voice_prepare(allow_download=operation.get("allow_download", True))
            await self.shell.voice.open(prepare=False)
        elif kind == "setup":
            status = await self.client.setup_status()
            rows = [(f"Use {row.get('label', row['id'])} · newest model", {"kind": "setup_save", "provider": row["id"], "model": ""})
                    for row in status.providers if row.get("connected") is True and row.get("auto") is True]
            rows += [("Connect a provider…", {"kind": "providers"}), ("Choose a specific model…", {"kind": "setup_models"})]
            lines = [f"{'connected' if row.get('connected') is True else 'not connected'} · {row.get('label', row['id'])} · {row.get('instruction', '')}"
                     for row in status.providers if isinstance(row, dict) and row.get("id")]
            self.menu("Connect a provider", rows, ["Nexus starts with the newest model of the first provider you connect. Change it any time with /model."] + lines)
        elif kind == "setup_models":
            rows = await self.client.list_models(selectable_only=True)
            self.menu("Choose the default model", [(str(row.get("name") or row["id"]),
                {"kind": "setup_save", "provider": row["provider"], "model": row["id"]}) for row in rows])
        elif kind == "setup_save":
            result = await self.client.setup_save(operation["provider"], operation["model"])
            model = escape_controls(result.global_model)
            self.shell.show("Setup saved", "\n".join([f"Saved {model} as the default."] + (
                ["Restart the daemon (nexus daemon stop) to use it."] if result.restart_required else ["Using it now · /model to change"])))
            if not result.restart_required:
                await self.shell.controller.refresh_agent_metadata()
                self.shell.preview = await self.client.inspect_context(self.shell.controller.session)
        else:
            raise ValueError("Unknown native workflow")

    async def provider_page(self, provider_id, message=""):
        """One provider card: state, help, sign-in methods, resume of a pending sign-in, sign out."""
        result = await self.client.providers_status()
        provider = next((row for row in result.providers if row["id"] == provider_id), None)
        if provider is None:
            raise ValueError("Unknown provider")
        methods = [field(method, "id", method if isinstance(method, str) else "") for method in provider.get("methods", [])]
        rows = []
        login = provider.get("login")
        if login and field(login, "status") == "pending":
            rows.append(("Resume sign-in", {"kind": "provider_resume", "login": plain_login(login)}))
        if "api_key" in methods:
            rows.append(("Set API key", {"kind": "provider_key", "id": provider_id}))
        for method_id in methods:
            if method_id and method_id != "api_key":
                rows.append((METHOD_LABELS.get(method_id, f"Sign in · {method_id}"),
                             {"kind": "provider_login", "id": provider_id, "method": method_id}))
        if provider.get("connected") and provider.get("can_logout", True) is not False:
            rows.append(("Sign out…", {"kind": "confirm", "label": "Sign out of this provider?",
                                       "next": {"kind": "provider_logout", "id": provider_id}}))
        label = provider.get("label", provider_id)
        if self.shell.panel_title != "Providers":  # re-render in place: Back still reaches the provider list
            transient = {label, "Provider sign-in", "Sign out of this provider?"}
            self.stack = [entry for entry in self.stack if entry[0] not in transient]
            self.shell.panel_title = ""
        self.menu(provider.get("label", provider_id), rows, [
            f"Connection: {'connected' if provider.get('connected') else 'not connected'}" + (f" · {message}" if message else ""),
            provider.get("instruction") or "Choose a sign-in method below to manage this provider.",
            *labelled(provider)])
        for item in self.shell.items:
            item["group"] = "Connection management" if item["operation"]["kind"] == "confirm" else "Sign-in options"

    def review_binding(self):
        r = self.review
        return {"child_id": r.child_id, "review_id": r.review_id, "digest": r.digest}

    async def review_pages(self, child_id):
        """Load every review page for one pinned review identity."""
        import re
        first = await self.client.review_worktree(child_id, limit=REVIEW_PAGE_LIMIT)
        for value, pattern in ((first.review_id, r"[0-9a-f]{32}"), (first.digest, r"[0-9a-f]{64}")):
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ValueError("Host returned an invalid review identity or digest")
        files = [str(row.get("path", "")) for row in first.entries if isinstance(row, dict)]
        lines = [f"Changed files ({len(files)} reported):", *[f"  {path}" for path in files]] if files else []
        lines += labelled(first)
        result, pages, cursor = first, 1, first.cursor
        while result.has_more and pages < 1000:
            if not result.diff:
                raise ValueError("Host returned an empty review page with more pages")
            cursor = result.cursor + REVIEW_PAGE_LIMIT  # cursors count 128 KiB diff pages, a full page means `limit` consumed
            result = await self.client.review_worktree(child_id, review_id=first.review_id, cursor=cursor, limit=REVIEW_PAGE_LIMIT)
            pages += 1
            if result.review_id != first.review_id or result.digest != first.digest:
                self.review = None
                raise ValueError("Host review identity or digest changed; reload from the first page")
            if result.cursor != cursor:
                raise ValueError("Host returned a non-matching review page cursor")
            lines.append(f"-- page {pages} · file {cursor + 1} --")
            lines.extend(labelled(result.diff))
        if result.has_more:
            self.review = None
            self.shell.show("Worktree review", "\n".join(lines + ["[Review exceeds 1,000 pages; acknowledge unavailable]"]))
            return
        self.review, self.review_files = first, files
        self.menu("Worktree review · inspect all changes before acknowledging",
                  [("Acknowledge reviewed changes", {"kind": "worktree_ack"})], lines)

    def confirm_worktree(self, operation, preview, *, force, files, proceed):
        impact = field(preview, "impact", {}) or {}
        lines = [f"Operation: {operation}", f"Child: {field(preview, 'child_id', '')}"]
        if force:
            lines.append("WARNING: force discard removes the child worktree, including dirty files.")
        lines += [f"{key}: {impact[key]}" for key in ("parent_clean", "parent_head_matches_base", "child_dirty", "summary") if key in impact]
        lines.append("Changed files reported by the host review:")
        lines += [f"  {path}" for path in files] or (["  Dirty files are present; the host preview did not enumerate them."]
                                                     if impact.get("child_dirty") else ["  (none reported)"])
        lines.append("This confirmation authorizes the exact fresh preview shown above.")
        self.menu(f"Confirm {operation} · {field(preview, 'child_id', '')}", [("Cancel", {"kind": "worktrees"}),
            ("Confirm", proceed)], lines + labelled(impact))

    async def worktree_outcome(self, result):
        self.review = None  # the digest is consumed; a later mutation needs a fresh review
        self.stack.clear()
        self.shell.show("Worktree outcome", result)
        self.shell.items = [{"label": "Back to worktrees", "command": "", "operation": {"kind": "worktrees"}}]

    async def login_screen(self):
        rows = [("Open sign-in page", {"kind": "provider_open"}), ("Refresh sign-in status", {"kind": "provider_poll"}), ("Cancel", {"kind": "provider_cancel"})]
        if self.login.code_entry:
            rows.insert(0, ("Paste sign-in code", {"kind": "provider_code"}))
        self.menu("Provider sign-in", rows, labelled(self.login))

    async def save(self, form_id, body, revision):
        if not self.form or form_id != self.form["id"] or revision < self.form["revision"]:
            return
        self.form["revision"] = revision
        target = self.form_target
        if target["kind"] == "settings_read":
            self.form["body"] = body
            self.form["saved"] = False
            try:
                result = await self.client.settings_write(target["scope"], target["category"], target["id"], body,
                    expected_sha256=target["sha256"] or None)
            except Exception as exc:
                self.form["status"] = str(exc)
                self.form["autosave"] = False
                raise
            self.form["status"] = result.status
            self.form["saved"] = result.status not in {"conflict", "failed", "error"}
            if self.form["saved"]:
                self.form["status"] = f"Saved · {len(result.loaded)} loaded · {len(result.failed)} failed"
            if result.status == "conflict":
                self.form["autosave"] = False
                self.form["status"] = "Conflict: draft preserved. Reopen to load current file."
            elif result.sha256:
                target["sha256"] = result.sha256
            was_builtin = bool(target.get("builtin"))
            if self.form["saved"] and (target.pop("refresh", False) or was_builtin):
                target["builtin"] = False
                target["overrides_builtin"] = bool(target.get("overrides_builtin")) or was_builtin
                await self.refresh_settings_pages(target["scope"], target["category"])
        elif target["kind"] == "settings_new":
            if not body.strip():
                raise ValueError("Enter a file name")
            name = body.strip()
            self.edit("New file body · " + name, new_file_body(target["category"], name), {"kind": "settings_read", "scope": target["scope"],
                "category": target["category"], "id": name, "sha256": "", "builtin": False, "overrides_builtin": False,
                "refresh": True}, autosave=True, replace=True)
        elif target["kind"] == "provider_key":
            if not body.strip():
                raise ValueError("Paste your API key first")
            result = await self.client.provider_key_set(target["id"], body.strip())
            self.form = None
            message = field(result, "message", "")
            try:
                await self.provider_page(target["id"], message=message if isinstance(message, str) and message else "Key saved")
            except ValueError:  # provider vanished from status: show the list
                await self.providers()
        elif target["kind"] == "provider_code":
            if not body.strip():
                raise ValueError("Paste the code shown after signing in first")
            result = await self.client.provider_login_code(self.login.login_id, body.strip())
            self.form = None
            self.stack = [entry for entry in self.stack if entry[0] != "Provider sign-in"]
            self.shell.panel_title = ""
            await self.login_screen()
            self.shell.panel_lines.insert(0, str(field(result, "message", "")))

    tools_expanded: set = set()
    agent_draft: dict = {}

    def builtin_note(self):
        """A built-in default says that saving writes an override."""
        target = self.form_target or {}
        if self.form and target.get("builtin"):
            where = "~/.nexus" if target.get("scope") == "global" else "<project>/.agents"
            self.form["status"] = f"Built-in default · saving writes an override to {where}"

    def agent_page(self):
        """An agent file as form rows (model and fallbacks) beside the prompt file."""
        from ...ui_support.agent_frontmatter import MAX_FALLBACKS, agent_fields, fallback_items, run_mode
        draft = self.agent_draft
        fields = agent_fields(draft["body"])
        model, fallbacks = fields.get("model", ""), fallback_items(fields.get("fallback", ""))
        subagent = "subagent" in fields.get("contexts", "subagent")
        mode = run_mode(draft["body"])
        if mode == "session" and draft.get("mode"):
            mode = draft["mode"]  # chosen but nothing written yet
        label = {"session": "Session model", "model": "Specific model", "tier": "Tier"}[mode]
        rows = [(f"Run on · {label}", {"kind": "agent_mode", "mode": self.next_run_mode(mode, subagent)})]
        tier_rows, tier_notes = self.agent_tier_rows(draft["body"])
        if mode == "tier" and subagent:
            rows += tier_rows
        if mode == "model":
            rows.append((f"Model · {model or 'choose a model'}", {"kind": "agent_pick", "field": "model"}))
            if model:
                rows.append(("  × Clear model", {"kind": "agent_clear", "field": "model"}))
            for index, ref in enumerate(fallbacks):
                rows.append((f"Fallback {index + 1} · {ref}", {"kind": "agent_pick", "field": "fallback", "index": index}))
                rows.append((f"  × Remove fallback {index + 1}", {"kind": "agent_clear", "field": "fallback", "index": index}))
            if len(fallbacks) < MAX_FALLBACKS:
                rows.append(("+ Add fallback", {"kind": "agent_pick", "field": "fallback", "index": len(fallbacks)}))
        rows.append(("Edit prompt file…", {"kind": "agent_edit_file"}))
        help_lines = ["Run on: a specific model with ordered fallbacks, or "
                      + ("a tier, which picks a model from the connected providers. " if subagent else "the session model. ")
                      + "Choosing a mode removes the other mode's fields from the file."]
        if fields.get("model") and fields.get("tiers"):
            help_lines.append("This agent also lists tiers; choosing a mode removes the other.")
        for key in ("provider", "reasoning_effort"):
            if fields.get(key):
                help_lines.append(f"{key}: {fields[key]} (edit in the prompt file)")
        if mode == "tier" and subagent:
            help_lines.append("Tiers: " + TIERS_HELP)
            help_lines += tier_notes
        self.menu(f"Agent · {draft['target']['id']}", rows,
                  [*help_lines, str(fields.get("description", ""))] if fields.get("description") else help_lines)

    @staticmethod
    def next_run_mode(mode, subagent):
        modes = ["session", "model", "tier"] if subagent else ["session", "model"]
        return modes[(modes.index(mode) + 1) % len(modes)]

    async def agent_mode_switch(self, mode):
        """Switch run mode, keeping the discarded values so switching back restores them."""
        from ...ui_support.agent_frontmatter import agent_fields, run_mode, set_run_mode
        draft = self.agent_draft
        fields = agent_fields(draft["body"])
        stash = draft.setdefault("stash", {})
        draft["mode"] = mode
        stash[run_mode(draft["body"])] = {key: fields.get(key, "") for key in ("model", "fallback", "tiers")}
        await self.agent_save_body(set_run_mode(draft["body"], mode, stash.get(mode)))

    async def agent_model_picker(self, field, index):
        from ...ui_support.model_choice import model_groups, recent_models
        rows = [row for row in recent_models(await self.client.list_models(selectable_only=True)) if row.get("provider") and row.get("id")]
        groups, _ = model_groups(rows, favorites=self.shell.preferences.values["model_favorites"], recent=self.shell.preferences.values["model_recent"])
        items = [(f"{row.get('name') or row['id']} · {row['provider']}/{row['id']}", {"kind": "agent_set", "field": field, "index": index, "ref": f"{row['provider']}/{row['id']}"}, title)
                 for title, group in groups for row in group]
        self.menu("Agent model" if field == "model" else f"Agent fallback {index + 1}", [(label, op) for label, op, _ in items])
        for item, (_, _, title) in zip(self.shell.items, items):
            item["group"] = title

    async def agent_write(self, field, index, ref):
        """Set (or clear) one agent field and save the file at once,."""
        from ...ui_support.agent_frontmatter import agent_fields, fallback_items
        draft = self.agent_draft
        fields = agent_fields(draft["body"])
        if field == "model":
            updates = {"model": ref}
        else:
            current = fallback_items(fields.get("fallback", ""))
            if ref:
                current[index:index + 1] = [ref]
            elif index < len(current):
                del current[index]
            updates = {"fallback": ", ".join(current)}
        await self.agent_save(updates)

    async def agent_save(self, updates):
        """Write frontmatter ``updates`` to the agent file at once and re-show the agent page."""
        from ...ui_support.agent_frontmatter import set_agent_fields
        await self.agent_save_body(set_agent_fields(self.agent_draft["body"], updates))

    async def agent_save_body(self, body):
        draft = self.agent_draft
        target = draft["target"]
        result = await self.client.settings_write(target["scope"], target["category"], target["id"], body,
                                                  expected_sha256=target["sha256"] or None)
        if result.status in {"conflict", "failed", "error"}:
            self.shell.notice = "Conflict: the agent file changed on disk. Reopen it to load the current file." if result.status == "conflict" else f"Save failed · {result.status}"
        else:
            draft["body"] = body
            if result.sha256:
                target["sha256"] = result.sha256
            self.shell.notice = f"Saved · {len(result.loaded)} loaded · {len(result.failed)} failed"
            await self.refresh_settings_pages(target["scope"], target["category"])
            await self.load_tier_cache(body)
        if self.shell.panel_title.startswith("Agent ") and self.shell.panel_title != f"Agent · {target['id']}":
            self.back()
        self.agent_page()

    def tools_modal(self):
        """Expand families for definitions; right-side controls select session tools."""
        from ...ui_support.context import _compact_tokens, tool_groups
        preview = self.shell.preview
        groups = tool_groups(list(preview.tools))
        state = {str(row.get("name")): row.get("enabled") is not False for row in preview.tools if isinstance(row, dict)}
        locked = bool(getattr(preview, "context_locked", False)) or bool(self.agent_page_id)
        rows, toggles = [], []
        count = tokens = total = 0
        for group in groups:
            live = [entry for entry in group.entries if state.get(entry.title, True)]
            count += len(live)
            total += len(group.entries)
            tokens += sum(entry.tokens for entry in live)
            mark = "▾" if group.key in self.tools_expanded else "▸"
            rows.append((f"{mark} {group.title} · {len(live)}/{len(group.entries)} · ~{_compact_tokens(sum(e.tokens for e in live))} tokens",
                         {"kind": "tools_toggle", "key": group.key}))
            toggles.append(None)
            # Families start expanded; users may fold them explicitly.
            if group.key not in self.tools_expanded:
                continue
            for index, entry in enumerate(group.entries):
                enabled = state.get(entry.title, True)
                rows.append((f"    {entry.title} · {entry.detail[:60]} · ~{_compact_tokens(entry.tokens)}",
                             {"kind": "tool_definition", "group": group.key, "index": index}))
                toggles.append((enabled, {"kind": "context_toggle", "category": "tools", "name": entry.title, "enabled": not enabled}))
        rows.append(("Edit tools…", {"kind": "settings", "scope": "global", "category": "tools"}))
        toggles.append(None)
        note = ["Context locked after first turn" if locked else "Click a right-side toggle or press Space to switch a tool; Enter reads its definition."]
        if getattr(preview, "tools_supported", True) is False:
            note.append("The selected model does not support tools; none are sent.")
        self.menu(f"Tools · {count} of {total} definitions · ~{_compact_tokens(tokens)} tokens", rows,
                  note + ([] if groups else ["(none)"]), layout="context")
        for item, toggle in zip(self.shell.items, toggles):
            if toggle is not None:
                item.update(toggle_enabled=toggle[0], toggle_operation=toggle[1], toggle_locked=locked)

    async def context_extensions(self, category):
        self.shell.preview = await self.client.inspect_context(self.shell.controller.session)
        preview = self.shell.preview
        rows = [row for row in (preview.skills_index if category == "skills" else preview.mcp_servers) if row.get("config_enabled") is not False]
        locked = bool(getattr(preview, "context_locked", False)) or bool(self.agent_page_id)
        ordered = sorted(rows, key=lambda row: (row.get("scope", "global"), str(row.get("name") or row.get("id"))))
        items = []
        for row in ordered:
            name = row.get("name") or row.get("id")
            scope = row.get("scope", "project" if category == "mcp" else "global")
            items.append((f"{scope:<8} {name}", {"kind": "context_extension_details", "category": category, "name": name}))
        self.menu(category.upper(), items,
                  ["Context locked after first turn" if locked else "Click a right-side toggle or press Space to switch; Enter shows details."], layout="context")
        for item, row in zip(self.shell.items, ordered):
            enabled = row.get("enabled") is not False
            item.update(toggle_enabled=enabled, toggle_locked=locked,
                        toggle_operation={"kind": "context_toggle", "category": category,
                                          "name": row.get("name") or row.get("id"), "enabled": not enabled})

    async def sessions(self):
        result = await self.client.project_sessions()
        self.shell.sessions = session_rows(result, self.shell.controller.session, self.shell.seen_seq)
        self.shell.sessions_truncated = bool(result.truncated)
        self.menu("Sessions", [(f"{row['workspace']} · {row['title']} · {row['state']}",
            {"kind": "session_open", "id": row["id"], "workspace": row["workspace"]}) for row in self.shell.sessions],
            ["[Session list truncated]"] if result.truncated else [])


REVIEW_PAGE_LIMIT = 8
METHOD_LABELS = {"browser": "Sign in with browser", "device": "Use a device code"}


def plain_login(login):
    from .actions import plain
    return plain(login) if not isinstance(login, dict) else dict(login)


def login_result(login):
    from ...host import protocol as p
    return p.ProviderLoginResult(**{key: value for key, value in login.items() if key in p.ProviderLoginResult.__struct_fields__})


def new_file_body(category, name):
    """Starter text for a new settings file."""
    if category == "agents":
        return new_agent_template(name)
    if category == "skills":
        return f"---\nname: {name}\ndescription: Describe this skill.\n---\nInstructions.\n"
    return '{"mcpServers": {}}\n' if category == "mcp" else ""


def session_rows(result, current: str = "", seen: dict | None = None, now: float | None = None):
    """Project session cards: ``status``/``sub`` come from shared presentation helpers.

    ``seen`` records the last terminal-turn sequence viewed per session, so
    presence-only log activity cannot make a session appear newly finished.
    """
    from ...ui_support.session_groups import _session_groups
    from ...ui_support.session_status import session_status, session_subline
    from ...ui_support.text import escape_controls
    seen = {} if seen is None else seen
    groups = _session_groups([(row.workspace, row.session) for row in result.sessions], "")
    workspace_for = {id(row.session): row.workspace for row in result.sessions}
    rows = []
    for group, members in groups:
        for row in members:
            if row.id not in seen or row.id == current:
                seen[row.id] = max(
                    seen.get(row.id, 0),
                    getattr(row, "completion_seq", getattr(row, "last_seq", 0)),
                )
            status = session_status(row, seen, current)
            rows.append({"id": row.id, "title": escape_controls(row.title or "New Session"),
                         "workspace": workspace_for[id(row)], "state": row.state, "group": escape_controls(group),
                         "status": status, "sub": escape_controls(session_subline(row, status, now, compact=True)),
                         "active": row.id == current})
    return rows
