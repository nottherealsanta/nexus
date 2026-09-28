"""Small runtime-free entry seam for launching the optional Textual app."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from ...client.protocol import Client
from .app import NexusTextualApp


def preferences_path() -> Path:
    """Per-user shell preferences (theme, panels): ``$XDG_CONFIG_HOME/nexus/tui.json``."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "nexus" / "tui.json"


async def run(
    client: Client,
    *,
    session: str = "default",
    reconnect: Callable[[], Awaitable[Client]] | None = None,
) -> int:
    """Run the Textual shell using an already-created host client."""
    app = NexusTextualApp(
        client, session=session, reconnect=reconnect, preferences_path=preferences_path()
    )
    return await app.run_async()


__all__ = ["NexusTextualApp", "preferences_path", "run"]
