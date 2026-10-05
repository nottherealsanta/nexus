"""Client default-model calls go through ``_request`` (regression: ``Client.execute`` never existed)."""
from __future__ import annotations

import asyncio

from nexus.client.protocol import Client
from nexus.host import protocol as p


class _Transport:
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def request(self, command):
        self.commands.append(command)
        return command


def test_default_model_calls_use_request():
    transport = _Transport()
    client = Client(transport)  # type: ignore[arg-type]
    asyncio.run(client.default_model_settings())
    asyncio.run(client.default_model_set(["a/b"]))
    assert [type(c) for c in transport.commands] == [p.DefaultModelSettings, p.DefaultModelSet]
