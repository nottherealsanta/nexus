"""The public UI boundary: JSON-serializable events, no rendering or input."""
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
