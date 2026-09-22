"""Delimited, bounded skill invocation output (plan section 5.4).

When the model calls ``Skill(name=...)`` the body is returned as a tool result.
That result is text the model acts on, so it is wrapped in an explicit delimiter
pair and prefixed with provenance the harness can rely on:

* the skill's name and source tier;
* the manifest generation it was activated under;
* the whole-skill fingerprint and the body hash.

The renderer is **bounded**: the body is truncated to a byte budget and the whole
document is guaranteed not to exceed ``max_output_bytes``, so a hostile or merely
huge ``SKILL.md`` cannot blow the context window. It is a pure function of the
snapshot, so an invocation under a pinned generation is deterministic.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .model import DEFAULT_MAX_BODY_BYTES

__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "SKILL_BEGIN_DELIMITER",
    "SKILL_END_DELIMITER",
    "SkillInvocation",
    "render_invocation",
]

#: Delimiter lines. Distinctive so a reader can find the exact skill content.
SKILL_BEGIN_DELIMITER = "-----BEGIN SKILL-----"
SKILL_END_DELIMITER = "-----END SKILL-----"
#: The whole rendered document is capped, delimiters and provenance included.
DEFAULT_MAX_OUTPUT_BYTES = 262_144


@dataclass(frozen=True)
class SkillInvocation:
    """The bounded, delimited invocation text plus its provenance and hashes."""

    name: str
    source: str
    generation: int
    fingerprint: str
    body_sha256: str
    body_bytes: int
    truncated: bool
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "generation": self.generation,
            "fingerprint": self.fingerprint,
            "body_sha256": self.body_sha256,
            "body_bytes": self.body_bytes,
            "truncated": self.truncated,
        }


def _truncate_utf8(data: bytes, limit: int) -> bytes:
    if limit <= 0:
        return b""
    if len(data) <= limit:
        return data
    return data[:limit].decode("utf-8", "ignore").encode("utf-8")


def render_invocation(
    skill: Any,
    *,
    body: str | None = None,
    generation: int = 0,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> SkillInvocation:
    """Render a skill body as a delimited, bounded invocation document.

    ``body`` overrides the skill's own snapshot (useful for tests); otherwise the
    snapshot is loaded via :meth:`Skill.load_body`. The result's ``text`` never
    exceeds ``max_output_bytes``.
    """
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
        raise TypeError("max_output_bytes must be an int")
    if max_output_bytes < 0:
        raise ValueError("max_output_bytes must be non-negative")

    if body is None:
        body_text = skill.load_body()
    else:
        if not isinstance(body, str):
            raise TypeError("body must be a string")
        body_text = body
    encoded = body_text.encode("utf-8")
    body_bytes = len(encoded)
    if body is None and getattr(skill, "body_sha256", ""):
        body_sha = skill.body_sha256
    else:
        body_sha = hashlib.sha256(encoded).hexdigest()

    header = (
        f'<skill name="{skill.name}" source="{skill.source.value}" '
        f'generation="{generation}" fingerprint="{skill.fingerprint()}" '
        f'body_sha256="{body_sha}">'
    )
    prefix = f"{SKILL_BEGIN_DELIMITER}\n{header}\n"
    suffix = f"\n{SKILL_END_DELIMITER}"
    overhead = len(prefix.encode("utf-8")) + len(suffix.encode("utf-8"))

    budget = max(max_output_bytes - overhead, 0)
    body_budget = min(max_body_bytes, budget) if max_body_bytes is not None else budget
    rendered = _truncate_utf8(encoded, body_budget)
    truncated = rendered != encoded
    text = prefix + rendered.decode("utf-8") + suffix
    if len(text.encode("utf-8")) > max_output_bytes:
        # The budget math is exact for ASCII delimiters; this guards a non-ASCII
        # skill name/source and keeps the hard bound regardless.
        overflow = len(text.encode("utf-8")) - max_output_bytes
        rendered = _truncate_utf8(rendered, max(0, len(rendered) - overflow))
        truncated = True
        text = prefix + rendered.decode("utf-8") + suffix

    return SkillInvocation(
        name=skill.name,
        source=skill.source.value,
        generation=generation,
        fingerprint=skill.fingerprint(),
        body_sha256=body_sha,
        body_bytes=body_bytes,
        truncated=truncated,
        text=text,
    )
