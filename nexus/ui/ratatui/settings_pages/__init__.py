"""One-page Settings areas (plan RATATUI_DESIGN_MOCKUPS_PLAN §9.3).

Each module here owns one area: ``build(workflows)`` returns the typed page
(``ui_support/settings_page.py``) and ``handle(workflows, operation)`` applies one
operation the client sent back through host commands. A page is rebuilt after every
operation, so what is shown is always what the host reports, never a local guess.
"""
from __future__ import annotations

import importlib

#: Areas that are one page. Others still use the older menu pages until they migrate.
PAGE_AREAS = ("appearance", "layout", "keys", "providers", "models", "voice", "live", "agents", "tools", "mcp", "skills")


def page_module(area: str):
    if area not in PAGE_AREAS:
        raise KeyError(area)
    return importlib.import_module(f"{__name__}.{area}")


#: Areas whose rows are inlined into the Settings root list (Grok-style overview).
OVERVIEW_AREAS = ("appearance", "layout")


_PROVIDER_TTL = 30.0
_PROVIDER_WAIT = 1.5


def remember_provider_count(workflows, connected: int, total: int) -> None:
    """Cache the Providers count (set by the Providers page, which already fetched the rows)."""
    import time
    workflows.provider_count = (time.monotonic(), f"{connected} of {total} connected")


async def provider_summary(workflows) -> str:
    """``2 of 6 connected`` for the root row: cached; one bounded refresh when stale, never blocking
    Settings beyond ``_PROVIDER_WAIT`` (a failure keeps the stale value, or none)."""
    import asyncio
    import time
    cached = getattr(workflows, "provider_count", None)
    if cached and time.monotonic() - cached[0] < _PROVIDER_TTL:
        return cached[1]
    try:
        async with asyncio.timeout(_PROVIDER_WAIT):
            rows = (await workflows.client.providers_status()).providers
        remember_provider_count(workflows, sum(bool(r.get("connected")) for r in rows), len(rows))
    except Exception:  # noqa: BLE001 - a summary is optional
        pass
    cached = getattr(workflows, "provider_count", None)
    return cached[1] if cached else ""


def cached_provider_summary(workflows) -> str:
    """The last Providers count the Providers page stored, however old; never touches the network, so
    opening Settings stays instant. Empty until the Providers page has been opened once."""
    cached = getattr(workflows, "provider_count", None)
    return cached[1] if cached else ""


async def build_overview(workflows) -> dict:
    """The root list's inlined rows: each overview area's own rows under its own heading, built by
    that area's builder so every operation is the one its drilled page offers and ``handle`` runs.
    ``summaries`` carries short drill-row values (Keyboard: the shortcut count)."""
    from ....ui_support import settings_page as sp
    from ....ui_support.shortcuts import LEADER_SHORTCUTS, SHORTCUTS
    blocks: list[dict] = []
    for area in OVERVIEW_AREAS:
        built = await page_module(area).build(workflows)
        blocks.append(sp.heading(built["title"]))
        blocks += [block for block in built["blocks"] if block.get("t") == "row"]
    overview = sp.page("overview", "Settings", blocks)
    overview["summaries"] = {"keys": f"{len(SHORTCUTS) + len(LEADER_SHORTCUTS)} shortcuts"}
    if providers := cached_provider_summary(workflows):
        overview["summaries"]["providers"] = providers
    return overview
