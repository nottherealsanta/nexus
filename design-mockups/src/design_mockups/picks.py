"""Your picks: one variant per element plus a layout and palette, saved to
``design-mockups/picks.json`` (git-ignored) and used by ``design mix``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


def _root() -> Path:
    here = Path(__file__).resolve().parents[2]
    return here if (here / "pyproject.toml").exists() else Path.cwd()


PATH = _root() / "picks.json"


@dataclass
class Picks:
    elements: dict[str, str] = field(default_factory=dict)
    layout: str = "classic"
    palette: str = "nexus"
    light: bool = False

    @classmethod
    def load(cls) -> "Picks":
        try:
            data = json.loads(PATH.read_text())
        except (OSError, ValueError):
            return cls()
        return cls(dict(data.get("elements", {})), data.get("layout", "classic"), data.get("palette", "nexus"), bool(data.get("light", False)))

    def save(self) -> None:
        PATH.write_text(json.dumps({"layout": self.layout, "palette": self.palette, "light": self.light,
                                    "elements": dict(sorted(self.elements.items()))}, indent=2) + "\n")

    @staticmethod
    def reset() -> None:
        PATH.unlink(missing_ok=True)
