"""The typed Settings page model (plan RATATUI_DESIGN_MOCKUPS_PLAN §11.2).

One page per Settings area, built from blocks (headings, rows with a control, tabs,
ordered lists, sections, buttons, tables). Pure data, no IO and no UI toolkit: the
host-facing builders live in ``ui/ratatui/settings_pages/`` and the native client
renders the result. Every text is control-safe and every list is bounded, so a hostile
config value or provider name cannot break the terminal or the layout.

Operations are plain dicts the client sends back; it adds ``value`` (and, for ordered
lists, ``action`` and ``index``). ``area`` and ``key`` route them to a page handler.
"""
from __future__ import annotations

from typing import Any

from .text import escape_controls

MAX_BLOCKS = 400
MAX_TEXT = 600
MAX_ITEMS = 200

#: Dict keys whose string values are machine-owned and left as they are.
_MACHINE = {"operation", "kind", "area", "key", "t", "c", "variant", "tone", "level", "id", "scope", "values", "action_key"}


def op(area: str, key: str, **fields: Any) -> dict[str, Any]:
    """An operation routed to ``area``'s page handler as ``key``."""
    return {"kind": f"sp_{area}", "area": area, "key": key, **fields}


# -- controls (the right-hand side of a row) --------------------------------


def toggle(on: bool, operation: dict, locked: str = "") -> dict:
    return {"c": "toggle", "on": bool(on), "locked": locked, "operation": operation}


def segmented(options: list[str], active: int, operation: dict, values: list[Any] | None = None) -> dict:
    """``values`` are what the client sends back for each option (default: the labels)."""
    return {"c": "segmented", "options": list(options), "active": max(0, min(active, len(options) - 1)) if options else 0,
            "values": list(values) if values is not None else list(options), "operation": operation}


def select(value: str, options: list[tuple[str, Any]], operation: dict) -> dict:
    """``options`` are ``(label, value)``; ``value`` is the label shown while closed."""
    return {"c": "select", "value": value, "options": [[label, val] for label, val in options[:MAX_ITEMS]], "operation": operation}


def stepper(value: float, operation: dict, *, display: str = "", minimum: float = 0, maximum: float = 100, step: float = 1) -> dict:
    return {"c": "stepper", "value": value, "display": display or str(value), "min": minimum, "max": maximum, "step": step, "operation": operation}


def text(value: str, operation: dict, *, secret: bool = False, placeholder: str = "") -> dict:
    return {"c": "text", "value": value, "secret": bool(secret), "placeholder": placeholder, "operation": operation}


def button(label: str, operation: dict, variant: str = "secondary") -> dict:
    return {"c": "button", "label": label, "variant": variant, "operation": operation}


def readout(value: str) -> dict:
    """A read-only value."""
    return {"c": "value", "value": value}


# -- blocks ------------------------------------------------------------------


def heading(text_: str) -> dict:
    return {"t": "heading", "text": text_}


def note(text_: str, tone: str = "muted") -> dict:
    """``tone``: muted | warning | error."""
    return {"t": "note", "text": text_, "tone": tone}


def gap() -> dict:
    return {"t": "gap"}


def row(id_: str, label: str, control: dict, *, description: str = "", scope: str = "", error: str = "") -> dict:
    return {"t": "row", "id": id_, "label": label, "description": description, "scope": scope, "error": error, "control": control}


def tabs(id_: str, labels: list[str], active: int, operation: dict, badges: list[str] | None = None) -> dict:
    badges = list(badges or [])
    return {"t": "tabs", "id": id_, "items": [[label, badges[i] if i < len(badges) else ""] for i, label in enumerate(labels)],
            "active": max(0, min(active, len(labels) - 1)) if labels else 0, "operation": operation}


def ordered(id_: str, items: list[tuple[str, str, str]], operation: dict, add_label: str = "Add model…", *, editable: bool = True) -> dict:
    """``items``: ``(label, tag, note)``; an empty tag means ``in use`` for the first and ``fallback`` after."""
    return {"t": "ordered", "id": id_, "items": [[label, tag, note_] for label, tag, note_ in items[:MAX_ITEMS]],
            "add_label": add_label, "editable": editable, "operation": operation}


def buttons(id_: str, items: list[tuple[str, dict, str]], label: str = "") -> dict:
    """``items``: ``(label, operation, variant)``."""
    return {"t": "buttons", "id": id_, "label": label,
            "items": [{"label": lab, "operation": operation, "variant": variant} for lab, operation, variant in items]}


def section(id_: str, title: str, blocks: list[dict], *, summary: str = "", tone: str = "", open_: bool = True, collapsible: bool = True) -> dict:
    return {"t": "section", "id": id_, "title": title, "summary": summary, "tone": tone, "open": bool(open_),
            "collapsible": collapsible, "blocks": blocks}


