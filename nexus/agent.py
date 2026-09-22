"""The entire harness loop: load → build → stream → commit."""
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

from .config import Config
from .context import Exchange, build_context
from .events import Event
from .provider import CodexProvider, Provider
from .store import SessionStore


class Agent:
    def __init__(self, workspace: str | Path = ".", *, provider: Provider | None = None):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {self.workspace}")
        self.provider = provider if provider is not None else CodexProvider()
        self.store = SessionStore(self.workspace / ".nexus" / "sessions")

    async def stream(self, message: str, *, session: str = "default") -> AsyncIterator[Event]:
        """Stream a turn. Use contextlib.aclosing if you may stop consuming early.

        Exceptions propagate to the caller; cancelled/failed turns are not committed.
        """
        with self.store.lock(session):
            config = Config.load(self.workspace)
            history = self.store.load(session)
            context = build_context(
                config.read(self.workspace, config.instructions_file),
                config.read(self.workspace, config.memory_file),
                history, message, config.context_chars,
            )
            yield Event("started", {"session": session, "context_chars": len(context.prompt),
                                    "omitted_exchanges": context.omitted_exchanges})
            messages = []
            async with aclosing(self.provider.stream(context.prompt, workspace=self.workspace, config=config)) as events:
                async for event in events:
                    if event.type == "message":
                        messages.append(event.data["text"])
                    yield event
            answer = "\n\n".join(messages)
            self.store.save(session, [*history, Exchange(message, answer)])
        yield Event("completed", {"session": session, "text": answer})

    async def run(self, message: str, *, session: str = "default") -> str:
        """Convenience API for Python callers who only want the final answer."""
        async with aclosing(self.stream(message, session=session)) as events:
            async for event in events:
                if event.type == "completed":
                    return event.data["text"]
        raise RuntimeError("Turn ended without completion")
