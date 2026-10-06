"""Settings → Models: default model, session titles, tiers (as tabs), subagent limit, catalogue.

Everything one page: the default model chain and the session-title model sit above the
Low/Medium/High tier tabs, each tier an ordered list the user reorders in place. Saves go
through the same host commands as before (``DefaultModelSet``, ``ModelTierSet``,
``ModelTierReset``, ``AgentMaxTierSet``, ``SessionTitleSettingsSet``, ``ModelsRefresh``).
"""
from __future__ import annotations

from ....ui_support import settings_page as sp
from ....ui_support import tier_settings as ts

AREA = "models"
PICK = "__pick__"


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _items(refs, resolved, candidates):
    """``(label, tag, note)`` per model: what runs, what is a fallback, what is skipped and why."""
    by_ref = {c.get("ref"): c for c in candidates or []}
    items = []
    for ref in refs:
        candidate = by_ref.get(ref)
        if ref == resolved:
            items.append((ref, "in use", ""))
        elif candidate is not None and not candidate.get("connected", True):
            items.append((ref, "skipped", candidate.get("reason") or "provider not connected"))
        else:
            items.append((ref, "fallback", ""))
    return items


async def build(workflows) -> dict:
    client = workflows.client
    state = workflows.page_state.setdefault(AREA, {"tab": 0})
    tiers = await client.model_tiers()
    order = list(_get(tiers, "order", []) or ["low", "medium", "high"])
    rows = {_get(row, "name"): row for row in _get(tiers, "tiers", [])}
    tab = max(0, min(state.get("tab", 0), len(order) - 1))
    state["tab"] = tab
    name = order[tab]
    tier = rows.get(name, {})

    blocks = []
    # -- default model chain -------------------------------------------------
    blocks.append(sp.heading("DEFAULT MODEL"))
    default = await client.default_model_settings() if hasattr(client, "default_model_settings") else None
    if default is not None:
        refs = list(_get(default, "refs", []))
        blocks.append(sp.note(f"New sessions run on the first model that can run: {_get(default, 'resolved') or 'none can run right now'}.",
                              "muted" if _get(default, "resolved") else "warning"))
        blocks.append(sp.ordered("chain", _items(refs, _get(default, "resolved", ""), _get(default, "candidates", [])),
                                 sp.op(AREA, "chain"), "Add model…"))
        if _get(default, "message"):
            blocks.append(sp.note(_get(default, "message"), "warning"))
    blocks.append(sp.gap())

    # -- session titles ------------------------------------------------------
    titles = await client.session_title_settings()
    model = _get(titles, "model", "low")
    resolved = _get(titles, "resolved", "")
    choices = [(f"{t} tier", t) for t in order]
    if model not in order:
        choices.append((model, model))
    choices.append(("Other model…", PICK))
    shown = f"{model} tier" if model in order else model
    blocks.append(sp.heading("SESSION TITLES"))
    blocks.append(sp.row("titles", "Name new sessions automatically", sp.toggle(_get(titles, "enabled", True), sp.op(AREA, "titles")),
                         description="Your first message is sent to a fast, low-cost model to write the title."))
    blocks.append(sp.row("title-model", "Title model", sp.select(shown, choices, sp.op(AREA, "title_model")),
                         description=(f"Runs on {resolved}" if resolved else (_get(titles, "message") or "No model can run: connect a provider or pick another."))))
    blocks.append(sp.gap())

    # -- tiers (tabs) --------------------------------------------------------
    blocks.append(sp.heading("TIERS"))
    blocks.append(sp.note(ts.MODELS_HELP))
    badges = ["!" if not _get(rows.get(t, {}), "resolved", "") else "" for t in order]
    blocks.append(sp.tabs("tiers", [t.title() for t in order], tab, sp.op(AREA, "tab"), badges))
    refs = list(_get(tier, "refs", []))
    blocks.append(sp.ordered(f"tier:{name}", _items(refs, _get(tier, "resolved", ""), _get(tier, "candidates", [])),
                             sp.op(AREA, "tier", tier=name), "Add model…", editable=bool(_get(tier, "editable", True))))
    source = _get(tier, "source", "")
    blocks.append(sp.note({
        "your list": "Your own list, saved in your global config.",
        "built-in": "Nexus's built-in choice. Add a model to make your own list.",
        "by price": "Models are sorted into this tier by price. Add a model to pin your own.",
    }.get(str(source), str(source))))
    if not _get(tier, "resolved", ""):
        blocks.append(sp.note("No model in this tier can run: connect its provider or add another model.", "warning"))
    if not _get(tier, "editable", True):
        blocks.append(sp.note("This tier's name needs quoting; edit it in Settings → Config.", "warning"))
    if source == "your list":
        blocks.append(sp.buttons("tier-reset", [(f"Reset {name} to default…", sp.op(AREA, "tier_reset_ask", tier=name), "secondary")]))
    blocks.append(sp.gap())

    # -- subagent limit and catalogue ---------------------------------------
    blocks.append(sp.heading("LIMITS"))
    ceiling = _get(tiers, "max_tier", "") or "high"
    blocks.append(sp.row("ceiling", "Highest tier for subagents",
                         sp.segmented([t.title() for t in order], order.index(ceiling) if ceiling in order else len(order) - 1,
                                      sp.op(AREA, "ceiling"), values=order),
                         description="Subagents never run above this tier, whatever their own list allows."))
    blocks.append(sp.gap())
    blocks.append(sp.heading("CATALOGUE"))
    models = await client.list_models(selectable_only=False) if hasattr(client, "list_models") else None
    count = len(models) if isinstance(models, list) else _get(models, "count", None)
    blocks.append(sp.row("catalogue", "Model catalogue", sp.button("Refresh", sp.op(AREA, "refresh")),
                         description=f"{count} models known." if count is not None else ""))
    return sp.page(AREA, "Models", blocks, footer="Saved in ~/.nexus/config.toml · applies on the next turn",
                   intro="The first model in each list that can run is used; the rest are fallbacks, tried in order.")


