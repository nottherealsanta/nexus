from __future__ import annotations

import msgspec

from nexus.host import protocol as p
from nexus.ui.cli.client import Client


class _Transport:
    def __init__(self):
        self.command = None

    async def request(self, command):
        self.command = command
        return p.ProjectSessionsListResult()


async def test_project_sessions_defaults_and_keyword_search():
    transport = _Transport()
    client = Client(transport)
    assert isinstance(await client.project_sessions(), p.ProjectSessionsListResult)
    assert transport.command == p.ProjectSessionsList(limit=1000, query="")
    await client.project_sessions(limit=20, query="older % session")
    assert transport.command == p.ProjectSessionsList(limit=20, query="older % session")
    assert msgspec.json.decode(msgspec.json.encode(transport.command), type=p.Command) == transport.command
    assert msgspec.json.decode(b'{"type":"ProjectSessionsList"}', type=p.Command) == p.ProjectSessionsList()
