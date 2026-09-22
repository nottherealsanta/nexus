"""MCP integration (plan section 5.5).

The official ``mcp`` package is wrapped behind normalized types so no other
layer imports it. The packet composes three pieces: the client
(:mod:`nexus.mcp.client`), the lifecycle manager (:mod:`nexus.mcp.manager`), and
the tool/resource/prompt bridge (:mod:`nexus.mcp.bridge`).

Everything resolves lazily through PEP 562, so ``import nexus.mcp`` does not
import ``httpx``, ``msgspec``, or the upstream ``mcp`` package, and a runtime
that never configures MCP pays nothing for it.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MCPClient",
    "MCPHealth",
    "MCPManager",
    "MCPResourceTemplate",
    "MCPServerConfig",
    "MCPServerSnapshot",
    "MCPSnapshot",
    "ServerDefinition",
    "parse_server_config",
]

_LAZY = {
    "MCPClient": (".client", "MCPClient"),
    "MCPResourceTemplate": (".client", "MCPResourceTemplate"),
    "MCPServerConfig": (".client", "MCPServerConfig"),
    "parse_server_config": (".client", "parse_server_config"),
    "MCPHealth": (".manager", "MCPHealth"),
    "MCPManager": (".manager", "MCPManager"),
    "MCPServerSnapshot": (".manager", "MCPServerSnapshot"),
    "MCPSnapshot": (".manager", "MCPSnapshot"),
    "ServerDefinition": (".manager", "ServerDefinition"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