def table(columns: list[tuple[str, int]], rows: list[list[str]]) -> dict:
    return {"t": "table", "cols": [[name, width] for name, width in columns], "rows": [list(map(str, r)) for r in rows[:MAX_ITEMS]]}


def progress(fraction: float, label: str = "") -> dict:
    return {"t": "progress", "fraction": max(0.0, min(1.0, float(fraction))), "label": label}


def callout(level: str, text_: str, action: dict | None = None) -> dict:
    """``action``: ``{"label": ..., "operation": ...}``."""
    return {"t": "callout", "level": level, "text": text_, "action": action}


# -- the page ----------------------------------------------------------------


def page(area: str, title: str, blocks: list[dict], *, intro: str = "", footer: str = "", scope: dict | None = None) -> dict:
    """``scope``: ``{"options": [...], "value": index, "operation": ...}`` only for pages that can differ per project."""
    return finish({"area": area, "title": title, "intro": intro, "footer": footer, "scope": scope, "blocks": blocks})


def finish(value: Any, _machine: bool = False) -> Any:
    """Control-safe, bounded copy of a page (applied once, when a page is built)."""
    if isinstance(value, str):
        return value if _machine else escape_controls(value)[:MAX_TEXT]
    if isinstance(value, list):
        return [finish(item, _machine) for item in value[:MAX_BLOCKS]]
    if isinstance(value, dict):
        return {key: finish(item, _machine or key in _MACHINE) for key, item in value.items()}
    return value


def count_blocks(value: Any) -> int:
    """Total blocks, counting section children (for tests and bounds)."""
    total = 0
    for block in value if isinstance(value, list) else value.get("blocks", []):
        total += 1
        if block.get("t") == "section":
            total += count_blocks(block.get("blocks", []))
    return total


# -- which operations the client may send back ---------------------------------

#: Fields the client fills in itself; everything else in an operation is the host's own.
CLIENT_FIELDS = ("value", "action", "index")
_LIST_ACTIONS = {"up", "down", "remove", "add"}


def _key(operation: dict) -> str:
    import json
    return json.dumps(operation, sort_keys=True)


def _walk(blocks):
    for block in blocks:
        yield block
        if block.get("t") == "section":
            yield from _walk(block.get("blocks", []))


def _control_rule(control: dict):
    """Predicate on the client's ``value`` for one control, or ``None`` when it sends no value."""
    kind = control.get("c")
    if kind == "toggle":
        return lambda op: isinstance(op.get("value"), bool)
    if kind == "segmented":
        values = control.get("values", [])
        return lambda op: op.get("value") in values
    if kind == "select":
        values = [option[1] for option in control.get("options", [])]
        return lambda op: op.get("value") in values
    if kind == "stepper":
        low, high = control.get("min", float("-inf")), control.get("max", float("inf"))
        return lambda op: isinstance(op.get("value"), (int, float)) and not isinstance(op.get("value"), bool) and low <= op["value"] <= high
    if kind == "text":
        return lambda op: isinstance(op.get("value"), str) and len(op["value"]) <= 400
    return None


def operations(page: dict) -> tuple[set[str], dict[str, Any]]:
    """The operations a page offers: exact ones (buttons) and base operations the client completes."""
    exact: set[str] = set()
    derived: dict[str, Any] = {}
    scope = page.get("scope")
    if scope:
        count = len(scope.get("options", []))
        derived[_key(scope["operation"])] = lambda op: isinstance(op.get("value"), int) and 0 <= op["value"] < count
    for block in _walk(page.get("blocks", [])):
        kind = block.get("t")
        if kind == "row":
            control = block.get("control", {})
            rule = _control_rule(control)
            operation = control.get("operation")
            if operation is not None:
                if rule is not None:
                    derived[_key(operation)] = rule
                else:
                    exact.add(_key(operation))
        elif kind == "tabs":
            count = len(block.get("items", []))
            derived[_key(block["operation"])] = lambda op, count=count: isinstance(op.get("value"), int) and 0 <= op["value"] < count
        elif kind == "ordered":
            count = len(block.get("items", []))
            derived[_key(block["operation"])] = lambda op, count=count: (
                op.get("action") in _LIST_ACTIONS and isinstance(op.get("index"), int) and 0 <= op["index"] <= count)
        elif kind == "buttons":
            exact.update(_key(item["operation"]) for item in block.get("items", []))
        elif kind == "callout" and block.get("action"):
            exact.add(_key(block["action"]["operation"]))
    return exact, derived


def accepts(page: dict | None, operation: dict) -> bool:
    """Is ``operation`` one this page offered? Exact, or an offered base completed with a valid client field."""
    if not page or not isinstance(operation, dict):
        return False
    exact, derived = operations(page)
    if _key(operation) in exact:
        return True
    if not any(field in operation for field in CLIENT_FIELDS):
        return False
    base = {key: value for key, value in operation.items() if key not in CLIENT_FIELDS}
    rule = derived.get(_key(base))
    return bool(rule and rule(operation))
