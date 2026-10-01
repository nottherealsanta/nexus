"""Element registry: every UI element with lettered design variants.

An element (``recording``, ``context-header``, ``tool-call`` …) owns a list of
variants ``a``, ``b``, ``c`` … Each variant is a function ``Ctx -> renderable``.
``samples`` lists the (caption, item, phase) cases the gallery draws each
variant with, so every variant is judged on the same inputs.

Designs and ``design mix`` choose one variant per element ("picks").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable

from rich.console import RenderableType

from ..kit import Ctx

Render = Callable[[Ctx], RenderableType]
Sample = tuple[str, Any, str]


@dataclass(frozen=True)
class Variant:
    key: str
    name: str
    note: str
    render: Render


@dataclass
class Element:
    slug: str
    title: str
    description: str
    samples: Callable[[Ctx], list[Sample]] = lambda c: [("", None, "")]
    variants: list[Variant] = field(default_factory=list)

    def variant(self, name: str, note: str = "") -> Callable[[Render], Render]:
        def register(fn: Render) -> Render:
            self.variants.append(Variant(chr(ord("a") + len(self.variants)), name, note, fn))
            return fn
        return register

    def get(self, key: str) -> Variant:
        for v in self.variants:
            if v.key == key:
                return v
        return self.variants[0]


ELEMENTS: dict[str, Element] = {}


def element(slug: str, title: str, description: str, samples: Callable[[Ctx], list[Sample]] | None = None) -> Element:
    el = Element(slug, title, description)
    if samples is not None:
        el.samples = samples
    ELEMENTS[slug] = el
    return el


def render(c: Ctx, slug: str, item: Any = None, phase: str = "") -> RenderableType:
    """Draw ``slug`` with the variant picked in ``c.picks`` (default ``a``)."""
    el = ELEMENTS[slug]
    return el.get(c.picks.get(slug, "a")).render(c.with_(item=item, phase=phase))


# Import order is gallery order: chrome first, then the conversation top to bottom.
_MODULES = (
    "chrome", "context", "conversation", "tools_calls", "agents", "prompts", "composer", "recording", "overlays",
)
for _m in _MODULES:
    import_module(f"{__name__}.{_m}")
