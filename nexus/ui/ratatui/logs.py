"""Bounded paged host diagnostics for the native drawer (PLAN §14.7).

Keep daemon/session cursors separate. Problems remain visible while routine
entries fold behind an explicit toggle. Session changes discard old session rows.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime

from ...ui_support.text import escape_controls


class Logs:
    LIMIT = 60

    def __init__(self, shell):
        self.shell = shell
        self.open = False
        self.visible = False
        self.trace = []
        self.python_trace = []
        self.show_all = False
        self.rows = {"daemon": [], "session": []}
        self.cursors = {"daemon": None, "session": None}
        self.truncated = set()
        self.error = ""
        self.notices = deque(maxlen=50)  # toasts of this window: (time, level, text)

    def add_notice(self, level, text):
        """Every toast is also a log line, so a dismissed toast is never lost."""
        self.notices.append((time.time(), level, str(text)))

    def reset_session(self):
        self.rows["session"] = []
        self.cursors["session"] = None
        self.truncated.discard("session")

    async def poll(self):
        session = self.shell.controller.session
        generation = self.shell.generation
        try:
            for _ in range(4):
                result = await self.shell.client.read_logs(session,
                    daemon_cursor=self.cursors["daemon"], session_cursor=self.cursors["session"], limit=50)
                if generation != self.shell.generation:
                    return
                for source in ("daemon", "session"):
                    page = getattr(result, source)
                    if page.truncated:
                        self.rows[source] = []
                        self.truncated.add(source)
                    known = {(row.seq, row.ts, row.kind) for row in self.rows[source]}
                    self.rows[source].extend(row for row in page.entries if (row.seq, row.ts, row.kind) not in known)
                    if len(self.rows[source]) > self.LIMIT:
                        self.truncated.add(source)
                    self.rows[source] = self.rows[source][-self.LIMIT:]
                    self.cursors[source] = page.next_cursor
                if not result.daemon.has_more and not result.session.has_more:
                    break
            self.error = ""
        except Exception as exc:
            self.error = str(exc)

    def lines(self, *, show_all=None):
        show_all = self.show_all if show_all is None else show_all
        rows = sorted([*self.rows["daemon"], *self.rows["session"]], key=lambda row: row.ts)
        problems = [row for row in rows if row.level != "info"]
        routine = [row for row in rows if row.level == "info"]
        selected = sorted(problems + (routine if show_all else []), key=lambda row: row.ts)
        entries = [(row.ts, row.level, f"{row.source} · {row.kind} · {row.summary}") for row in selected]
        # Toasts: warnings and errors always; info and success with the routine entries.
        notices = [(ts, level, f"client · toast · {text}") for ts, level, text in self.notices]
        shown = [n for n in notices if show_all or n[1] in ("warning", "error")]
        routine_count = len(routine) + len(notices) - len(shown)
        lines = []
        if self.truncated:
            lines.append("[Earlier log entries clipped · " + ", ".join(sorted(self.truncated)) + "]")
        for ts, level, text in sorted(entries + shown, key=lambda entry: entry[0]):
            try:
                stamp = datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OverflowError, OSError):
                stamp = "Unknown time"
            lines.append(f"{stamp} [{level.upper()}] {text}")
        lines.append(f"{routine_count} routine entries {'shown' if show_all else 'folded'} · Ctrl+A toggle")
        if self.error:
            lines.append("Log read failed: " + self.error)
        return [escape_controls(line) for line in lines]
