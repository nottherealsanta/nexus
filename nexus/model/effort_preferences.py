"""Bounded machine-wide memory of per-model reasoning-effort choices."""
from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path

from .request import REASONING_EFFORTS

__all__ = ["ModelEffortStore"]


class ModelEffortStore:
    """Persist the last effort (including an explicit default) per exact ref.

    The file is private machine state, not session history. Invalid or damaged
    files are ignored, and writes use a same-directory atomic replacement on a
    best-effort basis.
    """

    VERSION = 1
    MAX_ENTRIES = 256
    MAX_FILE_BYTES = 64 * 1024
    MAX_KEY_LENGTH = 256

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else Path.home() / ".nexus" / "model_efforts.json"
        self._entries = self._load()

    def get(self, reference: str) -> tuple[str | None] | None:
        """Return a one-item effort tuple, or ``None`` when not remembered."""
        if not self._valid_key(reference):
            return None
        if reference not in self._entries:
            return None
        return (self._entries[reference],)

    def put(self, reference: str, effort: str | None) -> None:
        """Remember ``effort`` for this exact provider/model reference."""
        if not self._valid_key(reference):
            raise ValueError("reference must be a nonempty string of at most 256 characters")
        if effort is not None and (not isinstance(effort, str) or effort not in REASONING_EFFORTS):
            raise ValueError(f"invalid reasoning effort: {effort!r}")

        self._entries.pop(reference, None)
        self._entries[reference] = effort
        while len(self._entries) > self.MAX_ENTRIES:
            self._entries.popitem(last=False)
        self._save()

    @classmethod
    def _valid_key(cls, reference: object) -> bool:
        return isinstance(reference, str) and 0 < len(reference) <= cls.MAX_KEY_LENGTH

    def _load(self) -> OrderedDict[str, str | None]:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(self.MAX_FILE_BYTES + 1)
            if len(raw) > self.MAX_FILE_BYTES:
                return OrderedDict()
            payload = json.loads(raw)
            if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != self.VERSION:
                return OrderedDict()
            entries = payload.get("entries")
            if not isinstance(entries, dict):
                return OrderedDict()
            result: OrderedDict[str, str | None] = OrderedDict()
            for key, effort in entries.items():
                if not self._valid_key(key):
                    continue
                if effort is not None and (not isinstance(effort, str) or effort not in REASONING_EFFORTS):
                    continue
                result[key] = effort
            while len(result) > self.MAX_ENTRIES:
                result.popitem(last=False)
            return result
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return OrderedDict()

    def _save(self) -> None:
        payload = {"version": self.VERSION, "entries": self._entries}
        try:
            encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            while len(encoded) > self.MAX_FILE_BYTES and self._entries:
                self._entries.popitem(last=False)
                payload["entries"] = self._entries
                encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except OSError:
            return
