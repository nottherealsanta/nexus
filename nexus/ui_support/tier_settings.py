"""Pure logic for Settings -> Models, Session titles and an agent's Tiers row.

Both terminal clients render these pages from the same rows, labels and help
text, so they say the same thing (plans/SESSION_TITLE_PLAN.md). Nothing here
touches a widget or the host: it turns host results into labelled rows and
turns user actions into the values a host command takes.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .agent_frontmatter import MAX_TIERS, agent_fields, tier_items

__all__ = [
    "MODELS_HELP",
    "NEW_AGENT_TIERS",
    "TIERS_HELP",
    "TITLES_HELP",
    "agent_tiers_label",
    "agent_tiers_notes",
    "make_default_tier",
    "move_ref",
    "new_agent_template",
    "remove_ref",
    "tier_label",
    "tier_lines",
    "titles_explanation",
    "toggle_tier",
]

NEW_AGENT_TIERS = ("low", "medium")

MODELS_HELP = (
    "Tiers let you refer to a model by size. `low` is used for quick background "
    "jobs such as session titles. The first model in a list that can run is used."
)
TIERS_HELP = (
    "Which model tiers the calling agent may run this subagent on. The first is "
    "used unless the caller asks for another. Tiers map to models in Settings → Models."
)
TITLES_HELP = (
    "Nexus names each new session by sending your first message to a fast, "
    "low-cost model. Turn this off to use the first line of your first message as the title."
)


def _plain(value: Any, key: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def tier_label(row: Any) -> str:
    """``low · your list · runs on openai/gpt-5-mini`` (or why nothing runs)."""
    name = _plain(row, "name")
    source = _plain(row, "source")
    resolved = _plain(row, "resolved")
    refs = list(_plain(row, "refs", []) or [])
    tail = f"runs on {resolved}" if resolved else "no runnable model"
    count = f" · {len(refs)} model{'s' if len(refs) != 1 else ''}" if refs else ""
    return f"{name} · {source}{count} · {tail}"


def tier_lines(row: Any) -> list[str]:
    """The explanatory lines on one tier's page."""
    source = _plain(row, "source")
    resolved = _plain(row, "resolved")
    lines = [
        f"Source: {source}. "
        + {
            "your list": "Saved in your global config; the first model that can run is used.",
            "built-in": "Nexus's built-in choice. Add a model to make your own list.",
            "by price": "Models are sorted into this tier by price. Add a model to pin your own.",
        }.get(str(source), "")
    ]
    lines.append(f"Runs on: {resolved}" if resolved else "No model in this tier can run: connect its provider or add another model.")
    if not _plain(row, "editable", True):
        lines.append("This tier's name needs quoting; edit it in Settings → Config.")
    return lines


def move_ref(refs: Sequence[str], index: int, delta: int) -> list[str]:
    """Swap ``refs[index]`` with its neighbour (no-op at either end)."""
    result = list(refs)
    target = index + delta
    if 0 <= index < len(result) and 0 <= target < len(result):
        result[index], result[target] = result[target], result[index]
    return result


def remove_ref(refs: Sequence[str], index: int) -> list[str]:
    return [ref for position, ref in enumerate(refs) if position != index]


def titles_explanation(state: Any) -> list[str]:
    """The two-line explanation, naming the model titles will be sent to."""
    model = _plain(state, "model") or "low"
    resolved = _plain(state, "resolved")
    target = f"{model} → {resolved}" if resolved and resolved != model else model
    lines = [
        f"Nexus names each new session by sending your first message to a fast, low-cost model ({target}).",
        "Turn this off to use the first line of your first message as the title.",
    ]
    message = _plain(state, "message")
    if message:
        lines.append(message)
    return lines


# -- an agent's Tiers row ---------------------------------------------------


def agent_tiers_label(body: str) -> str:
    """``Tiers · low (default), medium`` or the not-set wording."""
    tiers = tier_items(agent_fields(body).get("tiers", ""))
    if not tiers:
        return "Tiers · not set (uses the parent's model)"
    listed = ", ".join(f"{tier} (default)" if index == 0 and len(tiers) > 1 else tier for index, tier in enumerate(tiers))
    return f"Tiers · {listed}"


def toggle_tier(current: Sequence[str], tier: str, order: Sequence[str]) -> tuple[list[str], str]:
    """Check or uncheck ``tier``; returns ``(new list, error)``.

    The list keeps the user's order (the first stays the default) and a newly
    checked tier goes after the others. The last tier cannot be unchecked.
    """
    items = list(current)
    if tier in items:
        if len(items) == 1:
            return items, "At least one tier must stay checked."
        items.remove(tier)
        return items, ""
    if len(items) >= MAX_TIERS:
        return items, f"At most {MAX_TIERS} tiers."
    items.append(tier)
    ranked = {name: index for index, name in enumerate(order)}
    head, rest = items[0], sorted(items[1:], key=lambda name: ranked.get(name, len(ranked)))
    return [head, *rest], ""


def make_default_tier(current: Sequence[str], tier: str) -> list[str]:
    """Move ``tier`` to the front (the default); other order is kept."""
    if tier not in current:
        return list(current)
    return [tier, *[name for name in current if name != tier]]


def agent_tiers_notes(body: str, order: Sequence[str], ceiling: str, tier_of_model: str = "") -> list[str]:
    """Conflicts and ceiling effects, shown beside the Tiers row (never hidden)."""
    fields = agent_fields(body)
    tiers = tier_items(fields.get("tiers", ""))
    notes: list[str] = []
    if not tiers:
        return notes
    model = fields.get("model", "")
    if model == "inherit":
        notes.append("Model is 'inherit' while tiers are set; remove one of them or the agent will not load.")
    elif tier_of_model and tier_of_model not in tiers:
        notes.append(f"The pinned model is in the {tier_of_model} tier, which is not checked; calls move to the nearest checked tier.")
    ranked = {name: index for index, name in enumerate(order)}
    if ceiling in ranked:
        above = [tier for tier in tiers if ranked.get(tier, 0) > ranked[ceiling]]
        for tier in above:
            notes.append(f"{tier} is above the global limit ({ceiling}): it runs as {ceiling}.")
    return notes


def new_agent_template(name: str) -> str:
    """Starter file for a new subagent, with tiers the user can change at once."""
    tiers = ", ".join(NEW_AGENT_TIERS)
    return (
        f"---\nname: {name}\ndescription: Describe when the root agent should use {name}.\n"
        f"contexts: [subagent]\ntiers: [{tiers}]\n---\nYou are a subagent. Do the task you are given and "
        "finish with a report that lists every file you changed.\n"
    )
