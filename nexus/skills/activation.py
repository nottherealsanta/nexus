"""Session/turn-local :class:`SkillActivation` overlay (plan sections 5.3-5.4).

A skill's declaration is *not* a grant. ``SkillActivation`` is the immutable
overlay a session or turn computes when a skill becomes active: it narrows the
tools already granted by the turn to the subset the skill declares, and it can
never widen anything. The rule is a single intersection::

    authority = available_tools  &  profile_tools
    active    = authority        &  declared_tools      (when a declaration exists)

``available_tools`` is the set the live manifest currently exposes, and
``profile_tools`` is the set the active profile enables; both are inputs, so an
activation is always a subset of the authority it was handed. Declaring a tool
the turn does not have yields no tool (it is retained in :attr:`unavailable` for
diagnostics), never a new one. A declaration whose bundle names are all unknown
fails closed to the empty set. Declaring nothing at all means "do not narrow" and
leaves the authority untouched.

The overlay is frozen and copies every input into a :class:`frozenset`, so two
activations never share mutable state and a caller mutating the collections it
passed in cannot change a built activation.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import SkillActivationError

__all__ = [
    "SkillActivation",
]


def _as_frozenset(value: object, label: str) -> frozenset[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise SkillActivationError(f"{label} must be an iterable of tool names")
    names = []
    for item in value:
        if not isinstance(item, str):
            raise SkillActivationError(f"{label} must contain only strings")
        names.append(item)
    return frozenset(names)


def _as_name_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise SkillActivationError(f"{label} must be an iterable of names")
    names = []
    for item in value:
        if not isinstance(item, str):
            raise SkillActivationError(f"{label} must contain only strings")
        names.append(item)
    return tuple(names)


@dataclass(frozen=True)
class SkillActivation:
    """An immutable, turn-local narrowing of the tools a skill may use.

    Build it with :meth:`intersect`; the direct constructor is validated so even
    a hand-built overlay cannot claim a tool outside ``available & profile``.
    """

    skill: str
    active: frozenset[str] = field(default_factory=frozenset)
    available: frozenset[str] = field(default_factory=frozenset)
    profile: frozenset[str] = field(default_factory=frozenset)
    declared: frozenset[str] = field(default_factory=frozenset)
    allowed_tools: tuple[str, ...] = ()
    bundles: tuple[str, ...] = ()
    unavailable: frozenset[str] = field(default_factory=frozenset)
    unknown_bundles: tuple[str, ...] = ()
    generation: int = 0
    session: str | None = None
    turn: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.skill, str) or not self.skill:
            raise SkillActivationError("activation skill name must be a non-empty string")
        for label in ("active", "available", "profile", "declared", "unavailable"):
            value = getattr(self, label)
            if not isinstance(value, frozenset):
                object.__setattr__(
                    self, label, _as_frozenset(value, f"SkillActivation.{label}")
                )
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise SkillActivationError("activation generation must be an int")
        if self.generation < 0:
            raise SkillActivationError("activation generation must be non-negative")
        if isinstance(self.turn, bool) or not isinstance(self.turn, int):
            raise SkillActivationError("activation turn must be an int")
        if self.turn < 0:
            raise SkillActivationError("activation turn must be non-negative")
        authority = self.available & self.profile
        if not self.active <= authority:
            raise SkillActivationError(
                f"activation for {self.skill!r} would expand authority: "
                f"{sorted(self.active - authority)} is not in available & profile"
            )
        if not self.unavailable <= self.declared:
            raise SkillActivationError(
                "activation unavailable set must be a subset of declared tools"
            )
        if self.unavailable & authority:
            raise SkillActivationError(
                "activation unavailable set must not overlap the authority"
            )

    # -- the authority this overlay is bounded by -------------------------

    @property
    def authority(self) -> frozenset[str]:
        """The most this activation could ever expose: ``available & profile``."""
        return self.available & self.profile

    @property
    def tools(self) -> frozenset[str]:
        """Alias for :attr:`active`."""
        return self.active

    @property
    def narrowed(self) -> bool:
        """Whether the declaration actually removed any authority."""
        return self.active != self.authority

    def permits(self, tool: object) -> bool:
        """Whether ``tool`` is active in this overlay. Never widens authority."""
        return isinstance(tool, str) and tool in self.active

    def __contains__(self, tool: object) -> bool:
        return self.permits(tool)

    def __len__(self) -> int:
        return len(self.active)

    # -- construction ------------------------------------------------------

    @classmethod
    def intersect(
        cls,
        *,
        skill: str,
        available: Iterable[str],
        profile: Iterable[str],
        allowed_tools: Iterable[str] = (),
        bundles: Iterable[str] = (),
        bundle_tools: Mapping[str, Iterable[str]] | None = None,
        extra_declared: Iterable[str] = (),
        generation: int = 0,
        session: str | None = None,
        turn: int = 0,
    ) -> SkillActivation:
        """Compute the active tool set by intersecting every authority source.

        ``bundle_tools`` maps a declared bundle name to the tool names it would
        contribute. An unknown bundle contributes nothing and is recorded in
        :attr:`unknown_bundles` (fail closed). ``extra_declared`` are additional
        names the skill declares by other means (for example its own bundled
        ``tools/*.py`` whose bundle it listed); they join ``allowed_tools`` and
        the expanded bundles in the declared set. The result is always a subset
        of ``available & profile``.
        """
        available_fs = _as_frozenset(available, "available")
        profile_fs = _as_frozenset(profile, "profile")
        allowed = _as_name_tuple(allowed_tools, "allowed_tools")
        bundle_names = _as_name_tuple(bundles, "bundles")
        extra = _as_name_tuple(extra_declared, "extra_declared")
        if bundle_tools is not None and not isinstance(bundle_tools, Mapping):
            raise SkillActivationError("bundle_tools must be a mapping or None")
        catalog: Mapping[str, Iterable[str]] = bundle_tools or {}

        expanded: set[str] = set()
        unknown: list[str] = []
        for bundle in bundle_names:
            names = catalog.get(bundle)
            if names is None:
                unknown.append(bundle)
                continue
            expanded.update(str(name) for name in names)

        declared = set(allowed) | expanded | set(extra)
        authority = available_fs & profile_fs
        # A declaration that names anything (even only an unknown bundle) narrows
        # the authority; failing closed to the empty set is the safe reading.
        declaration_present = bool(allowed) or bool(bundle_names) or bool(extra)
        active = authority & declared if declaration_present else authority
        unavailable = declared - authority

        return cls(
            skill=skill,
            active=frozenset(active),
            available=available_fs,
            profile=profile_fs,
            declared=frozenset(declared),
            allowed_tools=allowed,
            bundles=bundle_names,
            unavailable=frozenset(unavailable),
            unknown_bundles=tuple(unknown),
            generation=generation,
            session=session,
            turn=turn,
        )

    @classmethod
    def for_skill(
        cls,
        skill: Any,
        *,
        available: Iterable[str],
        profile: Iterable[str],
        bundle_tools: Mapping[str, Iterable[str]] | None = None,
        extra_declared: Iterable[str] = (),
        generation: int = 0,
        session: str | None = None,
        turn: int = 0,
    ) -> SkillActivation:
        """Convenience wrapper around :meth:`intersect` for a ``Skill``-like object."""
        return cls.intersect(
            skill=skill.name,
            available=available,
            profile=profile,
            allowed_tools=skill.allowed_tools,
            bundles=skill.bundles,
            bundle_tools=bundle_tools,
            extra_declared=extra_declared,
            generation=generation,
            session=session,
            turn=turn,
        )

    def to_dict(self) -> dict[str, Any]:
        """A deterministic, JSON-serializable view (sorted tool names)."""
        return {
            "skill": self.skill,
            "active": sorted(self.active),
            "authority": sorted(self.authority),
            "declared": sorted(self.declared),
            "unavailable": sorted(self.unavailable),
            "unknown_bundles": list(self.unknown_bundles),
            "narrowed": self.narrowed,
            "generation": self.generation,
            "session": self.session,
            "turn": self.turn,
        }
