"""Settings -> Models, Session titles, and an agent's Tiers row (Ratatui).

A mixin for ``Workflows``: the pages are built from the shared rows, labels and
help text in ``ui_support/tier_settings.py`` so native clients use consistent
wording, and every change goes through the host commands (``ModelTierSet``,
``AgentMaxTierSet``, ``SessionTitleSettingsSet`` and the agent file write).
"""
from __future__ import annotations

from ...ui_support import tier_settings as ts


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


class TierPages:
    """Page builders and operation handlers; ``operate`` calls :meth:`tier_operate` first."""

    #: Last ``ModelTiers`` result and the pinned model's tier, for the agent Tiers row.
    tier_cache: dict = {}

    # -- Settings -> Models -------------------------------------------------

    def models_rows(self, result):
        rows = [(ts.tier_label(row), {"kind": "tier_open", "name": _get(row, "name")}) for row in _get(result, "tiers", [])]
        rows.append((f"Highest tier for subagents · {_get(result, 'max_tier') or 'high'}", {"kind": "tier_ceiling_choices"}))
        lines = [ts.MODELS_HELP, "Subagents never run above their highest tier, whatever their own list allows."]
        return rows, lines

    async def models_settings(self):
        result = await self.client.model_tiers()
        rows, lines = await self.complete_models_rows(result)
        self.menu("Models", rows, lines)
        for item in self.shell.items:
            item["group"] = "Default model" if item["operation"]["kind"] == "models_default" else "Tiers"

    async def complete_models_rows(self, result):
        """Include the default chain on both live and refreshed parent pages."""
        rows, lines = self.models_rows(result)
        if hasattr(self.client, "default_model_settings"):
            state = await self.client.default_model_settings()
            rows.insert(0, (f"Default model · {_get(state, 'resolved', '') or 'not runnable'}", {"kind": "models_default"}))
        return rows, lines

    def refresh_stacked(self, title, rows, lines):
        """Rebuild a stacked page so Back never shows a stale tier table."""
        for index, entry in enumerate(self.stack):
            if entry[0] == title:
                self.stack[index] = (title, lines, [{"label": label, "command": "", "operation": op} for label, op in rows], *entry[3:])

    async def tier_page(self, name):
        result = await self.client.model_tiers()
        row = next((row for row in _get(result, "tiers", []) if _get(row, "name") == name), None)
        if row is None:
            raise ValueError(f"Unknown tier {name}")
        refs = list(_get(row, "refs", []))
        rows = []
        candidates = {c["ref"]: c for c in _get(row, "candidates", [])}
        for index, ref in enumerate(refs):
            candidate = candidates.get(ref, {})
            status = "in use" if ref == _get(row, "resolved", "") else "skipped · " + candidate.get("reason", "provider not connected") if candidate and not candidate.get("connected") else "fallback"
            op = {"kind": "tier_ref", "name": name, "index": index,
                  "_presentation": {"name": f"{index + 1}. {ref}", "status": status, "scope": "Global",
                    "changed": _get(row, "source") == "your list",
                    "move_up": {"kind": "tier_move", "name": name, "index": index, "delta": -1} if index else None,
                    "move_down": {"kind": "tier_move", "name": name, "index": index, "delta": 1} if index + 1 < len(refs) else None,
                    "remove": {"kind": "tier_remove", "name": name, "index": index} if len(refs) > 1 else None}}
            rows.append((f"{index + 1}. {ref} · {status}", op))
        if _get(row, "editable", True):
            rows.append(("+ Add model…", {"kind": "tier_add_pick", "name": name}))
            if _get(row, "source") == "your list":
                rows.append(("Reset to default…", {"kind": "confirm", "label": f"Remove your list for {name}? The tier returns to its default models.",
                                                   "next": {"kind": "tier_reset", "name": name}}))
        self.menu(f"Tier · {name}", rows, ts.tier_lines(row))
        # The Models page beneath shows the new state on Back.
        models_rows, models_lines = await self.complete_models_rows(result)
        self.refresh_stacked("Models", models_rows, models_lines)

    async def tier_commit(self, name, refs, *, message="Saved"):
        if not refs:
            raise ValueError("A tier needs at least one model. Use Reset to default to go back to the built-in models.")
        result = await self.client.model_tier_set(name, refs)
        self.shell.notice = f"{message} · restart the daemon to apply" if _get(result, "restart_required") else message

    async def tier_current_refs(self, name):
        result = await self.client.model_tiers()
        row = next((row for row in _get(result, "tiers", []) if _get(row, "name") == name), None)
        return list(_get(row, "refs", [])) if row is not None else []

    def back_to(self, title):
        """Pop transient pages until ``title`` is current again."""
        while self.stack and self.shell.panel_title != title:
            self.back()

    async def default_models_page(self):
        state = await self.client.default_model_settings()
        refs = list(_get(state, "refs", []))
        rows = []
        candidates = {row["ref"]: row for row in _get(state, "candidates", [])}
        for index, ref in enumerate(refs):
            candidate = candidates.get(ref, {})
            status = "in use" if ref == _get(state, "resolved") else (
                "not runnable · " + candidate.get("reason", "provider not connected")
                if candidate and not candidate.get("connected") else "fallback"
            )
            rows.append((f"{index + 1}. {ref}", {"kind": "models_default_ref", "index": index,
                "_presentation": {"name": f"{index + 1}. {ref}", "scope": "Global",
                    "status": status,
                    "move_up": {"kind": "models_default_move", "index": index, "delta": -1} if index else None,
                    "move_down": {"kind": "models_default_move", "index": index, "delta": 1} if index + 1 < len(refs) else None,
                    "remove": {"kind": "models_default_remove", "index": index} if len(refs) > 1 else None}}))
        rows.append(("+ Add model…", {"kind": "models_default_pick"}))
        self.menu("Default model", rows, ["Global · saved in ~/.nexus/config.toml", "First model is used; remaining models are ordered fallbacks.",
                  f"Runs on: {_get(state, 'resolved') or 'not runnable'}", *([_get(state, "message")] if _get(state, "message") else [])])
        parent_rows, parent_lines = await self.complete_models_rows(await self.client.model_tiers())
        self.refresh_stacked("Models", parent_rows, parent_lines)

    # -- Settings -> Session titles ----------------------------------------

    async def title_settings(self):
        state = await self.client.session_title_settings()
        enabled = bool(_get(state, "enabled", True))
        model = _get(state, "model", "low")
        resolved = _get(state, "resolved", "")
        rows = [
            (f"Generate titles automatically · {'on' if enabled else 'off'}", {"kind": "title_set", "enabled": not enabled}),
            (f"Title model · {model}" + (f" → {resolved}" if resolved and resolved != model else ""), {"kind": "title_model_pick"}),
        ]
        self.menu("Session titles", rows, ts.titles_explanation(state))
        for item in self.shell.items:
            item["group"] = "Titles"

    async def title_model_pick(self):
        from ...ui_support.model_choice import model_groups, recent_models
        tiers = await self.client.model_tiers()
        rows = [(f"{_get(row, 'name')} tier" + (f" → {_get(row, 'resolved')}" if _get(row, "resolved") else " · no runnable model"),
                 {"kind": "title_model_set", "model": _get(row, "name")}, "Tiers") for row in _get(tiers, "tiers", [])]
        models = [row for row in recent_models(await self.client.list_models(selectable_only=True)) if row.get("provider") and row.get("id")]
        groups, _ = model_groups(models, favorites=self.shell.preferences.values["model_favorites"], recent=self.shell.preferences.values["model_recent"])
        rows += [(f"{row.get('name') or row['id']} · {row['provider']}/{row['id']}",
                  {"kind": "title_model_set", "model": f"{row['provider']}/{row['id']}"}, title) for title, group in groups for row in group]
        self.menu("Title model", [(label, op) for label, op, _ in rows],
                  ["Choose a tier (recommended: low) or one model. Your first message is sent to it to write the title."])
        for item, (_, _, group) in zip(self.shell.items, rows):
            item["group"] = group

    # -- an agent's Tiers row -----------------------------------------------

    async def load_tier_cache(self, body):
        """Fetch the tier order, ceiling and the pinned model's tier for the agent page."""
        from ...ui_support.agent_frontmatter import agent_fields
        cache = {"order": ["low", "medium", "high"], "ceiling": "high", "model_tier": ""}
        try:
            result = await self.client.model_tiers()
            cache["order"] = list(_get(result, "order", cache["order"])) or cache["order"]
            cache["ceiling"] = _get(result, "max_tier", "") or "high"
            model = agent_fields(body).get("model", "")
            if model and model != "inherit" and model not in cache["order"]:
                shown = await self.client.show_model(model)
                info = _get(shown, "model")
                cache["model_tier"] = str(_get(info, "tier", "") or "") if info is not None else ""
        except Exception:  # noqa: BLE001 - the notes are advisory; the page still opens
            pass
        self.tier_cache = cache

    def agent_tier_rows(self, body):
        """Rows and notes for the Tiers entry on the agent page."""
        cache = self.tier_cache or {"order": ["low", "medium", "high"], "ceiling": "high", "model_tier": ""}
        notes = ts.agent_tiers_notes(body, cache["order"], cache["ceiling"], cache["model_tier"])
        return [(ts.agent_tiers_label(body), {"kind": "agent_tiers"})], notes

    def agent_tiers_page(self):
        from ...ui_support.agent_frontmatter import agent_fields, tier_items
        draft = self.agent_draft
        cache = self.tier_cache or {"order": ["low", "medium", "high"]}
        current = tier_items(agent_fields(draft["body"]).get("tiers", ""))
        rows = []
        for tier in cache["order"]:
            checked = tier in current
            rows.append((f"[{'x' if checked else ' '}] {tier}" + (" · default" if checked and current[0] == tier else ""),
                         {"kind": "agent_tier_toggle", "tier": tier}))
        for tier in current[1:]:
            rows.append((f"Make {tier} the default", {"kind": "agent_tier_default", "tier": tier}))
        self.menu("Agent tiers", rows, [ts.TIERS_HELP, "At least one tier stays checked. The first is the default."])

    async def agent_tiers_write(self, tiers):
        await self.agent_save({"tiers": ", ".join(tiers)})
        # agent_save re-shows the agent page; reopen the tiers page on top of it.
        self.agent_tiers_page()

    # -- dispatch ------------------------------------------------------------

    async def tier_operate(self, operation):
        """Handle one tier/title operation; ``False`` when it is not one of ours."""
        kind = operation["kind"]
        if kind == "models_default":
            await self.default_models_page()
        elif kind == "models_default_pick":
            state = await self.client.default_model_settings()
            refs = set(_get(state, "refs", []))
            rows = await self.client.list_models(selectable_only=True)
            self.menu("Add default model", [(f"{row['provider']}/{row['id']}", {"kind": "models_default_add", "ref": f"{row['provider']}/{row['id']}"})
                      for row in rows if row.get("provider") and row.get("id") and f"{row['provider']}/{row['id']}" not in refs])
        elif kind in {"models_default_move", "models_default_remove", "models_default_add"}:
            state = await self.client.default_model_settings()
            refs = list(_get(state, "refs", []))
            if kind == "models_default_add": refs.append(operation["ref"])
            elif kind == "models_default_remove":
                if len(refs) <= 1: raise ValueError("Keep at least one default model")
                refs.pop(operation["index"])
            else: refs = ts.move_ref(refs, operation["index"], operation["delta"])
            saved = await self.client.default_model_set(refs)
            self.shell.notice = "Saved · restart the daemon to apply" if _get(saved, "restart_required") else "Saved · applies next turn"
            self.back_to("Default model")
            await self.default_models_page()
        elif kind == "models_default_ref":
            index = operation["index"]
            refs = list(_get(await self.client.default_model_settings(), "refs", []))
            rows = []
            if index: rows.append(("Move up", {"kind": "models_default_move", "index": index, "delta": -1}))
            if index + 1 < len(refs): rows.append(("Move down", {"kind": "models_default_move", "index": index, "delta": 1}))
            if len(refs) > 1: rows.append(("Remove", {"kind": "models_default_remove", "index": index}))
            self.menu("Default model entry", rows)
        elif kind == "models_settings":
            await self.models_settings()
        elif kind == "tier_open":
            await self.tier_page(operation["name"])
        elif kind == "tier_ref":
            name, index = operation["name"], operation["index"]
            refs = await self.tier_current_refs(name)
            ref = refs[index] if index < len(refs) else ""
            self.menu(f"Model · {ref}", [
                ("Move up", {"kind": "tier_edit", "action": "up", "name": name, "index": index}),
                ("Move down", {"kind": "tier_edit", "action": "down", "name": name, "index": index}),
                ("Remove from tier", {"kind": "tier_edit", "action": "remove", "name": name, "index": index}),
            ], [f"Tier {name} uses the first model in this list that can run."])
        elif kind in {"tier_edit", "tier_move", "tier_remove"}:
            name, index = operation["name"], operation["index"]
            action = operation.get("action") or ("remove" if kind == "tier_remove" else "up" if operation["delta"] < 0 else "down")
            refs = await self.tier_current_refs(name)
            new = (ts.move_ref(refs, index, -1) if action == "up" else ts.move_ref(refs, index, 1) if action == "down"
                   else ts.remove_ref(refs, index))
            await self.tier_commit(name, new)
            self.back_to(f"Tier · {name}")
            await self.tier_page(name)
        elif kind == "tier_add_pick":
            from ...ui_support.model_choice import model_groups, recent_models
            name = operation["name"]
            current = set(await self.tier_current_refs(name))
            rows = [row for row in recent_models(await self.client.list_models(selectable_only=True))
                    if row.get("provider") and row.get("id") and f"{row['provider']}/{row['id']}" not in current]
            groups, _ = model_groups(rows, favorites=self.shell.preferences.values["model_favorites"], recent=self.shell.preferences.values["model_recent"])
            items = [(f"{row.get('name') or row['id']} · {row['provider']}/{row['id']}",
                      {"kind": "tier_add", "name": name, "ref": f"{row['provider']}/{row['id']}"}, title)
                     for title, group in groups for row in group]
            self.menu(f"Add model to {name}", [(label, op) for label, op, _ in items])
            for item, (_, _, title) in zip(self.shell.items, items):
                item["group"] = title
        elif kind == "tier_add":
            name = operation["name"]
            refs = await self.tier_current_refs(name)
            await self.tier_commit(name, [*refs, operation["ref"]], message=f"Added to {name}")
            self.back_to(f"Tier · {name}")
            await self.tier_page(name)
        elif kind == "tier_reset":
            name = operation["name"]
            result = await self.client.model_tier_reset(name)
            self.shell.notice = f"{name} reset · restart the daemon to apply" if _get(result, "restart_required") else f"{name} reset to default"
            self.back_to(f"Tier · {name}")
            await self.tier_page(name)
        elif kind == "tier_ceiling_choices":
            result = await self.client.model_tiers()
            current = _get(result, "max_tier", "high")
            self.menu("Highest tier for subagents", [
                (f"{tier}{' · selected' if tier == current else ''}", {"kind": "tier_ceiling_set", "tier": tier})
                for tier in _get(result, "order", [])],
                ["Subagents never run above this tier, whatever their own list allows."])
        elif kind == "tier_ceiling_set":
            await self.client.agent_max_tier_set(operation["tier"])
            self.back()
            await self.models_settings()
        elif kind == "title_settings":
            await self.title_settings()
        elif kind == "title_set":
            await self.client.session_title_settings_set(enabled=operation["enabled"])
            self.back_to("Session titles")
            await self.title_settings()
        elif kind == "title_model_pick":
            await self.title_model_pick()
        elif kind == "title_model_set":
            await self.client.session_title_settings_set(model=operation["model"])
            self.back_to("Session titles")
            await self.title_settings()
        elif kind == "agent_tiers":
            self.agent_tiers_page()
        elif kind in {"agent_tier_toggle", "agent_tier_default"}:
            from ...ui_support.agent_frontmatter import agent_fields, tier_items
            current = tier_items(agent_fields(self.agent_draft["body"]).get("tiers", ""))
            order = (self.tier_cache or {}).get("order", ["low", "medium", "high"])
            if kind == "agent_tier_default":
                tiers = ts.make_default_tier(current, operation["tier"])
            else:
                tiers, error = ts.toggle_tier(current, operation["tier"], order)
                if error:
                    self.shell.notice = error
                    return True
            await self.agent_tiers_write(tiers)
        else:
            return False
        return True
