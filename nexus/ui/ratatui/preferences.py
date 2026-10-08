"""Native shell preferences persisted across terminal sessions."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


class Preferences:
    DEFAULTS = {"theme": "nexus-dark", "sessions_sidebar": True, "details_sidebar": True,
                "details_tab": "Session", "context_preview": True, "centered_layout": False, "model_favorites": [], "model_recent": []}

    def __init__(self, path=None):
        self._worker = None
        self._revision = 0
        self._saved = 0
        self.path = path or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "nexus/tui.json"
        self.values = {key: list(value) if isinstance(value, list) else value for key, value in self.DEFAULTS.items()}
        try:
            if self.path.stat().st_size <= 128 * 1024:
                value = json.loads(self.path.read_text())
                if isinstance(value, dict):
                    for key, default in self.DEFAULTS.items():
                        if isinstance(value.get(key), type(default)):
                            self.values[key] = value[key]
        except (OSError, ValueError):
            pass
        if self.values["details_tab"] not in {"Session", "Files", "MCP", "Logs"}:
            self.values["details_tab"] = "Session"

    def set(self, key, value):
        if key not in self.DEFAULTS or not isinstance(value, type(self.DEFAULTS[key])):
            return
        if key == "details_tab" and value not in {"Session", "Files", "MCP", "Logs"}:
            return
        if isinstance(value, list):
            value = list(dict.fromkeys(item for item in value if isinstance(item, str) and len(item) <= 256))[:100]
        self.values[key] = value
        self._revision += 1
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._write(dict(self.values))
            self._saved = self._revision
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._persist())

    def _write(self, values):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(values, indent=2))
            temporary.replace(self.path)
        except OSError:
            pass

    async def _persist(self):
        while self._saved != self._revision:
            await asyncio.sleep(.25)
            revision, values = self._revision, dict(self.values)
            await asyncio.to_thread(self._write, values)
            self._saved = revision

    async def flush(self):
        if self._worker:
            await self._worker