async def _refs(workflows, target: str) -> list[str]:
    client = workflows.client
    if target == "chain":
        return list(_get(await client.default_model_settings(), "refs", []))
    return await workflows.tier_current_refs(target.removeprefix("tier:"))


async def _commit(workflows, target: str, refs: list[str], message: str) -> None:
    client = workflows.client
    if not refs:
        raise ValueError("Keep at least one model. Use Reset to default to go back to the built-in models."
                         if target != "chain" else "Keep at least one default model")
    if target == "chain":
        result = await client.default_model_set(refs)
    else:
        result = await client.model_tier_set(target.removeprefix("tier:"), refs)
    workflows.shell.flash(f"{message} · restart the daemon to apply" if _get(result, "restart_required") else f"{message} · applies next turn", "success")


async def _pick(workflows, target: str) -> None:
    """The one allowed drill-in: choose a model to add; the page returns when it is added."""
    from ....ui_support.model_choice import model_groups, recent_models
    current = set(await _refs(workflows, target))
    rows = [row for row in recent_models(await workflows.client.list_models(selectable_only=True))
            if row.get("provider") and row.get("id") and f"{row['provider']}/{row['id']}" not in current]
    groups, _ = model_groups(rows, favorites=workflows.shell.preferences.values["model_favorites"],
                             recent=workflows.shell.preferences.values["model_recent"])
    items = [(f"{row.get('name') or row['id']} · {row['provider']}/{row['id']}",
              sp.op(AREA, "add", target=target, ref=f"{row['provider']}/{row['id']}"), title)
             for title, group in groups for row in group]
    workflows.menu(f"Add model · {target.removeprefix('tier:')}", [(label, operation) for label, operation, _ in items])
    for item, (_, _, title) in zip(workflows.shell.items, items):
        item["group"] = title


async def handle(workflows, operation) -> None:
    client = workflows.client
    key = operation["key"]
    state = workflows.page_state.setdefault(AREA, {"tab": 0})
    if key == "tab":
        state["tab"] = int(operation.get("value", 0))
    elif key in {"chain", "tier"}:
        target = "chain" if key == "chain" else f"tier:{operation['tier']}"
        action, index = operation.get("action"), int(operation.get("index", 0))
        if action == "add":
            return await _pick(workflows, target)
        refs = await _refs(workflows, target)
        if action == "up":
            refs = ts.move_ref(refs, index, -1)
        elif action == "down":
            refs = ts.move_ref(refs, index, 1)
        elif action == "remove":
            refs = ts.remove_ref(refs, index)
        else:
            raise ValueError(f"Unknown list action {action!r}")
        await _commit(workflows, target, refs, "Saved")
    elif key == "add":
        target = operation["target"]
        refs = await _refs(workflows, target)
        if operation["ref"] not in refs:
            await _commit(workflows, target, [*refs, operation["ref"]], f"Added to {target.removeprefix('tier:')}")
        workflows.return_to_page()  # a picker may be stacked on the page
    elif key == "titles":
        await client.session_title_settings_set(enabled=bool(operation["value"]))
    elif key == "title_model":
        value = operation["value"]
        if value == PICK:
            return await _title_pick(workflows)
        await client.session_title_settings_set(model=value)
    elif key == "title_set":
        await client.session_title_settings_set(model=operation["model"])
        workflows.return_to_page()
    elif key == "ceiling":
        await client.agent_max_tier_set(operation["value"])
    elif key == "tier_reset_ask":
        name = operation["tier"]
        workflows.menu(f"Reset {name}?", [("Keep my list", {"kind": "back"}), ("Reset to default", sp.op(AREA, "tier_reset", tier=name))],
                       [f"Remove your list for {name}? The tier returns to its default models."])
    elif key == "tier_reset":
        result = await client.model_tier_reset(operation["tier"])
        workflows.shell.flash(f"{operation['tier']} reset · restart the daemon to apply" if _get(result, "restart_required")
                              else f"{operation['tier']} reset to default", "success")
        workflows.return_to_page()
    elif key == "refresh":
        await client.refresh_models()
        workflows.shell.flash("Model catalogue refreshed", "success")
    else:
        raise ValueError(f"Unknown Models operation {key!r}")


async def _title_pick(workflows) -> None:
    from ....ui_support.model_choice import model_groups, recent_models
    models = [row for row in recent_models(await workflows.client.list_models(selectable_only=True)) if row.get("provider") and row.get("id")]
    groups, _ = model_groups(models, favorites=workflows.shell.preferences.values["model_favorites"],
                             recent=workflows.shell.preferences.values["model_recent"])
    items = [(f"{row.get('name') or row['id']} · {row['provider']}/{row['id']}",
              sp.op(AREA, "title_set", model=f"{row['provider']}/{row['id']}"), title) for title, group in groups for row in group]
    workflows.menu("Title model", [(label, operation) for label, operation, _ in items],
                   ["Choose one model. Your first message is sent to it to write the title."])
    for item, (_, _, title) in zip(workflows.shell.items, items):
        item["group"] = title
