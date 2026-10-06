"""Settings → Keyboard: every shortcut, read-only, from the one table the app itself uses."""
from __future__ import annotations

from ....ui_support import settings_page as sp
from ....ui_support.shortcuts import LEADER_SHORTCUTS, SHORTCUTS

AREA = "keys"


def _title(key: str) -> str:
    return "+".join(part.capitalize() if len(part) > 1 else part.upper() for part in key.split("+"))


async def build(workflows) -> dict:
    direct = [[description, _title(key), "Global"] for key, _, description in SHORTCUTS]
    leader = [[description, f"Ctrl+X {letter.upper()}", "Leader"] for letter, _, description in LEADER_SHORTCUTS]
    extra = [["Dismiss all toasts", "Ctrl+X X", "Leader"], ["Run the newest toast's action", "Ctrl+X A", "Leader"]]
    blocks = [
        sp.heading("SHORTCUTS"),
        sp.table([("Action", 0), ("Keys", 24), ("Where", 8)], direct),
        sp.gap(),
        sp.heading("CTRL+X LEADER · press Ctrl+X, then a key"),
        sp.table([("Action", 0), ("Keys", 24), ("Where", 8)], leader + extra),
        sp.gap(),
        sp.heading("IN LISTS AND SETTINGS"),
        sp.table([("Action", 0), ("Keys", 24), ("Where", 8)], [
            ["Move", "Up / Down", "Lists"], ["Change a value", "Left / Right", "Settings"], ["Toggle, open", "Space / Enter", "Settings"],
            ["Reorder a model", "Alt+Up / Alt+Down", "Settings"], ["Remove a model", "Delete", "Settings"],
            ["Switch tier", "Ctrl+PageUp / Ctrl+PageDown", "Settings"], ["Close", "Esc", "Settings"],
        ]),
    ]
    return sp.page(AREA, "Keyboard", blocks, footer="Read-only: shortcuts are defined in code.",
                   intro="Every shortcut, from the same table the app uses.")


async def handle(workflows, operation) -> None:
    raise ValueError("The Keyboard page is read-only")
