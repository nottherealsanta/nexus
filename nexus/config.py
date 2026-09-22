"""One configuration file; fresh immutable settings for every turn."""
from dataclasses import dataclass, fields
from pathlib import Path
import math
import tomllib


@dataclass(frozen=True)
class Config:
    executable: str = "codex"
    model: str | None = None
    sandbox: str = "workspace-write"
    timeout_seconds: float = 900
    context_chars: int = 64_000
    instructions_file: str = "SOUL.md"
    memory_file: str = "MEMORY.md"

    def __post_init__(self):
        for key in ("executable", "sandbox", "instructions_file", "memory_file"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f"{key} must be a nonempty string")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("model must be a nonempty string")
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("sandbox must be read-only or workspace-write")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if type(self.context_chars) is not int or self.context_chars < 1024:
            raise ValueError("context_chars must be an integer >= 1024")

    @classmethod
    def load(cls, workspace: Path) -> "Config":
        path = workspace / "nexus.toml"
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        unknown = data.keys() - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown nexus.toml settings: {', '.join(sorted(unknown))}")
        return cls(**data)

    def read(self, workspace: Path, filename: str) -> str:
        path = (workspace / filename).resolve()
        if not path.is_relative_to(workspace.resolve()):
            raise ValueError(f"Context file must be inside workspace: {filename}")
        if not path.exists():
            return ""
        # Reject oversized files before allocating unbounded input.
        if path.stat().st_size > self.context_chars * 4:
            raise ValueError(f"Context file too large: {filename}")
        return path.read_text(encoding="utf-8")
