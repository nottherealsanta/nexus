"""Settings → Skills: reusable workflows (SKILL.md), global or per project."""
from __future__ import annotations

from ....ui_support import settings_page as sp
from ....ui_support.settings_help import SETTINGS_HELP
from . import files

AREA = "skills"


async def build(workflows) -> dict:
    blocks, root = await files.file_blocks(workflows, AREA, empty="No skills yet. New file creates one.")
    return sp.page(AREA, "Skills", blocks, intro=SETTINGS_HELP[AREA], footer=files.footer(workflows, AREA, root),
                   scope=files.scope_control(workflows, AREA))


async def handle(workflows, operation) -> None:
    if not await files.handle_scope(workflows, operation):
        raise ValueError(f"Unknown Skills operation {operation['key']!r}")
