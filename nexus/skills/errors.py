"""Skill-layer exception taxonomy.

Kept local to :mod:`nexus.skills` rather than added to ``nexus/errors.py`` so
this packet owns exactly its own surface. The base class is the shared
:class:`nexus.errors.NexusError`, so callers can still catch every
Nexus-originated failure in one place.
"""
from __future__ import annotations

from ..errors import NexusError

__all__ = [
    "SkillActivationError",
    "SkillError",
    "SkillNotFoundError",
    "SkillOversizeError",
    "SkillParseError",
    "SkillResourceError",
    "SkillSecurityError",
    "SkillStaleError",
]


class SkillError(NexusError):
    """Base class for skill discovery, parsing, and resource failures."""


class SkillParseError(SkillError, ValueError):
    """A ``SKILL.md`` declaration is malformed or outside the restricted grammar."""


class SkillOversizeError(SkillParseError):
    """A skill file, frontmatter block, body, or resource exceeds its byte budget."""


class SkillResourceError(SkillError, ValueError):
    """A bundled resource cannot be resolved or read."""


class SkillSecurityError(SkillResourceError):
    """A resource path is absolute, traverses, or escapes the skill root."""


class SkillStaleError(SkillError):
    """A snapshotted skill's on-disk artifact no longer matches its fingerprint."""


class SkillActivationError(SkillError, ValueError):
    """A skill activation overlay is malformed or would expand authority."""


class SkillNotFoundError(SkillError):
    """No discovered skill matches the requested name."""

    def __init__(self, name: object) -> None:
        self.name = name
        super().__init__(f"unknown skill {name!r}")
