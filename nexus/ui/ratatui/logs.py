"""Bounded paged host diagnostics for the native drawer (PLAN §14.7).

Keep daemon/session cursors separate. Problems remain visible while routine
entries fold behind an explicit toggle. Session changes discard old session rows.
"""
from __future__ import annotations

from datetime import datetime

from ...ui_support.text import escape_controls, redact


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
        lines = []
        if self.truncated:
            lines.append("[Earlier log entries clipped · " + ", ".join(sorted(self.truncated)) + "]")
        for row in selected:
            try:
                stamp = datetime.fromtimestamp(row.ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OverflowError, OSError):
                stamp = "Unknown time"
            lines.append(f"{stamp} [{row.level.upper()}] {row.source} · {row.kind} · {row.summary}")
        lines.append(f"{len(routine)} routine entries {'shown' if show_all else 'folded'} · Ctrl+A toggle")
        if self.error:
            lines.append("Log read failed: " + self.error)
        return [redact(escape_controls(line)) for line in lines]
