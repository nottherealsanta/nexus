"""Native shell preferences persisted across terminal sessions."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ...ui_support import details as details_view
from ...ui_support.sidebar import TABS, clamp_width, normalize_tab


class Preferences:
    #: ``sessions_sidebar`` is the GPUI desktop's sessions sidebar (default open, shared file); the
    #: terminal's left sidebar (Projects, Sessions, Files, Git) is ``sidebar_open``, closed until opened.
    DEFAULTS = {"theme": "nexus-dark", "sessions_sidebar": True, "details_sidebar": True,
                "details_tab": "Session", "context_preview": True, "centered_layout": False, "model_favorites": [], "model_recent": [],
                "sidebar_open": False, "sidebar_width": 34, "sidebar_tab": "Sessions",
                "details_width": details_view.DEFAULT_WIDTH}

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
        # The right sidebar's Changes tab is stored as ``Files``; its width is bounded like the left one.
        self.values["details_tab"] = self.values["details_tab"] if self.values["details_tab"] in {"Browser", "Git"} else details_view.normalize_tab(self.values["details_tab"])
        self.values["details_width"] = details_view.clamp_width(self.values["details_width"])
        # A stored width outside the bounds is clamped; an unknown tab falls back to Sessions.
        self.values["sidebar_width"] = clamp_width(self.values["sidebar_width"])
        self.values["sidebar_tab"] = normalize_tab(self.values["sidebar_tab"])

    def set(self, key, value):
        if key not in self.DEFAULTS or not isinstance(value, type(self.DEFAULTS[key])):
            return
        if key == "details_tab":
            if value not in {*details_view.TABS, "Changes", "Browser", "Git"}:
                return
            value = value if value in {"Browser", "Git"} else details_view.normalize_tab(value)
        if key == "details_width":
            if isinstance(value, bool):
                return
            value = details_view.clamp_width(value)
        if key == "sidebar_tab" and value not in TABS:
            return
        if key == "sidebar_width":
            if isinstance(value, bool):
                return
            value = clamp_width(value)
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
