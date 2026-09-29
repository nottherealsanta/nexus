"""Layered configuration with v1 flat-config compatibility (plan section 7).

``Config`` keeps the legacy flat API used by the current agent, context builder,
and provider. A flat (v1) ``nexus.toml`` loads exactly as before; a
``config_version = 2`` document loads through :func:`layers.load_effective` and
the derived facade fields are populated from the v2 sections.

Cross-layer mixing is bridged, not rejected: a v1 workspace beside a v2 user
config (or the reverse) resolves to v2, with v1 layers translated into the v2
shape. Mixing both shapes inside a single document is still an error.
"""
from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from . import layers
from .paths import resolve_within
from .schema import DEFAULT_CONTEXT_TOKENS, ConfigV2

__all__ = ["Config", "ConfigError", "ConfigV2", "resolve_within"]


@dataclass(frozen=True)
class Config:
    """Effective configuration. Extra fields describe the shape it resolved to."""

    executable: str = "codex"
    model: str | None = None
    sandbox: str = "workspace-write"
    timeout_seconds: float = 900
    context_chars: int = 64_000
    instructions_file: str = "SOUL.md"
    memory_file: str = "MEMORY.md"
    # Transitional provenance, deliberately excluded from equality and hashing so
    # the legacy frozen-Config API keeps working even when ``v2`` carries lists.
    version: int = field(default=1, compare=False)
    # ``repr=False`` so provider credentials held inside v2 cannot leak via
    # repr(Config); equality/hash already exclude it.
    v2: ConfigV2 | None = field(default=None, compare=False, repr=False)
    source: str | None = field(default=None, compare=False)

    def __post_init__(self):
        for key in ("executable", "sandbox", "instructions_file", "memory_file"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f"{key} must be a nonempty string")
        if self.model is not None and (
            not isinstance(self.model, str) or not self.model.strip()
        ):
            raise ValueError("model must be a nonempty string")
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("sandbox must be read-only or workspace-write")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if type(self.context_chars) is not int or self.context_chars < 1024:
            raise ValueError("context_chars must be an integer >= 1024")

    @classmethod
    def load(
        cls,
        workspace: str | Path,
        *,
        home: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        flags: Mapping[str, Any] | None = None,
        session: Mapping[str, Any] | None = None,
    ) -> Config:
        """Load and merge every layer. ``home``/``environ`` are injectable for tests."""
        effective = layers.load_effective(
            Path(workspace),
            Path(home) if home is not None else Path.home(),
            os.environ if environ is None else environ,
            flags=flags,
            session=session,
        )
        if effective.version == 2:
            return cls._from_v2(layers.build_v2(effective.values), source=effective.source)
        return cls(**effective.values, version=1, source=effective.source)

    @classmethod
    def _from_v2(cls, v2: ConfigV2, *, source: str | None) -> Config:
        codex = v2.providers.get("codex")
        return cls(
            executable=(codex.executable if codex and codex.executable else "codex"),
            model=v2.model_default(),
            sandbox=v2.agent.sandbox,
            timeout_seconds=(
                codex.timeout_seconds
                if codex and codex.timeout_seconds is not None
                else 900
            ),
            # The legacy context budget is measured in characters; v2 is in
            # tokens. 4 chars/token is the same approximation the current
            # context builder is calibrated against.
            context_chars=(v2.context.max_tokens or DEFAULT_CONTEXT_TOKENS) * 4,
            instructions_file=v2.agent.instructions_file,
            memory_file=v2.agent.memory_file,
            version=2,
            v2=v2,
            source=source,
        )

    def read(self, workspace: str | Path, filename: str) -> str:
        path = resolve_within(Path(workspace), filename)
        if not path.exists():
            return ""
        # Reject oversized files before allocating unbounded input.
        if path.stat().st_size > self.context_chars * 4:
            raise ValueError(f"Context file too large: {filename}")
        return path.read_text(encoding="utf-8")
