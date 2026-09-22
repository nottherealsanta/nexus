"""``Skill``: progressive disclosure of a skill body or bundled resource.

Plan section 5.4. A skill's frontmatter is advertised in the context index as a
single ``name: description`` line; the body is loaded **only** when the model
calls ``Skill(name="...")``. This module implements that invocation path and the
session/turn-local activation overlay it produces.

What one call does
------------------
1. Resolve the skill through the narrow :class:`~nexus.tools.spec.SkillServiceView`
   seam on :class:`~nexus.tools.spec.ToolContext` (never a ``Runtime``, never an
   import of the concrete manager).
2. Return the body -- or, when ``resource`` is given, one bundled
   ``scripts/``/``references/`` file -- as a **bounded, delimited** document with
   provenance (name, source tier, manifest generation) and content hashes. The
   bytes come from the manager's refresh-time snapshot, so an invocation under a
   pinned generation cannot observe a later edit.
3. Build an immutable :class:`~nexus.skills.activation.SkillActivation` by
   intersecting the live manifest's tools with the active profile and the
   skill's declared ``allowed-tools``/``bundles``. The overlay is *always* a
   subset of the authority the turn already holds: a declaration can narrow but
   never widen, and an unknown tool/bundle contributes nothing.
4. Record that overlay into a session/turn-local sink and emit
   ``skill.invoked``/``skill.completed`` through the context event seam. It
   **never mutates the manifest** and registers no tool.

Service absence
---------------
Without an injected skill service the tool reports an actionable error rather
than pretending a skill does not exist. Without an extension service the
activation is built against an empty authority (so it stays empty), never
against the declaration alone.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from ...errors import ToolError
from ...skills.activation import SkillActivation
from ...skills.invocation import (
    DEFAULT_MAX_OUTPUT_BYTES,
    render_invocation,
)
from ..bundles import BUNDLES, DEFAULT_PROFILE, get_profile, profile_tools
from ..spec import (
    ExtensionServiceView,
    SkillServiceView,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)

__all__ = [
    "DEFAULT_MAX_RESOURCE_OUTPUT_BYTES",
    "RESOURCE_BEGIN_DELIMITER",
    "RESOURCE_END_DELIMITER",
    "SKILL_SPEC",
    "SkillActivationLog",
    "activation_log_for",
    "activation_sink_for",
    "bind_log",
    "build_activation",
    "clear_default_log",
    "get_default_log",
    "reset_log",
    "run",
    "set_default_log",
    "use_log",
]

#: The whole resource document is capped, delimiters and provenance included.
DEFAULT_MAX_RESOURCE_OUTPUT_BYTES = DEFAULT_MAX_OUTPUT_BYTES
RESOURCE_BEGIN_DELIMITER = "-----BEGIN SKILL RESOURCE-----"
RESOURCE_END_DELIMITER = "-----END SKILL RESOURCE-----"
#: The context builder's 4 chars/token heuristic (mirrors the fs tools).
_CHARS_PER_TOKEN = 4
_FALLBACK_MAX_RESULT_TOKENS = 25_000


class _SkillToolError(ToolError):
    """A model-visible failure while invoking a skill."""


# ---------------------------------------------------------------------------
# Session/turn-local activation log
# ---------------------------------------------------------------------------


@dataclass
class SkillActivationLog:
    """A session/turn-keyed store of the most recent ``SkillActivation``.

    The log is deliberately *not* a grant and *not* the manifest: it holds only
    the immutable overlays a ``Skill`` call produced, so the next loop iteration
    can read the scoped tool set for this session/turn. One activation per
    ``(session, turn)`` is retained; a later call in the same turn replaces the
    earlier one (the union is not taken -- the caller decides).
    """

    _by_key: dict[tuple[str, int], SkillActivation] = field(default_factory=dict)

    def record(self, activation: SkillActivation) -> SkillActivation:
        if not isinstance(activation, SkillActivation):
            raise _SkillToolError("activation must be a SkillActivation")
        key = (activation.session or "", int(activation.turn))
        self._by_key[key] = activation
        return activation

    def get(self, session: str | None, turn: int = 0) -> SkillActivation | None:
        return self._by_key.get((session or "", int(turn)))

    def active(
        self, session: str | None = None, turn: int | None = None
    ) -> tuple[SkillActivation, ...]:
        """Overlays for a session (all turns) or one exact session/turn."""
        if session is None:
            return tuple(
                value for _key, value in sorted(self._by_key.items())
            )
        if turn is None:
            return tuple(
                value
                for (sess, _turn), value in sorted(self._by_key.items())
                if sess == session
            )
        exact = self.get(session, turn)
        return () if exact is None else (exact,)

    def clear(self, session: str | None = None) -> None:
        if session is None:
            self._by_key.clear()
            return
        for key in [key for key in self._by_key if key[0] == session]:
            self._by_key.pop(key, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            f"{session}:{turn}": activation.to_dict()
            for (session, turn), activation in sorted(self._by_key.items())
        }

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self):
        return iter(self.active())


_default_log: SkillActivationLog | None = None
_current_log: ContextVar[SkillActivationLog | None] = ContextVar(
    "nexus_skill_activation_log", default=None
)


def get_default_log() -> SkillActivationLog:
    global _default_log
    if _default_log is None:
        _default_log = SkillActivationLog()
    return _default_log


def set_default_log(log: SkillActivationLog | None) -> None:
    global _default_log
    _default_log = log


def clear_default_log() -> None:
    get_default_log().clear()


def bind_log(log: SkillActivationLog) -> Token[SkillActivationLog | None]:
    if not isinstance(log, SkillActivationLog):
        raise TypeError("log must be a SkillActivationLog")
    return _current_log.set(log)


def reset_log(token: Token[SkillActivationLog | None]) -> None:
    _current_log.reset(token)


@contextmanager
def use_log(log: SkillActivationLog):
    token = bind_log(log)
    try:
        yield log
    finally:
        reset_log(token)


def activation_sink_for(ctx: object) -> object:
    """Resolve the activation sink for a context (explicit, bound, or default).

    Prefers ``ctx.activations`` then its ``ctx.activation_sink`` alias; falls
    back to the :mod:`contextvars` binding and finally the process-wide default
    log. The returned object only has to expose ``record(activation)``.
    """
    for name in ("activations", "activation_sink"):
        explicit = getattr(ctx, name, None)
        if explicit is not None and callable(getattr(explicit, "record", None)):
            return explicit
    bound = _current_log.get()
    if bound is not None:
        return bound
    return get_default_log()


def activation_log_for(ctx: object) -> SkillActivationLog:
    """Like :func:`activation_sink_for` but guaranteed to return a real log."""
    sink = activation_sink_for(ctx)
    return sink if isinstance(sink, SkillActivationLog) else get_default_log()


# ---------------------------------------------------------------------------
# Service resolution
# ---------------------------------------------------------------------------


def _skill_service(ctx: ToolContext) -> SkillServiceView | None:
    for name in ("skills", "skill_service"):
        service = getattr(ctx, name, None)
        if service is not None:
            return service
    return None


def _extension_service(ctx: ToolContext) -> ExtensionServiceView | None:
    for name in ("extensions", "extension_service"):
        service = getattr(ctx, name, None)
        if service is not None:
            return service
    return None


def _profile_name(config: object) -> str:
    v2 = getattr(config, "v2", None)
    agent = getattr(v2, "agent", None)
    name = getattr(agent, "profile", None)
    return name if isinstance(name, str) and name else DEFAULT_PROFILE


def _profile_tools(config: object) -> frozenset[str]:
    try:
        return profile_tools(_profile_name(config))
    except Exception:  # noqa: BLE001 - an unknown profile fails closed to empty
        return frozenset()


def _profile_bundles(config: object) -> frozenset[str]:
    """The bundle names the active profile enables (fail closed to empty)."""
    try:
        return frozenset(get_profile(_profile_name(config)).bundles)
    except Exception:  # noqa: BLE001 - an unknown profile enables no bundle
        return frozenset()


def _bundled_tool_entries(ctx: ToolContext, skill: Any) -> tuple[tuple[str, str | None], ...]:
    """The active skill's bundled tool ``(name, bundle)`` pairs from the manifest.

    Reads the pinned manifest's ``skill_tools`` association; a skill with no
    bundled tools (or a manager that does not carry the association) yields
    nothing. The tools are never added to the global catalog -- they join the
    activation's ``available`` set for this invocation only.
    """
    extensions = _extension_service(ctx)
    manifest = getattr(extensions, "manifest", None) if extensions is not None else None
    skill_tools = getattr(manifest, "skill_tools", None)
    if not isinstance(skill_tools, Mapping):
        return ()
    entry = skill_tools.get(getattr(skill, "name", None))
    tools = getattr(entry, "tools", ()) if entry is not None else ()
    out: list[tuple[str, str | None]] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            continue
        spec = getattr(tool, "spec", None)
        bundle = getattr(spec, "bundle", None)
        out.append((name, bundle if isinstance(bundle, str) else None))
    return tuple(out)


def _available_tools(ctx: ToolContext) -> frozenset[str]:
    extensions = _extension_service(ctx)
    if extensions is not None:
        manifest = getattr(extensions, "manifest", None)
        tools = getattr(manifest, "tools", None)
        if isinstance(tools, Mapping):
            return frozenset(str(name) for name in tools)
    service = _skill_service(ctx)
    known = getattr(service, "known_tools", None) if service is not None else None
    if isinstance(known, Iterable) and not isinstance(known, (str, bytes)):
        return frozenset(str(name) for name in known)
    return frozenset()


def _generation(ctx: ToolContext) -> int:
    extensions = _extension_service(ctx)
    if extensions is not None:
        generation = getattr(extensions, "generation", None)
        if isinstance(generation, int) and not isinstance(generation, bool):
            return generation
    service = _skill_service(ctx)
    generation = getattr(service, "generation", None) if service is not None else None
    if isinstance(generation, int) and not isinstance(generation, bool):
        return generation
    return 0


def _turn_number(turn_id: object) -> int:
    if isinstance(turn_id, bool):
        return 0
    if isinstance(turn_id, int):
        return max(0, turn_id)
    if isinstance(turn_id, str):
        digits = "".join(ch for ch in turn_id if ch.isdigit())
        if digits:
            with contextlib.suppress(ValueError):
                return max(0, int(digits))
    return 0


def build_activation(ctx: ToolContext, skill: Any) -> SkillActivation:
    """Intersect the live authority with a skill's declaration.

    The result is always a subset of ``available & profile``. An absent
    extension service yields an empty authority, so a declaration can never be
    read as a grant. The skill's own bundled tools are added to the *available*
    set for this invocation (and to the profile only when their bundle is one
    the active profile enables), so a bundled tool is exposed only while its
    skill is active and only under a profile that would allow it.
    """
    bundled = _bundled_tool_entries(ctx, skill)
    available = set(_available_tools(ctx))
    available.update(name for name, _bundle in bundled)
    profile = set(_profile_tools(ctx.config))
    profile_bundles = _profile_bundles(ctx.config)
    for name, bundle in bundled:
        if bundle is not None and bundle in profile_bundles:
            profile.add(name)
    skill_bundles = set(getattr(skill, "bundles", ()) or ())
    extra_declared = [name for name, bundle in bundled if bundle in skill_bundles]
    return SkillActivation.for_skill(
        skill,
        available=available,
        profile=profile,
        bundle_tools={name: bundle.tools for name, bundle in BUNDLES.items()},
        extra_declared=extra_declared,
        generation=_generation(ctx),
        session=ctx.session_id or None,
        turn=_turn_number(ctx.turn_id),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _truncate_utf8(data: bytes, limit: int) -> bytes:
    if limit <= 0:
        return b""
    if len(data) <= limit:
        return data
    return data[:limit].decode("utf-8", "ignore").encode("utf-8")


def _byte_budget(ctx: ToolContext, default: int) -> int:
    v2 = getattr(ctx.config, "v2", None)
    tools = getattr(v2, "tools", None)
    tokens = getattr(tools, "max_result_tokens", None)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        tokens = _FALLBACK_MAX_RESULT_TOKENS
    return max(1, min(default, tokens * _CHARS_PER_TOKEN))


@dataclass(frozen=True)
class _ResourceInvocation:
    resource: str
    sha256: str
    size: int
    truncated: bool
    text: str


def render_resource_invocation(
    skill: Any,
    relative: str,
    body: str,
    *,
    generation: int = 0,
    max_output_bytes: int = DEFAULT_MAX_RESOURCE_OUTPUT_BYTES,
) -> _ResourceInvocation:
    """Wrap a bundled resource in a bounded, delimited provenance document."""
    encoded = body.encode("utf-8")
    size = len(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    source = getattr(getattr(skill, "source", None), "value", None) or "unknown"
    header = (
        f'<skill-resource skill="{skill.name}" source="{source}" '
        f'generation="{generation}" resource="{relative}" '
        f'sha256="{digest}" bytes="{size}">'
    )
    prefix = f"{RESOURCE_BEGIN_DELIMITER}\n{header}\n"
    suffix = f"\n{RESOURCE_END_DELIMITER}"
    overhead = len(prefix.encode("utf-8")) + len(suffix.encode("utf-8"))
    budget = max(max_output_bytes - overhead, 0)
    rendered = _truncate_utf8(encoded, budget)
    truncated = rendered != encoded
    text = prefix + rendered.decode("utf-8") + suffix
    if len(text.encode("utf-8")) > max_output_bytes:
        overflow = len(text.encode("utf-8")) - max_output_bytes
        rendered = _truncate_utf8(rendered, max(0, len(rendered) - overflow))
        truncated = True
        text = prefix + rendered.decode("utf-8") + suffix
    return _ResourceInvocation(
        resource=relative,
        sha256=digest,
        size=size,
        truncated=truncated,
        text=text,
    )


# ---------------------------------------------------------------------------
# Event seam
# ---------------------------------------------------------------------------


async def _emit(ctx: ToolContext, event_type: str, data: dict[str, Any]) -> None:
    emit = getattr(ctx, "emit", None)
    if emit is None:
        return
    try:
        outcome = emit(event_type, dict(data))
    except Exception:  # noqa: BLE001 - a broken sink never breaks a call
        return
    if inspect.isawaitable(outcome):
        with contextlib.suppress(Exception):
            await outcome


async def _record(ctx: ToolContext, activation: SkillActivation) -> None:
    sink = activation_sink_for(ctx)
    record = getattr(sink, "record", None)
    if not callable(record):
        return
    outcome = record(activation)
    if inspect.isawaitable(outcome):
        await outcome


def _check_cancel(ctx: ToolContext) -> None:
    token = ctx.cancel_token
    if token is not None:
        token.raise_if_cancelled()


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

_SKILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Name of the skill to invoke, as listed in the index.",
        },
        "resource": {
            "type": "string",
            "description": (
                "Optional bundled resource to load instead of the body, e.g. "
                "'scripts/run.py' or 'references/notes.md'."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

SKILL_SPEC = ToolSpec(
    name="Skill",
    description=(
        "Load a skill's instructions (or one of its bundled resources) by name. "
        "Use this when a task matches a skill listed in the skills index; the "
        "skill's tools are scoped to this turn only."
    ),
    input_schema=_SKILL_SCHEMA,
    bundle="ext",
    mutates=False,
    concurrency="parallel",
    max_result_tokens=25_000,
)


def _require_name(args: Mapping[str, Any]) -> str:
    value = args.get("name")
    if not isinstance(value, str) or not value.strip():
        raise _SkillToolError("'name' must be a non-empty skill name")
    return value.strip()


def _opt_resource(args: Mapping[str, Any]) -> str | None:
    value = args.get("resource")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _SkillToolError("'resource' must be a non-empty string when given")
    return value.strip()


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return ToolExecutionResult.text(
            "Skill: arguments must be an object", is_error=True
        )
    try:
        name = _require_name(args)
        resource = _opt_resource(args)
    except ToolError as exc:
        return ToolExecutionResult.text(f"Skill: {exc}", is_error=True)

    service = _skill_service(ctx)
    if service is None:
        return ToolExecutionResult.text(
            "Skill: no skill service is available for this call; the harness "
            "was not given a skill manager (this is a configuration error, not "
            "a missing skill)",
            is_error=True,
        )

    await _emit(ctx, "skill.invoked", {"skill": name, "resource": resource})
    try:
        _check_cancel(ctx)
        skill = service.get(name)
        if skill is None:
            raise _SkillToolError(
                f"unknown skill {name!r}; call ListExtensions to see available "
                "skills, or check the skills index"
            )
        generation = _generation(ctx)

        if resource is None:
            invocation = render_invocation(
                skill,
                generation=generation,
                max_output_bytes=_byte_budget(
                    ctx, DEFAULT_MAX_OUTPUT_BYTES
                ),
            )
            body_text = invocation.text
            metrics: dict[str, Any] = {
                "skill": invocation.name,
                "source": invocation.source,
                "generation": invocation.generation,
                "fingerprint": invocation.fingerprint,
                "body_sha256": invocation.body_sha256,
                "body_bytes": invocation.body_bytes,
                "truncated": invocation.truncated,
            }
            note = (
                f"[Skill {invocation.name}: body {invocation.body_bytes}B "
                f"(sha256 {invocation.body_sha256[:12]}); skill tools are scoped "
                "to this turn]"
            )
        else:
            body = service.read_resource(skill, resource)
            rendered = render_resource_invocation(
                skill,
                resource,
                body,
                generation=generation,
                max_output_bytes=_byte_budget(
                    ctx, DEFAULT_MAX_RESOURCE_OUTPUT_BYTES
                ),
            )
            body_text = rendered.text
            metrics = {
                "skill": getattr(skill, "name", name),
                "source": getattr(
                    getattr(skill, "source", None), "value", None
                ),
                "generation": generation,
                "resource": rendered.resource,
                "resource_sha256": rendered.sha256,
                "resource_bytes": rendered.size,
                "truncated": rendered.truncated,
            }
            note = (
                f"[Skill {getattr(skill, 'name', name)}: resource "
                f"{rendered.resource} {rendered.size}B (sha256 "
                f"{rendered.sha256[:12]})]"
            )

        activation = build_activation(ctx, skill)
        await _record(ctx, activation)
        metrics["activation"] = activation.to_dict()
        metrics["scoped_tools"] = sorted(activation.active)
    except _SkillToolError as exc:
        await _emit(
            ctx,
            "skill.completed",
            {"skill": name, "resource": resource, "ok": False, "error": str(exc)},
        )
        return ToolExecutionResult.text(f"Skill: {exc}", is_error=True)
    except ToolError as exc:
        await _emit(
            ctx,
            "skill.completed",
            {"skill": name, "resource": resource, "ok": False, "error": str(exc)},
        )
        return ToolExecutionResult.text(f"Skill: {exc}", is_error=True)
    except Exception as exc:  # noqa: BLE001 - tool failures are model-visible
        message = f"{type(exc).__name__}: {exc}"
        await _emit(
            ctx,
            "skill.completed",
            {"skill": name, "resource": resource, "ok": False, "error": message},
        )
        return ToolExecutionResult.text(
            f"Skill: could not load {name!r}: {message}", is_error=True
        )

    await _emit(
        ctx,
        "skill.completed",
        {
            "skill": metrics.get("skill", name),
            "resource": resource,
            "ok": True,
            "scoped_tools": sorted(activation.active),
            "narrowed": activation.narrowed,
            "unknown_bundles": list(activation.unknown_bundles),
        },
    )
    return ToolExecutionResult.text(
        body_text,
        display=f"Skill {metrics.get('skill', name)}",
        context_note=note,
        metrics=metrics,
    )
