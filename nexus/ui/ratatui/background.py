"""Bounded independent bridge actions (TUI_LOCAL_INTERACTION_PLAN §1).

Submit/cancel/answer/operations stay in the serial reader. Completions are latest
request wins; attachment and model replies are guarded by session generation.
"""
from __future__ import annotations
import asyncio

from ...ui_support.completion import complete
from ...ui_support.clipboard import read_clipboard_image
from .desktop import copy_text


class BackgroundActions:
    KINDS = {"complete", "refresh_models", "clipboard", "copy_text", "copy_selection"}

    def __init__(self, shell, update):
        self.shell, self.update = shell, update
        self.tasks = set()
        self.completion = None
        self.completion_serial = 0

    def start(self, action):
        if action["type"] == "complete":
            self.completion_serial += 1
            if self.completion and not self.completion.done():
                self.completion.cancel()
        if len(self.tasks) >= 8:
            self.shell.notice = "UI background actions busy; try again"
            return False
        task = asyncio.create_task(self._run(dict(action), self.completion_serial))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        if action["type"] == "complete":
            self.completion = task
        return True

    async def _run(self, action, serial):
        shell = self.shell
        generation, client, session = shell.generation, shell.client, shell.controller.session
        panel_revision = getattr(shell, "panel_revision", 0)
        current = lambda: shell.generation == generation and shell.controller.session == session
        try:
            kind = action["type"]
            if kind == "complete":
                query, prefix = action["text"], action.get("prefix", action["text"])
                rows = await complete(client, prefix, query, efforts=shell.controller.supported_levels)
                if not current() or serial != self.completion_serial:
                    return
                shell.completion_query, shell.completion_prefix, shell.completions = query, prefix, rows
            elif kind == "refresh_models":
                await client.refresh_models()
                if not current() or shell.panel_title != "Select model" or getattr(shell, "panel_revision", 0) != panel_revision:
                    return
                await shell.command("/model", ())
            elif kind == "clipboard":
                if len(shell.attachments) >= 8:
                    raise ValueError("At most eight attachments are allowed")
                image = await asyncio.to_thread(read_clipboard_image)
                if image and current():
                    prepared = await client.prepare_attachment(name="clipboard.png", data=image)
                    if not current():
                        return
                    if len(shell.attachments) >= 8:
                        raise ValueError("At most eight attachments are allowed")
                    shell.attachments.append(prepared)
                    shell.composer_insert += shell.attachment_marker(len(shell.attachments)-1)
            else:
                text = str(action["text"])[:1_000_000]
                try:
                    await copy_text(text)
                    if current() and kind == "copy_selection":
                        shell.notice = f"Copied {len(text)} characters"
                except ValueError as exc:
                    if kind != "copy_selection":
                        raise
                    if current():
                        shell.notice = f"Copied {len(text)} characters via the terminal ({exc})"
            if current():
                await self.update()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if current():
                shell.notice = str(exc)
                await self.update()

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
