"""Settings → Appearance: the terminal theme. Always local to this shell, so no scope control."""
from __future__ import annotations

from ....ui_support import settings_page as sp

AREA = "appearance"
THEMES = (("Dark", "nexus-dark"), ("Light", "nexus-light"))


async def build(workflows) -> dict:
    current = workflows.shell.preferences.values["theme"]
    active = next((i for i, (_, value) in enumerate(THEMES) if value == current), 0)
    blocks = [
        sp.row("theme", "Theme", sp.segmented([label for label, _ in THEMES], active, sp.op(AREA, "theme"), values=[v for _, v in THEMES]),
               description="How Nexus looks in this terminal."),
        sp.gap(),
        sp.buttons("reset", [("Reset to default", sp.op(AREA, "reset"), "ghost")]),
    ]
    return sp.page(AREA, "Appearance", blocks, footer="Saved in ~/.config/nexus/tui.json · applies at once",
                   intro="Settings for this terminal shell.")


async def handle(workflows, operation) -> None:
    prefs = workflows.shell.preferences
    if operation["key"] == "theme":
        if operation["value"] not in {value for _, value in THEMES}:
            raise ValueError("Unknown theme")
        prefs.set("theme", operation["value"])
        workflows.shell.flash("Theme saved", "success")
    elif operation["key"] == "reset":
        prefs.set("theme", prefs.DEFAULTS["theme"])
        workflows.shell.flash("Theme reset to default", "success")
    else:
        raise ValueError(f"Unknown Appearance operation {operation['key']!r}")
