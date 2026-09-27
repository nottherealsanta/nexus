"""Pure, transport-neutral client for the host protocol (PLAN section 14.8).

The daemon owns all runtime execution. This client speaks protocol commands
only; its injected transport makes it testable without starting a daemon.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from ..events import Event
from ..host import protocol as p


class ClientError(Exception):
    """Base class for failures raised by a host protocol client."""


class ProtocolVersionError(ClientError):
    """The daemon speaks a different protocol revision than the client."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"protocol version mismatch: client speaks v{expected}, "
            f"daemon reports v{actual}; upgrade the client and daemon together"
        )


class FacadeError(ClientError):
    """A command was rejected by the facade, with its redacted message."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}" if message else kind)


class TransportClosed(ClientError):
    """The transport went away mid-command or mid-stream."""


@runtime_checkable
class Transport(Protocol):
    """Request/result and event-stream operations shared by each transport."""

    async def request(self, command: p.Command) -> p.Result:
        ...

    def events(
        self,
        session: str,
        from_seq: int = 0,
        *,
        follow: bool = True,
        client_id: str | None = None,
    ) -> AsyncIterator[Event]:
        ...

    async def aclose(self) -> None:
        ...


class Client:
    """A typed, transport-neutral client over the host protocol."""

    def __init__(
        self,
        transport: Transport,
        *,
        expected_version: int = p.PROTOCOL_VERSION,
        client_id: str | None = None,
    ) -> None:
        self.transport = transport
        self.expected_version = expected_version
        self.client_id = client_id or uuid.uuid4().hex
        self._health: p.HealthResult | None = None

    @property
    def health(self) -> p.HealthResult | None:
        return self._health

    async def handshake(self) -> p.HealthResult:
        """Exchange versions and pin the daemon's health snapshot."""
        result = await self._request(p.Health())
        if not isinstance(result, p.HealthResult):
            raise ClientError(f"unexpected handshake result {type(result).__name__}")
        if result.version != self.expected_version:
            raise ProtocolVersionError(self.expected_version, result.version)
        self._health = result
        return result

    async def list_sessions(self) -> list[Any]:
        return list((await self._request(p.SessionList())).sessions)  # type: ignore[union-attr]

    async def search_files(self, query: str, limit: int = 30) -> list[str]:
        return list((await self._request(p.FileSearch(query=query, limit=limit))).paths)  # type: ignore[union-attr]

    async def list_worktrees(self) -> p.WorktreeListResult: return await self._request(p.WorktreeList())  # type: ignore[return-value]

    async def inspect_worktree(self, child_id: str) -> p.WorktreeInspectResult: return await self._request(p.WorktreeInspect(child_id=child_id))  # type: ignore[return-value]

    async def review_worktree(self, child_id: str, *, review_id: str | None = None,
                              cursor: int = 0, limit: int = 1) -> p.WorktreeReviewResult:
        command = p.WorktreeReview(child_id=child_id, review_id=review_id, cursor=cursor, limit=limit)
        return await self._request(command)  # type: ignore[return-value]

    async def acknowledge_worktree(self, child_id: str, review_id: str, digest: str) -> p.WorktreeAcknowledgeResult:
        command = p.WorktreeAcknowledge(child_id=child_id, review_id=review_id, digest=digest)
        return await self._request(command)  # type: ignore[return-value]

    async def integrate_worktree(self, child_id: str, review_id: str, digest: str, *,
                                 confirmation_token: str = "") -> p.WorktreeMutationResult:
        command = p.WorktreeIntegrate(child_id=child_id, review_id=review_id, digest=digest, confirmation_token=confirmation_token)
        return await self._request(command)  # type: ignore[return-value]

    async def discard_worktree(self, child_id: str, *, force: bool = False,
                               review_id: str | None = None,
                               confirmation_token: str = "") -> p.WorktreeMutationResult:
        command = p.WorktreeDiscard(child_id=child_id, force=force, review_id=review_id, confirmation_token=confirmation_token)
        return await self._request(command)  # type: ignore[return-value]

    async def open_session(self, session: str, *, create: bool = True, recover: bool = True) -> Any:
        return (await self._request(p.SessionOpen(session=session, create=create, recover=recover))).session  # type: ignore[union-attr]

    async def start_turn(self, session: str, content: str) -> str:
        return (await self._request(p.SessionStart(session=session, content=content))).turn_id  # type: ignore[union-attr]

    async def enqueue(self, session: str, content: str) -> tuple[str, str]:
        result = await self._request(p.SessionEnqueue(session=session, content=content))
        return result.queued_id, result.turn_id  # type: ignore[union-attr]

    async def cancel(self, session: str, *, reason: str = "", drop_queue: bool = True) -> tuple[bool, int]:
        result = await self._request(p.SessionCancel(session=session, reason=reason, drop_queue=drop_queue))
        return result.cancelled, result.dropped  # type: ignore[union-attr]

    async def fork(self, session: str, at_seq: int | None = None, *, new_id: str | None = None) -> Any:
        return (await self._request(p.SessionFork(session=session, at_seq=at_seq, new_id=new_id))).session  # type: ignore[union-attr]

    async def delete(self, session: str, *, force: bool = False, reason: str = "") -> tuple[str, float]:
        result = await self._request(p.SessionDelete(session=session, force=force, reason=reason))
        return result.trash_id, result.delete_after  # type: ignore[union-attr]

    async def restore(self, trash_id: str) -> str:
        return (await self._request(p.SessionRestore(trash_id=trash_id))).session  # type: ignore[union-attr]

    async def export(self, session: str, *, format: str = "markdown") -> str:
        return (await self._request(p.SessionExport(session=session, format=format))).content  # type: ignore[union-attr]

    async def state(self, session: str, from_seq: int = 0) -> tuple[dict[str, Any], int]:
        result = await self._request(p.SessionState(session=session, from_seq=from_seq))
        return dict(result.view), result.seq  # type: ignore[union-attr]

    async def read_logs(self, session: str | None = None, *, daemon_cursor: str | None = None,
                        session_cursor: int | None = None, limit: int = 50) -> p.LogsReadResult:
        command = p.LogsRead(session=session, daemon_cursor=daemon_cursor, session_cursor=session_cursor, limit=limit)
        return await self._request(command)  # type: ignore[return-value]

    async def agent_transcript(self, session: str, agent_id: str) -> dict[str, Any]:
        result = await self._request(
            p.AgentTranscript(session=session, agent_id=agent_id)
        )
        return {
            "found": result.found,  # type: ignore[union-attr]
            "status": result.status,  # type: ignore[union-attr]
            "view": dict(result.view),  # type: ignore[union-attr]
        }

    async def resolve_permission(self, session: str, request_id: str, decision: str) -> bool:
        command = p.PermissionResolve(session=session, request_id=request_id, decision=decision, client_id=self.client_id)
        result = await self._request(command)
        return bool(result.resolved)  # type: ignore[union-attr]

    async def reload_extensions(self, trigger: str = "api") -> Any: return await self._request(p.ExtensionsReload(trigger=trigger))

    async def list_extensions(self) -> tuple[int, list[dict[str, Any]]]:
        result = await self._request(p.ExtensionsList())
        return result.generation, list(result.extensions)  # type: ignore[union-attr]

    async def validate_extensions(self, target: str | None = None) -> Any: return await self._request(p.ExtensionsValidate(target=target))

    async def trash_extensions(self, target: str, *, reason: str = "", force: bool = False) -> Any:
        return await self._request(p.ExtensionsTrash(target=target, reason=reason, force=force))

    async def list_models(
        self, *, provider: str | None = None, tier: str | None = None,
        selectable_only: bool = False, search: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._request(p.ModelsList(provider=provider, tier=tier, selectable_only=selectable_only, search=search))
        return list(result.models)  # type: ignore[union-attr]

    async def show_model(self, ref: str) -> Any: return await self._request(p.ModelShow(ref=ref))

    async def model_tiers(self) -> Any: return await self._request(p.ModelTiers())

    async def select_model(self, session: str, ref: str) -> Any: return await self._request(p.ModelSelect(session=session, ref=ref))

    async def select_reasoning_effort(self, session: str, effort: str | None) -> Any: return await self._request(p.ReasoningEffortSelect(session=session, effort=effort))

    async def refresh_models(self) -> Any: return await self._request(p.ModelsRefresh())

    async def list_agents(self) -> list[dict[str, Any]]: return list((await self._request(p.AgentsList())).agents)  # type: ignore[union-attr]

    async def current_agent(self, session: str) -> Any: return await self._request(p.AgentCurrent(session=session))

    async def select_agent(self, session: str, name: str) -> Any: return await self._request(p.AgentSelect(session=session, name=name))

    async def reset_agent(self, session: str) -> Any: return await self._request(p.AgentReset(session=session))

    async def list_tools(self) -> list[dict[str, Any]]: return list((await self._request(p.ToolsList())).tools)  # type: ignore[union-attr]

    async def inspect_context(self, session: str) -> p.ContextInspectResult:
        """Preview next-turn standing context; no prompt is persisted or sent."""
        return await self._request(p.ContextInspect(session=session))  # type: ignore[return-value]

    async def doctor(self, *, explain_reload: bool = False) -> Any: return await self._request(p.Doctor(explain_reload=explain_reload))

    async def shutdown(self, reason: str = "") -> bool: return bool((await self._request(p.Shutdown(reason=reason))).stopping)  # type: ignore[union-attr]

    def stream(self, session: str, from_seq: int = 0, *, follow: bool = True) -> AsyncIterator[Event]:
        return self.transport.events(session, from_seq, follow=follow, client_id=self.client_id)

    async def _request(self, command: p.Command) -> p.Result:
        result = await self.transport.request(command)
        if isinstance(result, p.ErrorResult):
            raise FacadeError(result.kind, result.message)
        return result

    async def aclose(self) -> None: await self.transport.aclose()


__all__ = [
    "Client",
    "ClientError",
    "FacadeError",
    "ProtocolVersionError",
    "Transport",
    "TransportClosed",
]
