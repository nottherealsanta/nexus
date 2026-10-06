"""An agent's Tiers row (Ratatui). Settings → Models and the session titles now live on one
page in ``settings_pages/models.py``.

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

    async def tier_current_refs(self, name):
        result = await self.client.model_tiers()
        row = next((row for row in _get(result, "tiers", []) if _get(row, "name") == name), None)
        return list(_get(row, "refs", [])) if row is not None else []

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
        """Handle one agent-tier operation; ``False`` when it is not one of ours."""
        kind = operation["kind"]
        if kind == "agent_tiers":
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
                    self.shell.flash(error, "warning")
                    return True
            await self.agent_tiers_write(tiers)
        else:
            return False
        return True
