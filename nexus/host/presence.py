"""Host-level presence: client attachment and first-responder permission leases.

Presence is a subscriber *count*, not identity (PLAN §14.1, §14.6): a single
user with many views. The session already derives ``attended`` from its own live
subscriber count, so this module adds only what a session handle cannot know:

* **client attachment** — which views (by an optional ``client_id``) are
  watching which session, so the daemon can reason about viewers and the idle
  policy;
* **first-responder leases** — when several views race to answer one approval,
  the first claimant holds the lease and the rest are told they lost. This is
  exactly the PLAN §14.6 "first responder wins" rule, made explicit and
  attributable; the session's ``resolve_permission`` remains the final arbiter.

A client id is a debugging aid, never an identity model: nothing here branches
on its value, and two attachments that pass the same id are still distinct
viewers. A lease held by a view that disconnects is released, so a dead tab can
never wedge an approval for the live ones.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..util import new_id


@dataclass(frozen=True)
class Attachment:
    """One view attached to one session. Hashable, so it can be a set member."""

    session: str
    client_id: str
    token: str


@dataclass
class _SessionPresence:
    viewers: int = 0
    clients: dict[str, int] = field(default_factory=dict)
    leases: dict[str, str] = field(default_factory=dict)  # request_id -> token


class Presence:
    """Thread-safe registry of attached views and approval leases.

    All operations are synchronous and cheap, guarded by one re-entrant lock, so
    the facade may attach/detach from an async generator without awaiting.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, _SessionPresence] = {}
        self._tokens: dict[str, tuple[str, str]] = {}  # token -> (session, client)
        self._held: dict[str, set[str]] = {}  # token -> request ids

    # -- attach / detach ---------------------------------------------------

    def attach(self, session: str, client_id: str | None = None) -> Attachment:
        """Register one view of ``session`` and return its attachment token."""
        if not isinstance(session, str) or not session:
            raise ValueError("session must be a non-empty string")
        token = new_id()
        client = client_id if isinstance(client_id, str) and client_id else token
        with self._lock:
            entry = self._sessions.setdefault(session, _SessionPresence())
            entry.viewers += 1
            entry.clients[client] = entry.clients.get(client, 0) + 1
            self._tokens[token] = (session, client)
        return Attachment(session=session, client_id=client, token=token)

    def detach(self, attachment: Attachment) -> None:
        """Unregister a view; releases any leases it still held.

        Releasing on detach is what makes first-responder semantics robust: a
        view that claims a request and then disconnects (or crashes) does not
        leave the approval leased to a ghost, so a live view can still answer.
        """
        token = attachment.token
        with self._lock:
            entry = self._sessions.get(attachment.session)
            if entry is not None:
                entry.viewers = max(0, entry.viewers - 1)
                remaining = entry.clients.get(attachment.client_id, 0) - 1
                if remaining > 0:
                    entry.clients[attachment.client_id] = remaining
                else:
                    entry.clients.pop(attachment.client_id, None)
                    # A lease is keyed by the ``client_id`` a claimant passed to
                    # ``claim`` -- never by the attachment token -- so releasing
                    # must use that client key. Releasing on the last attachment
                    # for this client is what lets a live view answer a request a
                    # disconnecting holder left parked. A shared client id is not
                    # an identity, so a still-attached view keeps the lease.
                    for request_id in self._held.pop(attachment.client_id, set()):
                        if entry.leases.get(request_id) == attachment.client_id:
                            entry.leases.pop(request_id, None)
                if entry.viewers == 0:
                    self._sessions.pop(attachment.session, None)
            self._tokens.pop(token, None)

    # -- queries -----------------------------------------------------------

    def viewers(self, session: str) -> int:
        with self._lock:
            entry = self._sessions.get(session)
            return entry.viewers if entry is not None else 0

    def attended(self, session: str) -> bool:
        """Derived attendance: any attached view counts as attended."""
        return self.viewers(session) > 0

    def clients(self, session: str) -> tuple[str, ...]:
        with self._lock:
            entry = self._sessions.get(session)
            return tuple(entry.clients) if entry is not None else ()

    def active_sessions(self) -> tuple[str, ...]:
        """Sessions with at least one attached view, in insertion order."""
        with self._lock:
            return tuple(name for name, entry in self._sessions.items() if entry.viewers)

    def total_viewers(self) -> int:
        with self._lock:
            return sum(entry.viewers for entry in self._sessions.values())

    # -- first-responder leases -------------------------------------------

    def claim(self, session: str, request_id: str, client_id: str | None = None) -> bool:
        """Claim the right to answer ``request_id``; ``False`` if already held.

        The claimant is identified by ``client_id`` when given, otherwise by a
        fresh anonymous token. The lease is keyed by ``(session, request_id)``,
        so two sessions with the same request id never collide.
        """
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        holder = client_id if isinstance(client_id, str) and client_id else new_id()
        with self._lock:
            entry = self._sessions.setdefault(session, _SessionPresence())
            if request_id in entry.leases:
                return entry.leases[request_id] == holder and self._reclaim(
                    holder, session, request_id
                )
            entry.leases[request_id] = holder
            self._held.setdefault(holder, set()).add(request_id)
        return True

    def release(self, session: str, request_id: str, client_id: str | None = None) -> None:
        """Release a lease, optionally only when ``client_id`` is the holder."""
        with self._lock:
            entry = self._sessions.get(session)
            if entry is None:
                return
            holder = entry.leases.get(request_id)
            if holder is None:
                return
            if client_id is not None and holder != client_id:
                return
            entry.leases.pop(request_id, None)
            held = self._held.get(holder)
            if held is not None:
                held.discard(request_id)
                if not held:
                    self._held.pop(holder, None)

    def holder(self, session: str, request_id: str) -> str | None:
        with self._lock:
            entry = self._sessions.get(session)
            return entry.leases.get(request_id) if entry is not None else None

    def forget(self, session: str) -> None:
        """Drop every view and lease for a deleted/closed session."""
        with self._lock:
            entry = self._sessions.pop(session, None)
            if entry is None:
                return
            for request_id, holder in entry.leases.items():
                held = self._held.get(holder)
                if held is not None:
                    held.discard(request_id)
                    if not held:
                        self._held.pop(holder, None)
            for token, (name, _client) in list(self._tokens.items()):
                if name == session:
                    self._tokens.pop(token, None)

    def _reclaim(self, holder: str, session: str, request_id: str) -> bool:
        """Re-affirm an idempotent claim by the current holder."""
        self._held.setdefault(holder, set()).add(request_id)
        return True


__all__ = ["Attachment", "Presence"]
