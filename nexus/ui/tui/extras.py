"""Small, host-backed chat commands kept outside the shell controller."""

from __future__ import annotations

import json

from ...ui_support.tui_context_header import ContextModal
from .messages import InputSubmitted
from .mock import MockCommandsMixin


class ExtraCommandsMixin(MockCommandsMixin):
    """Dispatch informational and Git workflow commands through the host client."""

    async def _dispatch_extra_command(self, name: str, args: tuple[str, ...]) -> None:
        if name == "/copy":
            result = self._context_preview
            if result is None:
                await self._show_notice("Context is unavailable")
            else:
                data = {field: getattr(result, field) for field in result.__struct_fields__}
                self.copy_to_clipboard(json.dumps(data, ensure_ascii=False, default=str))
                await self._show_notice("Copied context JSON")
        elif name == "/cost":
            usage = self.controller.view.usage
            await self._show_notice(f"Tokens: {usage.input_tokens:,} input · {usage.output_tokens:,} output")
        elif name == "/diff":
            refs = [arg for arg in args if arg != "--staged"]
            if len(refs) > 1:
                await self._show_notice("Use /diff [--staged] [ref]")
                return
            diff = await self.controller.client.git_diff(
                staged="--staged" in args, ref=refs[0] if refs else ""
            )
            body = diff.patch or "No changes"
            self.push_screen(ContextModal("Git diff", body + ("\n\n[diff truncated]" if diff.truncated else "")))
        elif name == "/mock":
            await self._mock_command(args)
        elif name == "/tasks":
            rows = [f"{agent.id} · {agent.status} · {agent.task or agent.description}"
                    for agent in self.controller.view.agents.values()]
            await self._show_notice("\n".join(rows) or "No background tasks")
        elif name == "/reload":
            result = await self.controller.client.reload_extensions(trigger="chat")
            await self._show_notice(str(getattr(result, "diff", result))[:240])
            self._start_context_preview()
        elif name in {"/review", "/commit"}:
            prompt = (
                "Review the current workspace changes for correctness and report findings with file references."
                if name == "/review" else
                "Review the current workspace changes, then create a commit for the completed work. Follow normal tool permissions."
            )
            await self._input_submitted(InputSubmitted(prompt))
