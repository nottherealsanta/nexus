"""Minimal Phase 1 context assembly (plan sections 4 and 5.2).

Phase 1 needs exactly one strategy: turn the session's structured history plus
workspace instructions into a provider-ready :class:`ModelRequest`. This module
deliberately stops there. There are no parts, no priority budgets, no token
counting, no compaction, and no prompt-cache breakpoints — those are Phase 3.

What it does own:

* **Complete structured history.** ``session.messages`` is copied verbatim, with
  every :class:`~nexus.model.message.ContentBlock` intact. The loop persists the
  current user message *before* calling :meth:`ContextManager.assemble`, so the
  active request is already present exactly once. Nothing is flattened and no
  message is duplicated.
* **Stable system text.** A small identity preamble followed by ``SOUL.md`` and
  ``MEMORY.md`` (filenames from the effective config), joined deterministically.
  Missing files contribute nothing.
* **Containment and size limits.** Every system file resolves through
  :func:`nexus.config.paths.resolve_within`, so ``../`` and symlink escapes fail
  closed, and an oversized file is a hard, named error rather than an unbounded
  read.
* **Model and sampling from config.** ``model``/``provider`` are derived from the
  effective model reference; sampling parameters come from ``ConfigV2`` when the
  effective configuration resolved to v2, and fall back to defaults for a legacy
  v1 bridge.

The manager can be constructed with a fixed ``config`` or a ``config_loader``
callable. A manager with a loader reloads on every :meth:`assemble`. To satisfy
the "one configuration per turn" rule, :meth:`for_turn` returns a *snapshot*
manager whose config and rendered system text are frozen; ``Session.send`` calls
it exactly once per turn, so every loop iteration and assembly in that turn sees
the same effective configuration, model, sampling, provider selection, and
system text. A config edit affects only the following turn and cannot race across
sessions because each send gets its own snapshot object.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..config import Config
from ..config.paths import resolve_within
from ..core.turn import TurnLimits
from ..errors import ConfigError
from ..model.request import ModelRequest, SamplingParams

__all__ = ["DEFAULT_MAX_FILE_BYTES", "IDENTITY_PREAMBLE", "ContextManager"]

#: Short, stable identity line. The plan's part 1 ("harness preamble,
#: capabilities, current date") is intentionally reduced here: dates are
#: non-deterministic and would defeat prompt-prefix stability, so they wait for
#: the Phase 3 parts/budget design.
IDENTITY_PREAMBLE = (
    "You are Nexus, a provider-agnostic agent harness working inside a local "
    "workspace. Be direct and accurate. Inspect the workspace when it helps, and "
    "verify your work before reporting success."
)

#: Phase-1 ceiling for a single system file. ``SOUL.md``/``MEMORY.md`` are
#: prose, not data payloads; a file this large is a configuration mistake and is
#: reported as such.
DEFAULT_MAX_FILE_BYTES = 1_000_000


class ContextManager:
    """Assembles a Phase-1 :class:`ModelRequest` from a session and config."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        config: Config | None = None,
        config_loader: Callable[[], Config] | None = None,
        identity: str = IDENTITY_PREAMBLE,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        system: str | None = None,
    ) -> None:
        if config is None and config_loader is None:
            raise ConfigError("ContextManager requires a config or config_loader")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("identity must be a nonempty string")
        if type(max_file_bytes) is not int or max_file_bytes < 1:
            raise ValueError("max_file_bytes must be a positive integer")
        if system is not None and not isinstance(system, str):
            raise ValueError("system must be a string or None")
        self.workspace = Path(workspace).resolve()
        self._config = config
        self._config_loader = config_loader
        self._identity = identity
        self.max_file_bytes = max_file_bytes
        #: Pre-rendered system text for a per-turn snapshot; ``None`` means render
        #: from the current config/files on each assemble.
        self._system = system

    # -- config ------------------------------------------------------------

    def effective_config(self) -> Config:
        """Return the configuration for this call, reloading if a loader exists."""
        if self._config_loader is not None:
            return self._config_loader()
        assert self._config is not None  # guarded by __init__
        return self._config

    def for_turn(self) -> ContextManager:
        """Return a snapshot manager with config and system text frozen.

        Called once per turn by ``Session.send`` so every assembly in the turn
        reuses one effective configuration. Reload happens only when a new turn
        asks for a new snapshot.
        """
        config = self.effective_config()
        return ContextManager(
            self.workspace,
            config=config,
            identity=self._identity,
            max_file_bytes=self.max_file_bytes,
            system=self._render_system(config),
        )

    def turn_limits(self) -> TurnLimits:
        """Turn limits derived from this manager's effective config snapshot.

        The per-turn snapshot exposes this so ``Session.send`` can derive limits
        and assemble requests from the *same* config load.
        """
        config = self.effective_config()
        v2 = getattr(config, "v2", None)
        if v2 is None:
            return TurnLimits()
        return TurnLimits(
            max_iterations=v2.agent.max_iterations,
            max_seconds=v2.agent.max_turn_seconds,
        )

    # -- protocol ----------------------------------------------------------

    def assemble(self, session) -> ModelRequest:
        """Build the request for the current state of ``session``.

        ``session.messages`` already includes the current user turn exactly once
        (the loop persists it first), so no separate user argument is needed.
        """
        config = self.effective_config()
        provider, model = self._model_reference(config)
        return ModelRequest(
            messages=list(session.messages),
            system=self._system_text(config),
            tools=[],
            params=self._sampling(config),
            model=model,
            provider=provider,
        )

    # -- system text -------------------------------------------------------

    def _system_text(self, config: Config) -> str:
        if self._system is not None:
            return self._system
        return self._render_system(config)

    def _render_system(self, config: Config) -> str:
        parts = [self._identity]
        instructions = self._read(config.instructions_file)
        if instructions.strip():
            parts.append(instructions)
        memory = self._read(config.memory_file)
        if memory.strip():
            parts.append(memory)
        return "\n\n".join(parts)

    def _read(self, filename: str) -> str:
        # ``resolve_within`` rejects ``../``, symlink escapes, and NUL bytes as
        # ConfigError. Reading a bounded prefix avoids the stat-then-read TOCTOU
        # race and never allocates more than the cap.
        path = resolve_within(self.workspace, filename)
        try:
            with open(path, "rb") as handle:
                data = handle.read(self.max_file_bytes + 1)
        except FileNotFoundError:
            return ""
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"Cannot read context file {filename!r}: {exc}"
            ) from exc
        if len(data) > self.max_file_bytes:
            raise ConfigError(
                f"Context file {filename!r} is too large "
                f"(> {self.max_file_bytes} bytes)"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(
                f"Cannot read context file {filename!r}: {exc}"
            ) from exc

    # -- model / sampling --------------------------------------------------

    @staticmethod
    def _sampling(config: Config) -> SamplingParams:
        v2 = getattr(config, "v2", None)
        params = getattr(getattr(v2, "model", None), "params", None)
        if params is None:
            return SamplingParams()
        return SamplingParams(
            temperature=params.temperature,
            max_output_tokens=params.max_output_tokens,
            thinking_budget=params.thinking_budget,
        )

    @staticmethod
    def _model_reference(config: Config) -> tuple[str | None, str | None]:
        ref = getattr(config, "model", None)
        if not ref:
            return None, None
        if "/" in ref:
            provider, _, model = ref.partition("/")
            return (provider or None), (model or None)
        return None, ref
