"""Textual-compatible native shell preferences (Ratatui feasibility §7)."""
from __future__ import annotations

import json
import os
from pathlib import Path


class Preferences:
    DEFAULTS = {"theme": "nexus-dark", "sessions_sidebar": True, "details_sidebar": True,
                "details_tab": "Session", "context_preview": True, "model_favorites": [], "model_recent": []}

    def __init__(self, path=None):
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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.values, indent=2))
            temporary.replace(self.path)
        except OSError:
            pass
