"""Small runtime-free entry seam for launching the optional Textual app."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ...client.protocol import Client
from .app import NexusTextualApp


async def run(
    client: Client,
    *,
    session: str = "default",
    reconnect: Callable[[], Awaitable[Client]] | None = None,
) -> int:
    """Run the Textual shell using an already-created host client."""
    app = NexusTextualApp(client, session=session, reconnect=reconnect)
    return await app.run_async()


__all__ = ["NexusTextualApp", "run"]
