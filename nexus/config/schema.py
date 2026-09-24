"""msgspec structs for the v2 layered configuration (plan section 7).

Every struct forbids unknown fields, so a typo in ``nexus.toml`` is a hard error
rather than a silently ignored setting. Field defaults are the built-in layer.
"""
from __future__ import annotations

import math
import re
from typing import Literal
from urllib.parse import urlsplit

import msgspec

from ..util import redact_secrets

Compaction = Literal["drop_oldest", "evict_tool_results", "summarize", "hybrid"]
PermissionMode = Literal["allow", "ask", "deny"]
UnattendedMode = Literal["deny", "allow", "fail_turn"]
SandboxMode = Literal["read-only", "workspace-write"]

_PERMISSION_MODES = frozenset({"allow", "ask", "deny"})
_UNATTENDED_MODES = frozenset({"deny", "allow", "fail_turn"})

#: Hosts for which plain ``http`` is tolerated (a loopback dev server such as
#: Ollama). Any remote endpoint must use TLS.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _validate_base_url(value: str) -> None:
    """Reject a provider ``base_url`` that could misdirect or leak a request.

    ``file:`` and any non-http(s) scheme are refused, and plain ``http`` is
    allowed only for a loopback host. Embedded ``user:pass@`` userinfo is
    *allowed* (some gateways require it) but is therefore **always redacted** by
    every repr, log, and error path. A ``${...}`` reference is left for
    request-time resolution. The config layer never needs ``httpx`` for this.
    """
    text = value.strip()
    if not text:
        raise ValueError("providers.*.base_url must be a non-empty URL")
    if text.startswith("${"):
        return
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ValueError(
            f"providers.*.base_url is not a valid URL: {exc}"
        ) from exc
    scheme = parts.scheme.lower()
    if scheme == "file":
        raise ValueError("providers.*.base_url must not use the file: scheme")
    if scheme not in ("http", "https"):
        raise ValueError("providers.*.base_url must use http or https")
    if not parts.hostname:
        raise ValueError("providers.*.base_url must name a host")
    if scheme == "http":
        host = (parts.hostname or "").lower()
        if host not in _LOOPBACK_HOSTS and not host.startswith("127."):
            raise ValueError(
                "providers.*.base_url may use plain http only for a loopback "
                "host; use https for a remote endpoint"
            )

#: Canonical model-registry defaults (plan section 15.9). Kept as literals here
#: so ``nexus.config`` never imports the model layer (``import nexus`` stays
#: lazy); the runtime passes these values straight into ``ModelRegistry``.
DEFAULT_CATALOGUE_URL = "https://models.dev/api.json"
DEFAULT_REFRESH_TTL_DAYS = 7.0


