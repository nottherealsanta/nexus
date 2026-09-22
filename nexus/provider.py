"""Legacy Codex CLI transport used by the current CLI path.

On this route Codex owns the model/tool loop; this module owns the subprocess
lifetime and event framing. The provider-neutral contract lives in
:mod:`nexus.model.provider`; :mod:`nexus.model.providers.legacy_codex_cli`
adapts this transport to it.
"""
import asyncio
from collections import deque
from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
import signal
from typing import Protocol

from .config import Config
from .errors import ProviderError
from .events import Event

__all__ = ["Provider", "ProviderError", "CodexProvider"]


class Provider(Protocol):
    def stream(self, prompt: str, *, workspace: Path, config: Config) -> AsyncIterator[Event]: ...


class CodexProvider:
    async def stream(self, prompt: str, *, workspace: Path, config: Config) -> AsyncIterator[Event]:
        command = [config.executable, "exec", "--json", "--ephemeral",
                   "--skip-git-repo-check", "--color", "never", "--sandbox", config.sandbox,
                   "-c", 'approval_policy="never"', "--cd", str(workspace)]
        if config.model:
            command.extend(["--model", config.model])
        command.append("-")
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=8 * 1024 * 1024,
        )
        errors: deque[str] = deque(maxlen=32)

        async def drain_stderr():
            while chunk := await process.stderr.read(4096):
                errors.append(chunk.decode("utf-8", errors="replace"))

        async def send_prompt():
            process.stdin.write(prompt.encode("utf-8"))
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        stderr_task = asyncio.create_task(drain_stderr())
        input_task = asyncio.create_task(send_prompt())
        completed = False
        try:
            async with asyncio.timeout(config.timeout_seconds):
                while line := await process.stdout.readline():
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
                            raise ValueError("Expected an event object with a type")
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise ProviderError("Invalid JSON event from Codex") from exc
                    kind = raw["type"]
                    if kind in {"error", "turn.failed"}:
                        raise ProviderError(json.dumps(raw, ensure_ascii=False))
                    item = raw.get("item")
                    if kind == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                        if not isinstance(item.get("text"), str):
                            raise ProviderError("Codex agent_message is missing text")
                        yield Event("message", {"text": item["text"]})
                    else:
                        yield Event("provider", raw)
                    if kind == "turn.completed":
                        completed = True
                await process.wait()
                await input_task
                await stderr_task
                if process.returncode:
                    raise ProviderError(f"Codex exited {process.returncode}: {''.join(errors)[-8000:]}")
                if not completed:
                    raise ProviderError("Codex exited without turn.completed")
        except TimeoutError as exc:
            raise ProviderError(f"Codex exceeded {config.timeout_seconds:g} seconds") from exc
        finally:
            # Kill the entire process group, including any running tool children.
            # This also runs when a UI cancels or closes the async generator.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            for task in (input_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(input_task, stderr_task, return_exceptions=True)
