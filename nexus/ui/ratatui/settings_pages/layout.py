"""Settings → Layout: which panels show. Local to this shell (saved in the terminal preferences)."""
from __future__ import annotations

from ....ui_support import settings_page as sp

AREA = "layout"
#: key, label, description
PANELS = (
    ("sessions_sidebar", "Sessions sidebar", "Ctrl+B. The list of sessions on the left; a drawer on narrow terminals."),
    ("details_sidebar", "Details sidebar", "Ctrl+L. Session facts, modified files, MCP servers and logs on the right."),
    ("context_preview", "Show context header", "The labelled blocks that open every conversation: system prompt, tools, skills, MCP."),
)


async def build(workflows) -> dict:
    values = workflows.shell.preferences.values
    blocks = [sp.row(key, label, sp.toggle(values[key], sp.op(AREA, "toggle", pref=key)), description=description)
              for key, label, description in PANELS]
    blocks += [sp.gap(), sp.buttons("reset", [("Reset to default", sp.op(AREA, "reset"), "ghost")])]
    return sp.page(AREA, "Layout", blocks, footer="Saved in ~/.config/nexus/tui.json · applies at once",
                   intro="Panels hide automatically on narrow terminals.")


async def handle(workflows, operation) -> None:
    prefs = workflows.shell.preferences
    keys = {key for key, _, _ in PANELS}
    if operation["key"] == "toggle":
        pref = operation["pref"]
        if pref not in keys:
            raise ValueError("Unknown layout setting")
        prefs.set(pref, bool(operation["value"]))
    elif operation["key"] == "reset":
        for key in keys:
            prefs.set(key, prefs.DEFAULTS[key])
        workflows.shell.flash("Layout reset to default", "success")
    else:
        raise ValueError(f"Unknown Layout operation {operation['key']!r}")
