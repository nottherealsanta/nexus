"""msgspec structs for the v2 layered configuration (plan section 7).

Every struct forbids unknown fields, so a typo in ``nexus.toml`` is a hard error
rather than a silently ignored setting. Field defaults are the built-in layer.
"""
from __future__ import annotations

from typing import Literal

import msgspec

Compaction = Literal["drop_oldest", "evict_tool_results", "summarize", "hybrid"]
PermissionMode = Literal["allow", "ask", "deny"]
UnattendedMode = Literal["deny", "allow", "fail_turn"]
SandboxMode = Literal["read-only", "workspace-write"]


class ModelParams(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking_budget: int | None = None


class AgentSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    profile: str = "coding"
    instructions_file: str = "SOUL.md"
    memory_file: str = "MEMORY.md"
    max_iterations: int = 60
    max_turn_seconds: float = 1800
    # Carried so a flat v1 layer can be bridged into v2 without losing it.
    sandbox: SandboxMode = "workspace-write"


class ModelSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    default: str | None = None
    fast: str | None = None
    plan: str | None = None
    fallback: list[str] = msgspec.field(default_factory=list)
    params: ModelParams = msgspec.field(default_factory=ModelParams)


class ProviderSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    api: str | None = None
    executable: str | None = None
    timeout_seconds: float | None = None

    def __repr__(self) -> str:
        # Never render a literal credential (or its reference) through repr, so
        # ``repr(config.v2)`` / tracebacks / logs cannot leak it.
        api_key = "***" if self.api_key else None
        return (
            f"ProviderSection(kind={self.kind!r}, api_key={api_key!r}, "
            f"base_url={self.base_url!r}, api={self.api!r}, "
            f"executable={self.executable!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


class ContextLimits(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    memory: int = 8000
    skills_index: int = 4000
    environment: int = 2000
    attachments: int = 20000


class ContextSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    max_tokens: int = 180000
    safety_margin_tokens: int = 4000
    compaction: Compaction = "hybrid"
    compact_at_fraction: float = 0.85
    limits: ContextLimits = msgspec.field(default_factory=ContextLimits)


class PermissionsSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    mode: PermissionMode = "ask"
    allow: list[str] = msgspec.field(default_factory=list)
    ask: list[str] = msgspec.field(default_factory=list)
    deny: list[str] = msgspec.field(default_factory=list)
    write_roots: list[str] = msgspec.field(default_factory=lambda: ["./"])
    on_unattended: UnattendedMode = "deny"


class ToolsSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    bash_timeout_s: float = 120
    max_result_tokens: int = 25000
    max_parallel: int = 8


class ExtSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    enabled: bool = True
    watch_interval_ms: int = 500
    dirs: list[str] = msgspec.field(
        default_factory=lambda: [".nexus/tools", "~/.nexus/tools"]
    )
    quarantine: bool = True
    max_file_bytes: int = 262144


class MCPSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    enabled: bool = True
    connect_timeout_s: float = 20
    restart_max: int = 5


class SessionSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    store: str = "jsonl"
    snapshot_every: int = 20


class TelemetrySection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    log_level: str = "info"
    log_file: str = ".nexus/logs/nexus.log"
    redact: list[str] = msgspec.field(
        default_factory=lambda: ["api_key", "token", "authorization", "password", "secret"]
    )


class ConfigV2(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    config_version: Literal[2] = 2
    agent: AgentSection = msgspec.field(default_factory=AgentSection)
    model: ModelSection = msgspec.field(default_factory=ModelSection)
    providers: dict[str, ProviderSection] = msgspec.field(default_factory=dict)
    context: ContextSection = msgspec.field(default_factory=ContextSection)
    permissions: PermissionsSection = msgspec.field(default_factory=PermissionsSection)
    tools: ToolsSection = msgspec.field(default_factory=ToolsSection)
    ext: ExtSection = msgspec.field(default_factory=ExtSection)
    mcp: MCPSection = msgspec.field(default_factory=MCPSection)
    session: SessionSection = msgspec.field(default_factory=SessionSection)
    telemetry: TelemetrySection = msgspec.field(default_factory=TelemetrySection)


__all__ = [
    "AgentSection",
    "ConfigV2",
    "ContextLimits",
    "ContextSection",
    "ExtSection",
    "MCPSection",
    "ModelParams",
    "ModelSection",
    "PermissionsSection",
    "ProviderSection",
    "SandboxMode",
    "SessionSection",
    "TelemetrySection",
    "ToolsSection",
]
