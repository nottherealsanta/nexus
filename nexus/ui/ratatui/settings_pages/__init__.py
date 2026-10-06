"""One-page Settings areas (plan RATATUI_DESIGN_MOCKUPS_PLAN §9.3).

Each module here owns one area: ``build(workflows)`` returns the typed page
(``ui_support/settings_page.py``) and ``handle(workflows, operation)`` applies one
operation the client sent back through host commands. A page is rebuilt after every
operation, so what is shown is always what the host reports, never a local guess.
"""
from __future__ import annotations

import importlib

#: Areas that are one page. Others still use the older menu pages until they migrate.
PAGE_AREAS = ("appearance", "layout", "keys", "providers", "models", "voice", "agents", "tools", "mcp", "skills")


def page_module(area: str):
    if area not in PAGE_AREAS:
        raise KeyError(area)
    return importlib.import_module(f"{__name__}.{area}")