class ModelParams(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking_budget: int | None = None


class AgentSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str = "general"
    profile: str = "coding"
    instructions_file: str = "SOUL.md"
    memory_file: str = "MEMORY.md"
    max_iterations: int = 60
    max_turn_seconds: float = 1800
    # Carried so a flat v1 layer can be bridged into v2 without losing it.
    sandbox: SandboxMode = "workspace-write"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("agent.name must be a nonempty string")
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ValueError("agent.profile must be a nonempty string")


class ModelSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    default: str | None = None
    fast: str | None = None
    plan: str | None = None
    fallback: list[str] = msgspec.field(default_factory=list)
    params: ModelParams = msgspec.field(default_factory=ModelParams)

    def __post_init__(self) -> None:
        if not isinstance(self.fallback, list) or any(
            not isinstance(ref, str) or not ref.strip() for ref in self.fallback
        ):
            raise ValueError(
                "model.fallback must be a list of non-empty model references"
            )


class ModelsSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Canonical ``[models]`` section (plan sections 15.3-15.4, 15.9).

    ``[model]`` remains the compatibility section; the two are reconciled in
    :class:`ConfigV2`, which rejects a field the two set to different values.
    ``tiers`` is the ``[models.tiers]`` table: ``tier -> [reference, ...]``.
    """

    default: str | None = None
    fast: str | None = None
    plan: str | None = None
    refresh_ttl_days: float = DEFAULT_REFRESH_TTL_DAYS
    catalogue_url: str = DEFAULT_CATALOGUE_URL
    offline: bool = False
    tiers: dict[str, list[str]] = msgspec.field(default_factory=dict)
    #: Ordered provider/model references tried only on a provider-level failure
    #: *before* any output has streamed (plan section 8). A refusal or a partial
    #: stream never falls back.
    fallback: list[str] = msgspec.field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("default", "fast", "plan"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"models.{name} must be a nonempty string")
        if not isinstance(self.fallback, list) or any(
            not isinstance(ref, str) or not ref.strip() for ref in self.fallback
        ):
            raise ValueError(
                "models.fallback must be a list of non-empty model references"
            )
        ttl = self.refresh_ttl_days
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl)
            or ttl < 0
        ):
            raise ValueError("models.refresh_ttl_days must be finite and >= 0")
        if not isinstance(self.catalogue_url, str) or self.catalogue_url.partition(
            "://"
        )[0].lower() not in ("https", "http"):
            raise ValueError("models.catalogue_url must be an http(s) URL")
        for tier, refs in self.tiers.items():
            if not isinstance(tier, str) or not tier.strip():
                raise ValueError("models.tiers keys must be nonempty tier names")
            if (
                not isinstance(refs, list)
                or not refs
                or not all(isinstance(ref, str) and ref.strip() for ref in refs)
            ):
                raise ValueError(
                    f"models.tiers.{tier} must be a nonempty list of references"
                )


class AgentsSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Canonical ``[agents]`` section (plan sections 5.6, 15.8).

    Bounds every subagent tree: depth, concurrent children, the requested tier,
    fan-out, and the aggregate token/cost budget. A child can never exceed its
    parent; these values only ever clamp further. ``max_tier`` is validated
    against the runtime tier table (this layer does not import ``model``).
    """

    enabled: bool = True
    default_type: str = "general"
    max_depth: int = 3
    max_concurrent: int = 4
    max_fanout: int | None = 16
    max_tier: str = "medium"
    token_budget: int | None = None
    cost_budget: float | None = None
    seed_roles: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.default_type, str) or not self.default_type.strip():
            raise ValueError("agents.default_type must be a nonempty string")
        if not isinstance(self.max_tier, str) or not self.max_tier.strip():
            raise ValueError("agents.max_tier must be a nonempty string")
        for name in ("max_depth",):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"agents.{name} must be a non-negative integer")
        if (
            isinstance(self.max_concurrent, bool)
            or not isinstance(self.max_concurrent, int)
            or self.max_concurrent < 1
        ):
            raise ValueError("agents.max_concurrent must be a positive integer")
        if self.max_fanout is not None and (
            isinstance(self.max_fanout, bool)
            or not isinstance(self.max_fanout, int)
            or self.max_fanout < 1
        ):
            raise ValueError("agents.max_fanout must be a positive integer or null")
        if self.token_budget is not None and (
            isinstance(self.token_budget, bool)
            or not isinstance(self.token_budget, int)
            or self.token_budget < 0
        ):
            raise ValueError("agents.token_budget must be a non-negative integer or null")
        if self.cost_budget is not None and (
            isinstance(self.cost_budget, bool)
            or not isinstance(self.cost_budget, (int, float))
            or not math.isfinite(self.cost_budget)
            or self.cost_budget < 0
        ):
            raise ValueError("agents.cost_budget must be finite and >= 0 or null")


class HooksSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Canonical ``[hooks]`` section (plan section 5.7).

    ``enabled = false`` disables discovery/execution entirely; the hook files
    remain on disk. Timeouts and non-zero policies are declared per hook in
    ``hooks.toml`` and are not configurable here.
    """

    enabled: bool = True


class ProviderSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    api: str | None = None
    auth: str | None = None
    profile: str | None = None
    executable: str | None = None
    timeout_seconds: float | None = None
    # -- OpenCode ACP subprocess agent (``kind = "opencode_agent"``) ---------
    #: Explicit argv override for the agent binary (no shell is ever used).
    command: list[str] | None = None
    #: Arguments for the default executable; ignored when ``command`` is set.
    args: list[str] | None = None
    #: How ACP permission requests are answered: ``deny`` (default) or ``allow``.
    #: This is *not* a sandbox -- see the adapter's documentation.
    permission_policy: str | None = None
    #: Extra parent environment variable names copied into the agent process.
    inherit_env: list[str] | None = None
    #: Literal environment values set on the agent process (may carry secrets).
    env: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.auth is not None and self.auth != "chatgpt_oauth":
            raise ValueError("providers.*.auth must be 'chatgpt_oauth'")
        if self.profile is not None and (
            not isinstance(self.profile, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.profile)
        ):
            raise ValueError("providers.*.profile must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
        if self.auth == "chatgpt_oauth":
            if self.api_key is not None or self.base_url is not None or self.api != "responses":
                raise ValueError("chatgpt_oauth requires api='responses' and cannot use api_key or base_url")
            if any(value is not None for value in (self.executable, self.command, self.args, self.env, self.inherit_env, self.permission_policy)):
                raise ValueError("chatgpt_oauth cannot be combined with executable or agent fields")
            if self.kind not in (None, "openai", "codex"):
                raise ValueError("chatgpt_oauth is supported only by the codex/OpenAI provider")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("providers.*.timeout_seconds must be a positive finite number")
        for name, value in (
            ("command", self.command),
            ("args", self.args),
            ("inherit_env", self.inherit_env),
        ):
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)
            ):
                raise ValueError(f"providers.*.{name} must be a list of strings")
        if self.env is not None and (
            not isinstance(self.env, dict)
            or any(
                not isinstance(key, str) or not isinstance(val, str)
                for key, val in self.env.items()
            )
        ):
            raise ValueError("providers.*.env must be a mapping of strings")
        if self.permission_policy is not None and self.permission_policy not in (
            "deny",
            "allow",
        ):
            raise ValueError(
                "providers.*.permission_policy must be 'deny' or 'allow'"
            )
        if self.base_url is not None:
            _validate_base_url(self.base_url)

    @staticmethod
    def _redact_list(values: list[str] | None) -> list[str] | None:
        if not values:
            return values
        return [redact_secrets(item) for item in values]

    def __repr__(self) -> str:
        # Never render a literal credential (or its reference) through repr, so
        # ``repr(config.v2)`` / tracebacks / logs cannot leak it. ``env`` values
        # are hidden entirely; a ``base_url`` has its userinfo and any token
        # pattern scrubbed, and ``command``/``args`` are scrubbed too because an
        # argv can carry a credential (for example ``--token sk-...``).
        api_key = "***" if self.api_key else None
        base_url = redact_secrets(self.base_url) if self.base_url else None
        env = (
            {key: "***" for key in self.env} if self.env else self.env
        )
        command = self._redact_list(self.command)
        args = self._redact_list(self.args)
        executable = (
            redact_secrets(self.executable) if self.executable else None
        )
        return (
            f"ProviderSection(kind={self.kind!r}, api_key={api_key!r}, "
            f"base_url={base_url!r}, api={self.api!r}, auth={self.auth!r}, profile={self.profile!r}, "
            f"executable={executable!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"command={command!r}, args={args!r}, "
            f"permission_policy={self.permission_policy!r}, "
            f"inherit_env={self.inherit_env!r}, env={env!r})"
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

    def __post_init__(self) -> None:
        if (
            isinstance(self.compact_at_fraction, bool)
            or not isinstance(self.compact_at_fraction, (int, float))
            or not math.isfinite(self.compact_at_fraction)
            or self.compact_at_fraction <= 0.0
            or self.compact_at_fraction > 1.0
        ):
            raise ValueError(
                "context.compact_at_fraction must be a finite number in (0, 1]"
            )
        if (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens < 0
        ):
            raise ValueError("context.max_tokens must be a non-negative integer")
        if (
            isinstance(self.safety_margin_tokens, bool)
            or not isinstance(self.safety_margin_tokens, int)
            or self.safety_margin_tokens < 0
        ):
            raise ValueError(
                "context.safety_margin_tokens must be a non-negative integer"
            )


class PermissionsSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    mode: PermissionMode = "ask"
    allow: list[str] = msgspec.field(default_factory=list)
    ask: list[str] = msgspec.field(default_factory=list)
    deny: list[str] = msgspec.field(default_factory=list)
    write_roots: list[str] = msgspec.field(default_factory=lambda: ["./"])
    #: Hard read boundaries. Unlike tool arguments these may use ``~``/``$HOME``.
    read_denyroots: list[str] = msgspec.field(default_factory=list)
    on_unattended: UnattendedMode = "deny"

    def __post_init__(self) -> None:
        if self.mode not in _PERMISSION_MODES:
            raise ValueError(
                f"permissions.mode must be one of "
                f"{', '.join(sorted(_PERMISSION_MODES))}"
            )
        if self.on_unattended not in _UNATTENDED_MODES:
            raise ValueError(
                f"permissions.on_unattended must be one of "
                f"{', '.join(sorted(_UNATTENDED_MODES))}"
            )
        for label, values in (
            ("allow", self.allow),
            ("ask", self.ask),
            ("deny", self.deny),
            ("write_roots", self.write_roots),
            ("read_denyroots", self.read_denyroots),
        ):
            if not isinstance(values, list) or not all(
                isinstance(item, str) for item in values
            ):
                raise ValueError(f"permissions.{label} must be a list of strings")
            if label in ("write_roots", "read_denyroots") and any(
                not item.strip() or "\x00" in item for item in values
            ):
                raise ValueError(
                    f"permissions.{label} entries must be non-empty path strings"
                )


class ToolsSection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    bash_timeout_s: float = 120
    #: Strict deadline for the isolated ``Grep`` regex worker. Finite, positive.
    grep_timeout_s: float = 5.0
    max_result_tokens: int = 25000
    max_parallel: int = 8

    def __post_init__(self) -> None:
        if (
            isinstance(self.bash_timeout_s, bool)
            or not isinstance(self.bash_timeout_s, (int, float))
            or not math.isfinite(self.bash_timeout_s)
            or self.bash_timeout_s <= 0
        ):
            raise ValueError("tools.bash_timeout_s must be a positive finite number")
        if (
            isinstance(self.grep_timeout_s, bool)
            or not isinstance(self.grep_timeout_s, (int, float))
            or not math.isfinite(self.grep_timeout_s)
            or self.grep_timeout_s <= 0
        ):
            raise ValueError("tools.grep_timeout_s must be a positive finite number")
        if (
            isinstance(self.max_result_tokens, bool)
            or not isinstance(self.max_result_tokens, int)
            or self.max_result_tokens <= 0
        ):
            raise ValueError("tools.max_result_tokens must be a positive integer")
        if (
            isinstance(self.max_parallel, bool)
            or not isinstance(self.max_parallel, int)
            or self.max_parallel <= 0
        ):
            raise ValueError("tools.max_parallel must be a positive integer")


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
    models: ModelsSection = msgspec.field(default_factory=ModelsSection)
    providers: dict[str, ProviderSection] = msgspec.field(default_factory=dict)
    context: ContextSection = msgspec.field(default_factory=ContextSection)
    permissions: PermissionsSection = msgspec.field(default_factory=PermissionsSection)
    tools: ToolsSection = msgspec.field(default_factory=ToolsSection)
    ext: ExtSection = msgspec.field(default_factory=ExtSection)
    agents: AgentsSection = msgspec.field(default_factory=AgentsSection)
    hooks: HooksSection = msgspec.field(default_factory=HooksSection)
    mcp: MCPSection = msgspec.field(default_factory=MCPSection)
    session: SessionSection = msgspec.field(default_factory=SessionSection)
    telemetry: TelemetrySection = msgspec.field(default_factory=TelemetrySection)

    def __post_init__(self) -> None:
        for provider_name, provider in self.providers.items():
            if provider.auth == "chatgpt_oauth" and provider_name != "codex":
                raise ValueError("chatgpt_oauth is supported only by providers.codex")
        # ``[models]`` is canonical and ``[model]`` is compatibility. A field
        # both sections set must agree; disagreement is a conflict, not a
        # precedence puzzle (plan section 15.9).
        for name in ("default", "fast", "plan"):
            canonical = getattr(self.models, name)
            compat = getattr(self.model, name)
            if canonical is not None and compat is not None and canonical != compat:
                raise ValueError(
                    f"conflicting model {name!r}: "
                    f"[models].{name}={canonical!r} but [model].{name}={compat!r}"
                )
        if (
            self.models.fallback
            and self.model.fallback
            and self.models.fallback != self.model.fallback
        ):
            raise ValueError(
                "conflicting model aliases: "
                f"[models].fallback={self.models.fallback!r} but "
                f"[model].fallback={self.model.fallback!r}"
            )

    def model_default(self) -> str | None:
        """Effective default model reference: canonical then compatibility."""
        return self.models.default or self.model.default

    def model_fast(self) -> str | None:
        return self.models.fast or self.model.fast

    def model_plan(self) -> str | None:
        return self.models.plan or self.model.plan

    def model_fallback(self) -> list[str]:
        """Effective ordered fallback references: canonical then compatibility."""
        return list(self.models.fallback or self.model.fallback)

    def models_configured(self) -> bool:
        """Whether ``[models]`` was set to anything beyond its defaults."""
        return self.models != ModelsSection()


__all__ = [
    "DEFAULT_CATALOGUE_URL",
    "DEFAULT_REFRESH_TTL_DAYS",
    "AgentSection",
    "AgentsSection",
    "ConfigV2",
    "ContextLimits",
    "ContextSection",
    "ExtSection",
    "HooksSection",
    "MCPSection",
    "ModelParams",
    "ModelSection",
    "ModelsSection",
    "PermissionsSection",
    "ProviderSection",
    "SandboxMode",
    "SessionSection",
    "TelemetrySection",
    "ToolsSection",
]
