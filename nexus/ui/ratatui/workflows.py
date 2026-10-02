"""Native interactive host workflows (Ratatui feasibility §7).

Picker operations are capabilities issued by Python, never arbitrary host calls.
Settings saves preserve hashes and keep unsaved bodies on conflict. All bodies
and previews come through the host; local files are used only for explicit export.
"""
from __future__ import annotations

import uuid

from ...ui_support.text import escape_controls, redact
from .actions import labelled


def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


class Workflows:
    def __init__(self, shell):
        self.shell = shell
        self.form = None
        self.form_target = None
        self.login = None
        self.review = None
        self.review_files = []
        self.stack = []
        self.agent_page_id = None

    @property
    def client(self):
        return self.shell.client

    def menu(self, title, rows, lines=()):
        if self.shell.panel_title and title != self.shell.panel_title:
            self.stack.append((self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form, self.form_target))
            self.stack = self.stack[-20:]
        self.shell.show(title, list(lines))
        self.shell.panel_lines = list(lines)
        self.shell.items = [{"label": label, "command": "", "operation": operation}
                            for label, operation in rows]
        self.form = None

    def edit(self, title, body, target, *, secret=False, autosave=False, replace=False):
        """Open an editor; Escape returns to the menu that opened it (Textual keeps the list)."""
        if self.shell.panel_title and not replace:
            self.stack.append((self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form, self.form_target))
            self.stack = self.stack[-20:]
        self.shell.show(title, "")
        self.form_target = target
        self.form = {"id": uuid.uuid4().hex, "body": body, "secret": secret,
                     "autosave": autosave, "status": "", "revision": 0, "saved": True}

    def back(self):
        if self.stack:
            self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form, self.form_target = self.stack.pop()
        else:
            self.shell.panel_title = ""
            self.shell.panel_lines = []
            self.shell.items = []
            self.form = None

    async def settings_menu(self, scope="global", category=""):
        """Title, rows and lines of one Settings page (also used to refresh stale stack entries)."""
        if category == "agents":
            scope = "global"
        inventory = await self.client.settings_inventory(scope)
        if not category:
            rows = [(item.label, {"kind": "settings", "scope": scope, "category": item.key})
                    for item in inventory.categories]
            rows += [("Providers", {"kind": "providers"}), ("Voice", {"kind": "voice"}),
                     ("Appearance", {"kind": "appearance"}),
                     ("Layout", {"kind": "layout"}),
                     ("Keyboard", {"kind": "keyboard"}),
                     ("Workspace", {"kind": "workspace"}),
                     ("Switch to project" if scope == "global" else "Switch to global",
                      {"kind": "settings", "scope": "project" if scope == "global" else "global"})]
        else:
            items = [item for item in inventory.items if item.category == category]
            order = {"build": 0, "orchestrator": 1, "advisor": 2, "task": 3, "quick": 4}
            if category == "agents":  # build first, built-in subagents, then custom (Textual order)
                items.sort(key=lambda item: (order.get(item.id, 9), item.id.casefold()))
            rows = [(escape_controls(item.label) + (" · built-in" if item.builtin else " · edited" if getattr(item, "overrides_builtin", False) else ""),
                     {"kind": "settings_read", "scope": scope, "category": category, "id": item.id})
                    for item in items]
            if category == "agents":
                rows.insert(0, ("New sessions start with…", {"kind": "default_agent"}))
            names = [item.id for item in items if not item.builtin and (category != "agents" or getattr(item, "overrides_builtin", False))]
            rows += [("New file", {"kind": "settings_new", "scope": scope, "category": category}),
                     ("Reset category…", {"kind": "confirm", "label": "Reset this category to default? Removed files move to trash.",
                      "lines": names, "next": {"kind": "settings_reset", "scope": scope, "category": category}})]
        from ...ui_support.settings_help import SETTINGS_HELP
        help_text = SETTINGS_HELP.get(category, "")
        return f"Settings · {scope} · {category or 'sections'}", rows, [inventory.root_display, *([help_text] if help_text else [])]

    async def settings(self, scope="global", category=""):
        title, rows, lines = await self.settings_menu(scope, category)
        self.menu(title, rows, lines)

    async def refresh_settings_pages(self, scope, category):
        """Rebuild stacked Settings pages so Back never shows deleted or missing files."""
        if category == "agents":
            scope = "global"
        for index, entry in enumerate(self.stack):
            if entry[0] == f"Settings · {scope} · {category}":
                title, rows, lines = await self.settings_menu(scope, category)
                self.stack[index] = (title, lines, [{"label": label, "command": "", "operation": op} for label, op in rows], None, None)

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
        self.menu("Providers", [(row.get("label", row["id"]) + (" · connected" if row.get("connected") else ""),
            {"kind": "provider", "id": row["id"]}) for row in result.providers])

    async def operate(self, operation):
        kind = operation["kind"]
        if kind == "back":
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
            if not levels:
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
        elif kind == "new_session":
            await self.shell.switch(operation["id"])
            await self.shell.controller.select_agent(operation["agent"])
            self.shell.preview = await self.client.inspect_context(operation["id"])
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
            self.edit(result.rel_path, result.body, {**operation, "sha256": result.sha256, "builtin": bool(result.builtin),
                      "overrides_builtin": overrides}, autosave=True)
            if result.builtin:
                where = "~/.nexus" if operation["scope"] == "global" else "<project>/.agents"
                self.form["status"] = f"Built-in default · saving writes an override to {where}"
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
            if following.get("kind") == "settings_delete":  # same wording as Textual
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
                {"kind": "default_agent_save", "name": row["name"]}) for row in await self.client.list_agents()])
        elif kind == "default_agent_save":
            self.shell.show("Default agent saved", await self.client.set_default_agent(operation["name"], "global"))
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
            await self.shell.command("/hotkeys", ())
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
            if not await self.shell.refresh_preview():
                self.shell.notice = "Context details are unavailable while a turn is running"
                return
            preview = self.shell.preview
            key = operation["key"]
            if key == "system":
                self.shell.show("System prompt · literal", preview.system_text or "(empty)")
            elif key == "agents":
                self.shell.show("AGENTS.md", [part for part in preview.included_parts if part.get("name") == "agents_md"] or preview.system_files)
            elif key == "tools":
                self.tools_expanded = set()
                self.tools_modal()
            elif key in {"skills", "mcp"}:
                await self.context_extensions(key)
            else:
                self.shell.show("Current request", preview)
        elif kind == "tools_toggle":
            self.tools_expanded ^= {operation["key"]}
            self.tools_modal()
        elif kind == "tool_definition":
            from ...ui_support.context import _compact_tokens, tool_groups
            group = next(g for g in tool_groups(list(self.shell.preview.tools)) if g.key == operation["group"])
            entry = group.entries[operation["index"]]
            self.menu(f"Tool · {entry.title} · ~{_compact_tokens(entry.tokens)} tokens", [("Back", {"kind": "back"})], entry.body.splitlines())
        elif kind == "context_toggle":
            self.shell.preview = await self.client.select_context_extension(self.shell.controller.session,
                operation["category"], operation["name"], operation["enabled"])
            await self.context_extensions(operation["category"])
        elif kind == "context_extensions":
            await self.context_extensions(operation["category"])
        elif kind == "agent_page":
            self.agent_page_id = operation["id"]
            self.shell.show("Agent transcript", await self.client.agent_transcript(self.shell.controller.session, operation["id"]))
        elif kind == "attachment_preview":
            item = next((item for item in self.shell.attachments if item.attachment_id == operation["id"]), None)
            if item is None:
                raise ValueError("Attachment is no longer attached")
            self.menu("Attachment · " + item.name, [("Remove attachment", {"kind": "attachment_remove", "id": item.attachment_id})], labelled(item.preview))
        elif kind == "attachment_remove":
            # Labels stay assigned, so the remaining references (and their numbers) keep their meaning.
            self.shell.attachments = [item for item in self.shell.attachments if item.attachment_id != operation["id"]]
            self.shell.panel_title = ""
            self.stack.clear()
            await self.shell.command("/attach", ())
        elif kind == "message_page":
            message = next(message for turn in self.shell.controller.view.turns for message in turn.messages if message.id == operation["id"])
            self.shell.show("Submitted message · every content block", message.to_dict())
        elif kind == "turn_toggle":
            if operation["id"] in self.shell.collapsed_turns:
                self.shell.collapsed_turns.remove(operation["id"])
            else:
                self.shell.collapsed_turns.add(operation["id"])
        elif kind == "tool_page":
            from ...ui_support.tool_details import styled_lines, tool_detail_sections
            tool = next(tool for tool in self.shell.controller.view.tools if tool.call_id == operation["id"])
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
        elif kind == "voice":
            await self.shell.voice.open()
        elif kind == "voice_enable":
            from ...ui_support.voice_settings import set_voice_config
            await set_voice_config(self.client, enabled=True)
            await self.shell.voice.open()
        elif kind == "voice_start":
            await self.shell.voice.start()
        elif kind == "voice_prepare":
            await self.client.voice_prepare()
            await self.shell.voice.open()
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
            model = redact(escape_controls(result.global_model))
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
        self.menu(provider.get("label", provider_id), rows, ([message] if message else []) + labelled(provider))

    def review_binding(self):
        r = self.review
        return {"child_id": r.child_id, "review_id": r.review_id, "digest": r.digest}

    async def review_pages(self, child_id):
        """Load every review page for one pinned review identity (Textual pages by 8 files)."""
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

    def tools_modal(self):
        """The Tools dialog: families (and MCP servers) with their tools and token estimates.

        A family row expands to its tools; a tool row opens everything the model is
        given for it (Textual's ``ToolsModal``).
        """
        from ...ui_support.context import _compact_tokens, tool_groups
        preview = self.shell.preview
        groups = tool_groups(list(preview.tools))
        count = sum(len(group.entries) for group in groups)
        tokens = sum(group.tokens for group in groups)
        names = [group.title.removeprefix("MCP · ") for group in groups]
        width = min(max((len(name) for name in names), default=8), 24)
        rows = []
        for group, name in zip(groups, names):
            mark = "▾" if group.key in self.tools_expanded else "▸"
            tools = "  ".join(entry.title for entry in group.entries)
            rows.append((f"{mark} ■ {name[:width]:<{width}}  {tools[:70]}  ~{_compact_tokens(group.tokens)}", {"kind": "tools_toggle", "key": group.key}))
            if group.key in self.tools_expanded:
                for index, entry in enumerate(group.entries):
                    rows.append((f"      {entry.title}  {entry.detail[:60]}  ~{_compact_tokens(entry.tokens)}",
                                 {"kind": "tool_definition", "group": group.key, "index": index}))
        rows.append(("Edit tools…", {"kind": "settings", "scope": "global", "category": "tools"}))
        note = [] if getattr(preview, "tools_supported", True) is not False else ["The selected model does not support tools; none are sent."]
        self.menu(f"Tools · {count} definition{'s' if count != 1 else ''} · ~{_compact_tokens(tokens)} tokens", rows,
                  note + ([] if groups else ["(none)"]))

    async def context_extensions(self, category):
        self.shell.preview = await self.client.inspect_context(self.shell.controller.session)
        preview = self.shell.preview
        rows = preview.skills_index if category == "skills" else preview.mcp_servers
        self.menu(category.upper(), [(str(row.get("name") or row.get("id")) + (" · disabled" if row.get("enabled") is False else " · enabled"),
            {"kind": "context_toggle", "category": category, "name": row.get("name") or row.get("id"), "enabled": row.get("enabled") is False})
            for row in rows if row.get("config_enabled") is not False], labelled(rows) + (["Context locked after first turn"] if preview.context_locked else []))
        if preview.context_locked:
            self.shell.items = []

    async def sessions(self):
        result = await self.client.project_sessions()
        self.shell.sessions = session_rows(result, self.shell.controller.session, self.shell.seen_seq)
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
    """Starter text for a new settings file (same templates as Textual)."""
    if category == "agents":
        return (f"---\nname: {name}\ndescription: Describe when the root agent should use {name}.\n"
                "contexts: [subagent]\n---\nYou are a subagent. Do the task you are given and "
                "finish with a report that lists every file you changed.\n")
    if category == "skills":
        return f"---\nname: {name}\ndescription: Describe this skill.\n---\nInstructions.\n"
    return '{"mcpServers": {}}\n' if category == "mcp" else ""


def session_rows(result, current: str = "", seen: dict | None = None, now: float | None = None):
    """Project session cards: ``status``/``sub`` come from the helpers Textual's cards use.

    ``seen`` records the last sequence viewed per session so a finished background
    session reads "finished" until it is opened (the Textual sidebar's rule).
    """
    from ...ui_support.session_groups import _session_groups
    from ...ui_support.session_status import session_status, session_subline
    from ...ui_support.text import escape_controls, redact
    seen = {} if seen is None else seen
    groups = _session_groups([(row.workspace, row.session) for row in result.sessions], "")
    workspace_for = {id(row.session): row.workspace for row in result.sessions}
    rows = []
    for group, members in groups:
        for row in members:
            if row.id not in seen or row.id == current:
                seen[row.id] = max(seen.get(row.id, 0), getattr(row, "last_seq", 0))
            status = session_status(row, seen, current)
            rows.append({"id": row.id, "title": redact(escape_controls(row.title or row.id)),
                         "workspace": workspace_for[id(row)], "state": row.state, "group": redact(escape_controls(group)),
                         "status": status, "sub": redact(escape_controls(session_subline(row, status, now))),
                         "active": row.id == current})
    return rows
