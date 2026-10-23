"""The single surface API: ``HostFacade`` over one :class:`Runtime`.

PLAN §14.4: "``host/facade.py`` is the **only** surface API. Everything a UI can
do is here; anything not here, a UI cannot do." The facade is transport-neutral:
it returns domain values (``SessionSummary``, ``ConversationView``, strings) and
the wire layer wraps them with the structs in :mod:`nexus.host.protocol`.

Responsibilities:

* **own every session operation** the PLAN §14.4 verb list names, delegating to
  the :class:`~nexus.runtime.Runtime` and its managers rather than reimplementing
  them;
* **schedule turns** through :class:`~nexus.host.supervisor.Supervisor`, so a
  global concurrency cap and fair per-session queues apply to every surface;
* **track views** through :class:`~nexus.host.presence.Presence`, giving
  first-responder semantics to permission races and derived attendance;
* **produce a view baseline** by folding the session log through the pure
  ``view/`` reducer, so a late joiner catches up from the same code every
  surface renders with.

The facade never returns a credential, environment value, or raw configuration:
errors are redacted, health reports counters only, and session projections cross
the same content contract as an export. There is deliberately no method that
hands a surface a ``Runtime``, a manager, or a tool.
"""
from __future__ import annotations

import asyncio
import contextlib
import base64
import hashlib
import hmac
import inspect
import json
import os  # noqa: F401 - preserves the facade's historical scandir patch seam
import secrets
import threading
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import msgspec

from ..agents.manager_prompt import MANAGER_KIND, MANAGER_MODEL_REF, MANAGER_PROVIDER, MANAGER_SIGN_IN_HINT
from ..agents.runner import cancel_child
from ..config.schema import ManagerSection
from ..devtools import dev_enabled
from ..errors import ConfigError, ExtensionTrashError, SessionBusy, SessionError
from ..events import Event
from ..ext.quarantine import sanitize_text
from ..host_support.attachments import AttachmentStore
from ..host_support.agent_context import project_agent_context
from ..host_support.browser_view import json_patch as _json_patch
from ..host_support.browser_view import web_view as _web_view
from ..host_support.context_preview import (
    project_context_preview,
)
from ..host_support.context_preview import (
    safe_text as _worktree_text,
)
from ..host_support import model_settings
from ..host_support.auto_title import AutoTitler, set_user_title
from ..host_support.session_jobs import SessionJobControl
from ..host_support.user_shell import UserShells
from ..host_support.doctor import doctor_report
from ..host_support.update_check import claim_announcement, update_status
from ..host_support.git_diff import git_diff
from ..host_support.git_head import git_head
from ..host_support.workspace_browse import git_status as workspace_git_status
from ..host_support.workspace_browse import list_directory, preview_file
from ..host_support.mock import dispatch_mock
from ..host_support.provider_auth import dispatch_providers
from ..host_support.session_archive import (
    archive_summary_count,
    dispatch_archive_command,
)
from ..host_support.settings_inventory import dispatch_settings
from ..host_support.setup import setup_save, setup_status
from ..host_support.workspace import search_files as _search_files
from ..host_support.workspace import session_workspace as _session_workspace
from ..host_support.live_consent import read_consent
from ..host_support.manager_port import FacadeManagerHost, HostManagerPort, scan_manager_log
from ..host_support.provider_auth import connected as provider_connected
from ..host_support.voice import dispatch_voice, doctor_voice
from ..host_support.worktree_projection import review_hex
from ..host_support.worktree_projection import (
    worktree_diff_row as _worktree_diff_row,
)
from ..host_support.worktree_projection import (
    worktree_record as _worktree_record,
)
from ..host_support.worktree_projection import (
    worktree_review_entry as _worktree_review_entry,
)
from ..model.message import ContentBlock
from ..model.reasoning_effort import ReasoningEffortSelection
from ..model.request import REASONING_EFFORTS
from ..model.selection import ModelSelection
from ..observability.session import (
    read_session_page,
    session_records,
    validate_daemon_cursor,
    validate_logs_read,
)
from ..session.manager import SessionSummary, TrashRecord
from ..tools.questions import QuestionAnswerError
from ..util import redact_secrets
from ..view import (
    ConversationView,
    apply,
    initial_state,
    jsonable,
)
from . import protocol as p
from .presence import Presence
from .reports import session_activity
from .supervisor import Supervisor
from ..host_support.usage_snapshot import UsageSnapshotService

_MAX_WORKTREE_LIST = 100
_MAX_WORKTREE_REVIEW_PAGE = 8
_MAX_WORKTREE_REVIEW_BYTES = 8 * 128 * 1024
_WORKTREE_CONFIRMATION_TTL = 120
_MAX_FILE_SEARCH_ENTRIES = 50_000

#: Default global cap when the caller does not supply one.
DEFAULT_MAX_CONCURRENT_TURNS = 4

#: The workspace manager's fixed session id (plan 02, docs/manager.md). Reserved: no
#: other session may be created under it, so ``ManagerOpen`` is a true singleton.
MANAGER_SESSION_ID = "nexus-manager"
MANAGER_DISABLED_MESSAGE = (
    "The manager is turned off. Set [manager] enabled = true in the Nexus config to use it."
)
#: Newest managed ids ``ManagerState`` lists.
_MAX_MANAGED_LISTED = 50


class HostFacade:
    """Wrap one runtime as the complete, transport-neutral surface API."""

    resource_limits = None
    project_key = ""

    def __init__(
        self,
        runtime: Any,
        *,
        max_concurrent_turns: int = DEFAULT_MAX_CONCURRENT_TURNS,
        owns_runtime: bool = False,
        emit: Any | None = None,
        supervisor: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.daemon_info: dict[str, Any] = {}
        self.resource_limits = None
        self.project_key = ""
        self.attachments = AttachmentStore(runtime)
        self.titles = AutoTitler(runtime)
        self.shells = UserShells(runtime)
        self.jobs = SessionJobControl(runtime)
        self.presence = Presence()
        # Plan-usage snapshot for sub-agent routing (docs/host.md): the runtime reads it,
        # this service keeps it fresh and shares the usage panel's fetches.
        self.usage = UsageSnapshotService(runtime)
        if getattr(runtime, "usage_kick", False) is None:
            runtime.usage_kick = self.usage.kick
        self.supervisor = supervisor if supervisor is not None else Supervisor(max_concurrent=max_concurrent_turns, emit=emit)
        self._owns_runtime = bool(owns_runtime)
        self._started = time.time()
        self._foreground: dict[str, tuple[str, float]] = {}
        self._closed = False
        self._managed: set[str] = set()
        # session id -> (folded view, events consumed, the last of them); see state().
        self._state_cache: dict[str, tuple[ConversationView, int, Event]] = {}
        self._worktree_confirmation_key = secrets.token_bytes(32)
        self._worktree_lock = threading.Lock()
        self._worktree_confirmation_lock = threading.Lock()
        self._worktree_used_tokens: dict[str, int] = {}
        # The manager's eight tools reach the host only through the port this builds
        # (docs/manager.md). A runtime that already has a factory (tests) keeps it.
        if getattr(runtime, "manager_port_factory", False) is None:
            runtime.manager_port_factory = self._manager_port
        # A background job's recorded completion wakes an idle session through
        # the same supervisor as any queued input (docs/shell-jobs: plan 3.5).
        self._wake_tasks: set[asyncio.Future[Any]] = set()
        if getattr(runtime, "job_completion_notifier", False) is None:
            runtime.job_completion_notifier = self._notify_job_completion

    def _notify_job_completion(self, session_id: str, admission: Any) -> None:
        """Runtime seam: schedule the turn for one admitted completion.

        The notification is already durable in the session queue; this only asks
        the supervisor to run it. An active turn consumes it at its next boundary
        (the parked submission is then skipped as its id is gone), so a burst
        yields one turn, not one per job.
        """
        if self._closed:
            return
        try:
            handle = self._session(session_id, create=False, recover=False)
        except Exception:  # noqa: BLE001 - the notification stays queued
            return
        task = asyncio.ensure_future(
            self.supervisor.submit(
                session_id, handle, None,
                queued_id=admission.queued_id, turn_id=admission.turn_id,
            )
        )
        self._wake_tasks.add(task)
        task.add_done_callback(self._wake_tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _manager_port(self, session: Any) -> HostManagerPort:
        return HostManagerPort(
            self.runtime.sessions, handle=session, host=FacadeManagerHost(self),
        )

    @property
    def max_concurrent_turns(self) -> int:
        return self.supervisor.max_concurrent

    @property
    def closed(self) -> bool:
        return self._closed

    # -- session lifecycle -------------------------------------------------

    def list_sessions(self, *, include_children: bool = False) -> list[SessionSummary]:
        """Every session, newest activity first (PLAN §14.4)."""
        return list(self.runtime.sessions.list(include_children=True) if include_children else self.runtime.sessions.list())

    # -- the manager session (plan 02, docs/manager.md) -------------------

    def manager_config(self) -> ManagerSection:
        """The effective ``[manager]`` section; the defaults (disabled) when unreadable."""
        loader = getattr(self.runtime, "_load_config", None)
        try:
            config = loader() if callable(loader) else None
        except (ConfigError, OSError):
            config = None
        section = getattr(getattr(config, "v2", None), "manager", None)
        return section if isinstance(section, ManagerSection) else ManagerSection()

    def open_manager(self) -> tuple[SessionSummary, bool]:
        """Open or create the workspace's singleton manager session; ``(summary, created)``.

        Idempotent: later calls return the same session with ``created`` false. The
        row's kind is ``manager`` from its first record, so no other path can make a
        regular session out of it or the reverse. Needs ``[manager] enabled``.
        """
        if not self.manager_config().enabled:
            raise ConfigError(MANAGER_DISABLED_MESSAGE)
        sessions = self.runtime.sessions
        created = sessions.manager_summary() is None
        if created:
            if sessions.store.row_exists(MANAGER_SESSION_ID):
                raise SessionError(
                    f"session id {MANAGER_SESSION_ID!r} is already used by a regular session; "
                    "rename or delete it to use the manager"
                )
            sessions.open(
                MANAGER_SESSION_ID, create=True, recover=False, ownership={"kind": MANAGER_KIND}
            )
        self._session(MANAGER_SESSION_ID)
        return sessions.summary(MANAGER_SESSION_ID), created

    async def manager_state(self) -> p.ManagerStateResult:
        """The manager's id, pinned model, sign-in state and managed roots (read-only, never raises for 'off')."""
        section = self.manager_config()
        sessions = self.runtime.sessions
        summary = sessions.manager_summary() if section.enabled else None
        home = getattr(self.runtime, "_home", None)
        copilot = await provider_connected(self.runtime, MANAGER_PROVIDER)
        codex = await provider_connected(self.runtime, "codex")
        base = bool(read_consent(home))
        cross = bool(read_consent(home, "manager"))
        managed: list[str] = []
        if summary is not None:
            try:
                records = await asyncio.to_thread(lambda: sessions.store.read(summary.id).records)
                managed = list(dict.fromkeys(link.child for link in scan_manager_log(records).links))
            except Exception:  # noqa: BLE001 - state is informational; an unreadable log lists nothing
                managed = []
        return p.ManagerStateResult(
            enabled=section.enabled,
            session=summary.id if summary is not None else "",
            state=summary.state if summary is not None else "",
            model=MANAGER_MODEL_REF,
            provider=MANAGER_PROVIDER,
            signed_in=copilot,
            sign_in="" if copilot else MANAGER_SIGN_IN_HINT,
            codex_signed_in=codex,
            live_consent=base,
            live_manager_consent=cross,
            live_ready=codex and base and cross,
            managed=managed[-_MAX_MANAGED_LISTED:],
            max_launches_per_turn=section.max_launches_per_turn,
            max_live_managed=section.max_live_managed,
        )

    def _refuse_manager(self, session_id: str, what: str) -> None:
        """Refuse ``what`` for the manager session (it is structural, not configurable)."""
        kind_of = getattr(self.runtime.sessions, "kind_of", None)
        if callable(kind_of) and kind_of(session_id) == MANAGER_KIND:
            raise ConfigError(f"the manager session does not support {what}")

    def search_files(
        self, query: str, limit: int = 30, *, include_directories: bool = False,
        session: str = "",
    ) -> list[str]:
        """Return bounded, visible workspace-relative file path matches."""
        return _search_files(
            self.runtime,
            query,
            limit,
            max_entries=_MAX_FILE_SEARCH_ENTRIES,
            include_directories=include_directories,
            session=session,
        )

    def _file_search_denied_roots(self) -> tuple[Path, ...]:
        """Read current hard read boundaries without exposing other config."""
        from ..host_support.workspace import _file_search_denied_roots

        return _file_search_denied_roots(self.runtime)

    def list_worktrees(self) -> tuple[list[dict[str, Any]], bool]:
        """Return a bounded allowlisted view of runtime-owned worktree records."""
        records = tuple(self.runtime.list_worktrees())
        rows = [_worktree_record(record) for record in records[:_MAX_WORKTREE_LIST]]
        return rows, len(records) > _MAX_WORKTREE_LIST

    async def _invoke_runtime_worktree(self, name: str, *args, **kwargs) -> Any:
        """Invoke session worktree actions on their owning event loop.

        Mutators append session events and touch loop-affine buses/handles. Only
        the read-only status projection is sent to a worker thread.
        """
        method = getattr(self.runtime, name, None)
        if not callable(method):
            raise ValueError("Session worktree operations are unavailable")
        if name == "session_worktree_status":
            result = await asyncio.to_thread(method, *args, **kwargs)
        else:
            result = method(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def inspect_worktree(self, child_id: str) -> dict[str, Any]:
        """Inspect one authenticated runtime-owned child without exposing paths."""
        record = self.runtime.inspect_worktree(child_id)
        return _worktree_record(record)

    def review_worktree(
        self,
        child_id: str,
        *,
        review_id: str | None = None,
        cursor: int = 0,
        limit: int = 1,
    ) -> dict[str, Any]:
        """Read one bounded review page and project only sanitized metadata."""
        if not isinstance(child_id, str) or not child_id or len(child_id) > 256:
            raise ValueError("child_id must be a non-empty string of at most 256 characters")
        if review_id is not None and (
            not isinstance(review_id, str)
            or len(review_id) != 32
            or any(char not in "0123456789abcdef" for char in review_id)
        ):
            raise ValueError("invalid review id")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("review cursor must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_WORKTREE_REVIEW_PAGE
        ):
            raise ValueError(
                f"review page limit must be between 1 and {_MAX_WORKTREE_REVIEW_PAGE}"
            )

        page = self.runtime.review_worktree(
            child_id,
            review_id=review_id,
            cursor=cursor,
            limit=limit,
        )
        raw_entries = page.manifest.get("entries", ())
        entries = [
            _worktree_review_entry(item)
            for item in (raw_entries[:500] if isinstance(raw_entries, list) else ())
            if isinstance(item, Mapping)
        ]
        remaining = _MAX_WORKTREE_REVIEW_BYTES
        diff: list[dict[str, Any]] = []
        diff_bytes = b"".join(
            raw_page
            for raw_page in page.diff_pages[:_MAX_WORKTREE_REVIEW_PAGE]
            if isinstance(raw_page, bytes)
        )[:remaining]
        for line in diff_bytes.splitlines()[:500]:
            try:
                row = msgspec.json.decode(line, type=dict[str, Any])
            except (msgspec.DecodeError, TypeError):
                continue
            diff.append(_worktree_diff_row(row, remaining))
            remaining -= len(line)
        if (
            not isinstance(page.review_id, str)
            or not isinstance(page.digest, str)
        ):
            raise TypeError("worktree review returned invalid identifiers")
        if (
            len(page.review_id) != 32
            or any(char not in "0123456789abcdef" for char in page.review_id)
            or len(page.digest) != 64
            or any(char not in "0123456789abcdef" for char in page.digest)
        ):
            raise ValueError("worktree review returned invalid identifiers")
        record = self.inspect_worktree(child_id)
        return {
            "record": record,
            "status": record["lifecycle"] or "unknown",
            "entries": entries,
            "diff": diff,
            "cursor": page.cursor,
            "has_more": page.next_cursor is not None,
            "review_id": review_hex(page.review_id, 32),  # validated hex above; redaction would corrupt it
            "digest": review_hex(page.digest, 64),
        }

    def acknowledge_worktree(
        self, child_id: str, review_id: str, digest: str
    ) -> dict[str, Any]:
        child_id = _validate_worktree_child_id(child_id)
        if not _hex_id(review_id, 32) or not _hex_id(digest, 64):
            raise ValueError("invalid worktree review identifier or digest")
        acknowledged = self.runtime.acknowledge_worktree(child_id, review_id, digest)
        del acknowledged
        return {"review_id": review_id, "digest": digest, "status": "acknowledged"}

    def _worktree_state(self, child_id: str) -> dict[str, Any]:
        reader = getattr(self.runtime, "worktree_confirmation_state", None)
        if not callable(reader):
            raise TypeError("runtime cannot prove worktree confirmation state; mutation refused")
        state = reader(child_id)
        if not isinstance(state, dict):
            raise TypeError("runtime returned invalid worktree confirmation state")
        return state

    def _confirmation_token(
        self, operation: str, child_id: str, state: dict[str, Any], options: dict[str, Any]
    ) -> str:
        expires = int(time.time()) + _WORKTREE_CONFIRMATION_TTL
        payload = {
            "operation": operation,
            "child_id": child_id,
            "state": hashlib.sha256(_canonical_json(state)).hexdigest(),
            "options": options,
            "expires": expires,
            "nonce": secrets.token_hex(16),
        }
        encoded = msgspec.json.encode(payload)
        signature = hmac.new(self._worktree_confirmation_key, encoded, hashlib.sha256).hexdigest()
        return f"{_b64url(encoded)}.{signature}"

    def _consume_confirmation(
        self,
        token: str,
        operation: str,
        child_id: str,
        state: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        if not isinstance(token, str) or len(token) > 2048 or token.count(".") != 1:
            raise ValueError("missing or invalid confirmation token")
        encoded_text, signature = token.split(".", 1)
        try:
            encoded = _b64url_decode(encoded_text)
            payload = msgspec.json.decode(encoded, type=dict[str, Any])
        except Exception as exc:
            raise ValueError("invalid confirmation token") from exc
        expected = hmac.new(self._worktree_confirmation_key, encoded, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid confirmation token")
        token_digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        with self._worktree_confirmation_lock:
            self._worktree_used_tokens = {
                digest: expiry
                for digest, expiry in self._worktree_used_tokens.items()
                if expiry >= int(time.time())
            }
            if token_digest in self._worktree_used_tokens:
                raise ValueError("confirmation token has already been used")
        if (
            payload.get("operation") != operation
            or payload.get("child_id") != child_id
            or payload.get("options") != options
            or isinstance(payload.get("expires"), bool)
            or not isinstance(payload.get("expires"), int)
            or payload["expires"] < int(time.time())
        ):
            raise ValueError("confirmation token is expired or does not match this operation")
        state_digest = hashlib.sha256(_canonical_json(state)).hexdigest()
        if not hmac.compare_digest(str(payload.get("state", "")), state_digest):
            raise ValueError("worktree state changed since confirmation preview")
        with self._worktree_confirmation_lock:
            if token_digest in self._worktree_used_tokens:
                raise ValueError("confirmation token has already been used")
            if len(self._worktree_used_tokens) >= 8192:
                raise ValueError("confirmation token capacity reached; retry with a fresh preview")
            self._worktree_used_tokens[token_digest] = payload["expires"]

    def mutate_worktree(
        self,
        operation: str,
        child_id: str,
        *,
        review_id: str | None = None,
        digest: str | None = None,
        force: bool = False,
        confirmation_token: str = "",
        cancel: object | None = None,
    ) -> dict[str, Any]:
        child_id = _validate_worktree_child_id(child_id)
        if operation not in {"integrate", "discard"}:
            raise ValueError("unsupported worktree operation")
        if not isinstance(force, bool):
            raise TypeError("force must be an explicit boolean")
        if operation == "integrate":
            if not _hex_id(review_id, 32) or not _hex_id(digest, 64):
                raise ValueError("integrate requires a valid review id and digest")
            options = {"review_id": review_id, "digest": digest, "force": False}
        else:
            if review_id is not None and not _hex_id(review_id, 32):
                raise ValueError("invalid review id")
            options = {"review_id": review_id, "digest": None, "force": force}

        with self._worktree_lock:
            state = self._worktree_state(child_id)
            record = state.get("record", {})
            if not isinstance(record, dict):
                raise TypeError("runtime cannot authenticate worktree record state")
            if operation == "integrate":
                if (
                    record.get("lifecycle") != "finalized"
                    or record.get("current_review_id") != review_id
                    or record.get("current_review_digest") != digest
                    or record.get("acknowledged_review_id") != review_id
                    or record.get("acknowledged_digest") != digest
                ):
                    raise ValueError("integration requires the current acknowledged review")
                parent_state = state.get("parent", {})
                if (
                    not isinstance(parent_state, dict)
                    or parent_state.get("dirty")
                    or parent_state.get("head") != record.get("base_commit")
                ):
                    raise ValueError("integration requires a clean parent at the recorded base commit")
            elif not force and (
                record.get("lifecycle") != "finalized"
                or record.get("current_review_id")
                != (review_id or record.get("acknowledged_review_id"))
                or record.get("acknowledged_review_id")
                != (review_id or record.get("acknowledged_review_id"))
                or record.get("current_review_digest") != record.get("acknowledged_digest")
            ):
                raise ValueError("discard requires the current acknowledged review")
            effective_review_id = (
                review_id or record.get("acknowledged_review_id")
                if operation == "discard" and not force
                else review_id
            )

            impact = {
                "parent_clean": not bool(state.get("parent", {}).get("dirty")),
                "parent_head_matches_base": state.get("parent", {}).get("head") == record.get("base_commit"),
                "child_dirty": bool(state.get("child", {}).get("dirty")),
                "lifecycle": str(record.get("lifecycle", "unknown"))[:32],
            }
            if operation == "integrate":
                impact["review_digest"] = str(digest)
                impact["summary"] = "Apply the acknowledged frozen review to a clean parent checkout"
            else:
                impact["force"] = force
                impact["summary"] = (
                    "Remove this owned child worktree, including dirty files"
                    if force
                    else "Remove this owned clean child worktree"
                )

            if not confirmation_token:
                token = self._confirmation_token(operation, child_id, state, options)
                return {
                    "status": "requires_confirmation",
                    "operation": operation,
                    "review_id": review_id,
                    "digest": digest,
                    "confirmation_token": token,
                    "impact": impact,
                }

            self._consume_confirmation(
                confirmation_token, operation, child_id, state, options
            )
            try:
                if operation == "integrate":
                    outcome = self.runtime.integrate_worktree(
                        child_id, review_id, digest, cancel=cancel
                    )
                    status_map = {
                        "integrated": "committed",
                        "rolled_back": "rolled_back",
                        "recovery_required": "recovery_required",
                    }
                    status = status_map.get(outcome.status, "recovery_required")
                    return {
                        "status": status,
                        "operation": operation,
                        "review_id": review_id,
                        "digest": digest,
                        "impact": impact,
                        "transaction_id": outcome.transaction_id,
                        "changed_paths": [
                            _worktree_text(path, 1024)
                            for path in outcome.changed_paths[:500]
                        ],
                        "error": (
                            redact_secrets(outcome.error)[:500]
                            if outcome.error
                            else None
                        ),
                    }
                outcome = self.runtime.discard_worktree(
                    child_id,
                    force=force,
                    review_id=effective_review_id,
                    cancel=cancel,
                )
                return {
                    "status": "committed" if outcome.lifecycle == "discarded" else "cleanup_pending",
                    "operation": operation,
                    "review_id": review_id,
                    "impact": impact,
                }
            except Exception as exc:
                if operation == "discard":
                    try:
                        fresh = self._worktree_state(child_id)
                        current = fresh.get("record", {})
                        status = (
                            "cleanup_pending"
                            if isinstance(current, dict) and current.get("lifecycle") == "cleanup_pending"
                            else "committed"
                            if isinstance(current, dict) and current.get("lifecycle") == "discarded"
                            else None
                        )
                    except Exception:  # noqa: BLE001 - unknown cleanup state fails closed
                        status = "cleanup_pending"
                    if status is None:
                        if isinstance(exc, _WorktreeMutationCancelled):
                            raise
                        raise ValueError(redact_secrets(str(exc))[:500]) from exc
                else:
                    try:
                        fresh = self._worktree_state(child_id)
                        changed = _canonical_json(fresh) != _canonical_json(state)
                    except Exception:  # noqa: BLE001 - unknown transaction state fails closed
                        changed = True
                    if isinstance(exc, _WorktreeMutationCancelled):
                        if changed:
                            status = "rolled_back"
                        else:
                            raise
                    elif changed:
                        status = "recovery_required"
                    else:
                        raise ValueError(redact_secrets(str(exc))[:500]) from exc
                return {
                    "status": status,
                    "operation": operation,
                    "review_id": review_id,
                    "digest": digest,
                    "impact": impact,
                    "error": redact_secrets(str(exc))[:500],
                }

    def open_session(
        self,
        session_id: str,
        *,
        create: bool = True,
        recover: bool = True,
    ) -> SessionSummary:
        """Open (migrating/recovering) a session and return its summary."""
        self._session(session_id, create=create, recover=recover)
        return self.runtime.sessions.summary(session_id)

    def fork(
        self,
        session_id: str,
        at_seq: int | None = None,
        *,
        new_id: str | None = None,
    ) -> SessionSummary:
        """Branch a session at ``at_seq`` into a new one."""
        if new_id == MANAGER_SESSION_ID:
            raise ValueError(f"session id {MANAGER_SESSION_ID!r} is reserved for the manager")
        child = self.runtime.sessions.fork(session_id, at_seq, new_id=new_id)
        return self.runtime.sessions.summary(child.id)

    def delete(
        self, session_id: str, *, force: bool = False, reason: str = ""
    ) -> TrashRecord:
        """Move a session's artifacts to trash (never cancelling a turn).

        Refuses any session the supervisor still holds work for -- a running
        turn or a parked submission -- and any live session with persisted
        queued input, because deleting under that work would let a durable
        submission resurrect a trashed session. Cancel explicitly (dropping the
        queue) first, then delete.
        """
        if self.supervisor.running_for(session_id) or self.supervisor.queued_for(
            session_id
        ):
            raise SessionBusy(
                f"Session {session_id!r} has scheduled work; cancel it explicitly "
                "before deleting (delete never cancels a turn)"
            )
        queued = getattr(self.runtime.sessions, "queued_depth", None)
        if callable(queued) and queued(session_id) > 0:
            raise SessionBusy(
                f"Session {session_id!r} has queued input; drop it explicitly "
                "before deleting (delete never drops a queue)"
            )
        record = self.runtime.sessions.delete(session_id, force=force, reason=reason)
        self.titles.cancel(session_id)
        self.presence.forget(session_id)
        # The handle is gone, so the scheduler must not retain it. This is a
        # no-op if the session still has an active/queued turn (which delete
        # already refused), so forgetting can never strand live work.
        self.supervisor.forget(session_id)
        self._managed.discard(session_id)
        return record

    def rename(self, session_id: str, title: str) -> SessionSummary:
        """Set a session's title durably as a user title (never replaced by auto-titling)."""
        set_user_title(self.runtime.sessions, session_id, title)
        self.titles.cancel(session_id)  # a slower side call must not even try
        return self.runtime.sessions.summary(session_id)

    def restore(self, trash_id: str) -> str:
        """Restore a trashed session; return its session id."""
        return self.runtime.sessions.restore(trash_id)

    def export(self, session_id: str, *, format: str = "json") -> str:
        """Render a consistent log prefix (json/markdown/jsonl)."""
        return self.runtime.sessions.export(session_id, format=format)

    def read_logs(
        self,
        *,
        session_id: str | None = None,
        daemon_cursor: str | None = None,
        session_cursor: int | None = None,
        limit: int = 50,
        daemon_diagnostics: Any | None = None,
    ) -> dict[str, Any]:
        """Read independent daemon/session diagnostic pages without mutations."""
        limit, session_cursor = validate_logs_read(limit, session_cursor)
        daemon_cursor = validate_daemon_cursor(daemon_cursor)
        daemon = (
            daemon_diagnostics.read(daemon_cursor, limit)
            if daemon_diagnostics is not None
            else _empty_log_page(daemon_cursor)
        )
        if session_id is None:
            session_page = _empty_log_page(
                session_cursor if session_cursor is not None else 0
            )
        else:
            if not isinstance(session_id, str):
                raise ValueError("session must be a string")
            # SessionManager validates the identifier and existence. No client
            # value is ever combined with a filesystem path in the facade.
            sessions = getattr(self.runtime, "sessions", None)
            opener = getattr(sessions, "open", None)
            if not callable(opener):
                raise ValueError("session manager is unavailable")
            from ..session.ids import validate_session_id

            session_id = validate_session_id(session_id)
            if not sessions.store.exists(session_id):
                raise ValueError(f"Session {session_id!r} does not exist")
            handle = getattr(sessions, "_live_handle", lambda _sid: None)(session_id)
            records, clipped, latest_seq = session_records(
                handle, store=sessions.store, session_id=session_id
            )
            session_page = read_session_page(
                records, cursor=session_cursor, limit=limit, latest_seq=latest_seq
            )
            if clipped and session_cursor is None:
                session_page["truncated"] = True
        return {"daemon": daemon, "session": session_page}

    def list_trashed(self) -> list[TrashRecord]:
        return list(self.runtime.sessions.list_trashed())

    # -- turns and the input queue ----------------------------------------

    def session_activity(self, session_id: str) -> p.SessionActivityResult:
        """What keeps ``session_id`` busy after its turn ended (one-shot quiescence wait)."""
        return session_activity(
            session_id, self._session(session_id, create=False, recover=False)
        )

    async def start_turn(self, session_id: str, content: Any, *, client_request_id: str = "") -> str:
        """Schedule one turn for ``session_id``; return its pre-assigned id."""
        if client_request_id:
            _, turn_id = await self.enqueue(
                session_id, content, mode="queue", client_request_id=client_request_id,
                _operation="start",
            )
            return turn_id
        handle = self._session(session_id)
        turn_id = await self.supervisor.submit(session_id, handle, content)
        return turn_id

    async def enqueue(
        self,
        session_id: str,
        content: Any,
        *,
        mode: str = "steer",
        origin: dict[str, Any] | None = None,
        title_text: str | None = None,
        title_labels: list[str] | None = None,
        client_request_id: str = "",
        _operation: str = "enqueue",
        _digest: str = "",
    ) -> tuple[str, str]:
        """Persist a submission and schedule its consumption at the boundary.

        Returns ``(queued_id, turn_id)``. The submission is durable through the
        session's own ``input.queued`` event; the supervisor only decides *when*
        it runs, so the global cap applies to queued work too. A fresh root
        session's first accepted message also starts its automatic title;
        ``title_text``/``title_labels`` override the text derived from ``content``.
        """
        handle = self._session(session_id)
        if mode not in {"queue", "steer", "interrupt"}:
            raise ValueError("mode must be queue, steer, or interrupt")
        if title_text is None:
            if isinstance(content, str):
                title_text = content
            elif isinstance(content, (list, tuple)):
                title_text = " ".join(
                    block.text for block in content if isinstance(getattr(block, "text", None), str)
                )
            else:
                title_text = ""
        # Decided before anything is logged: the first message is not stored yet.
        title = self.titles.candidate(session_id, title_text, title_labels)
        if client_request_id:
            from ..session.submissions import submission_digest, validate_request_id
            validate_request_id(client_request_id)
            digest = _digest or submission_digest(_operation, content, mode, origin)
            queued_id, turn_id, fresh = handle.admit_submission(
                content, request_id=client_request_id, digest=digest, mode=mode,
                origin=origin,
            )
            if fresh:
                if mode == "interrupt":
                    await self.cancel(session_id, reason="Interrupted by user message", drop_queue=False)
                await self.supervisor.submit(
                    session_id, handle, None, queued_id=queued_id,
                    priority=mode == "interrupt", turn_id=turn_id,
                )
                self.titles.start(session_id, title)
            return queued_id, turn_id
        if mode == "interrupt":
            await self.cancel(session_id, reason="Interrupted by user message", drop_queue=False)
        extra = {"origin": origin} if origin else {}
        queued_id = handle.enqueue(content, mode=mode, **extra)
        turn_id = await self.supervisor.submit(
            session_id, handle, None, queued_id=queued_id, priority=mode == "interrupt"
        )
        self.titles.start(session_id, title)
        return queued_id, turn_id

    def move_queued(self, session_id: str, queued_id: str, offset: int) -> bool:
        """Reorder one pending message; the supervisor follows the durable order."""
        handle = self._session(session_id, create=False, recover=False)
        if not handle.move_queued(queued_id, offset):
            return False
        self.supervisor.reorder(session_id, handle.queued_ids)
        return True

    async def set_queued_mode(self, session_id: str, queued_id: str, mode: str) -> bool:
        """Promote one pending message (``queue -> steer -> interrupt``).

        The session records the upgrade and the new order durably; the
        supervisor then follows that order. An interrupt also cancels the
        running turn without dropping the queue, so the promoted message (now
        at the head) starts the next turn, as a submitted interrupt does.
        """
        if mode not in {"steer", "interrupt"}:
            raise ValueError("mode must be steer or interrupt")
        handle = self._session(session_id, create=False, recover=False)
        if not handle.set_queued_mode(queued_id, mode):
            return False
        self.supervisor.reorder(session_id, handle.queued_ids)
        if mode == "interrupt":
            await self.cancel(session_id, reason="Interrupted by user message", drop_queue=False)
        return True

    def remove_queued(
        self, session_id: str, queued_id: str, *, return_content: bool = False
    ) -> bool | tuple[bool, list[dict[str, Any]]]:
        """Drop one pending message; its parked turn is skipped once the id is gone.

        With ``return_content`` the result is ``(removed, content_blocks)`` and
        the content is read before the drop, so it is the full, uncut message.
        """
        handle = self._session(session_id, create=False, recover=False)
        if not return_content:
            return handle.remove_queued(queued_id)
        content = handle.queued_content(queued_id)
        removed = handle.remove_queued(queued_id)
        blocks = msgspec.to_builtins(content) if removed and content else []
        return removed, blocks

    async def cancel(
        self, session_id: str, *, reason: str | None = None, drop_queue: bool = True
    ) -> tuple[bool, int]:
        """Cancel the active turn and (by default) drop the session queue.

        A Stop (``drop_queue``) also pauses automatic wake-ups from background
        job completions until the next human message; queued completions are kept.
        An interrupt (``drop_queue=False``) is followed by a human message.
        """
        if drop_queue:
            with contextlib.suppress(Exception):
                self._session(session_id, create=False, recover=False).pause_auto_wake("stop")
        return await self.supervisor.cancel(
            session_id, reason=reason, drop_queue=drop_queue
        )

    async def recover_admissions(self) -> list[dict[str, Any]]:
        """Report durable admissions a prior host left pending (read-only)."""
        return await self.supervisor.recover_admissions(self.runtime.sessions)

    async def wait_idle(self, *, timeout: float | None = None) -> None:
        """Wait until every scheduled turn has finished."""
        await self.supervisor.wait_idle(timeout=timeout)

    # -- views -------------------------------------------------------------

    async def subscribe(
        self,
        session_id: str,
        from_seq: int = 0,
        *,
        follow: bool = True,
        client_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """Catch up from ``from_seq`` then follow, registering a view.

        The attachment is registered on first iteration (so the count matches the
        session's own viewer count) and released on close, which also frees any
        approval lease the view still held.
        """
        handle = self._session(session_id)
        attachment = self.presence.attach(session_id, client_id)
        try:
            async for event in handle.subscribe(from_seq, follow=follow):
                yield event
        finally:
            self.presence.detach(attachment)

    def state(self, session_id: str, from_seq: int = 0) -> tuple[ConversationView, int]:
        """Fold the session log through the reducer into a baseline view.

        ``from_seq`` is exclusive: the returned view covers every event after it,
        which is exactly what a late joiner needs before subscribing from
        ``seq + 1``. This is the same pure reducer every surface renders with.
        """
        handle = self._session(session_id, create=False, recover=False)
        events = handle.events
        if from_seq:
            view = initial_state(session_id)
            for event in events:
                if event.seq > from_seq:
                    view = apply(view, event)
            return view, view.last_seq
        # The full fold is O(session) and runs on the host loop, so keep the last
        # one and apply only the new tail. The reducer is pure and views are only
        # read, so sharing is safe. A shrunk or rewritten log fails the check
        # below and is folded from scratch.
        view, count, last = self._state_cache.pop(session_id, (None, 0, None))
        if view is None or count > len(events) or (count and events[count - 1] != last):
            view, count = initial_state(session_id), 0
        for event in events[count:]:
            view = apply(view, event)
        if events:
            self._state_cache[session_id] = (view, len(events), events[-1])
            while len(self._state_cache) > 8:
                self._state_cache.pop(next(iter(self._state_cache)))
        return view, view.last_seq

    def web_snapshot(self, session_id: str, from_seq: int = 0) -> dict[str, Any]:
        """Return a reload-safe browser snapshot with stable transcript IDs.

        The established protocol projection intentionally omits reducer-local
        reconciliation IDs. The browser contract includes them so DOM rows can
        retain identity while events are replayed or a stream is reconnected.
        """
        # A browser snapshot is always complete and current. ``from_seq`` is
        # accepted for call-site symmetry, but cursors belong to subscribe_web.
        view, seq = self.state(session_id)
        return {
            "schema_version": 1,
            "session": session_id,
            "seq": seq,
            "view": _web_view(view),
        }

    async def subscribe_web(
        self,
        session_id: str,
        from_seq: int = 0,
        client_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Follow canonical reducer changes as compact, ordered view patches.

        A caller obtains a snapshot first, then subscribes at snapshot ``seq``.
        The session log closes that race by replaying events after the cursor.
        Event sequence numbers are monotonic but need not be contiguous here:
        message/snapshot records share the session sequence and are not event
        frames. ``Session.subscribe`` already heals dropped event-bus messages
        from the log before yielding the next event.
        """
        handle = self._session(session_id, create=False, recover=False)
        view = initial_state(session_id)
        for event in handle.events:
            if event.seq <= from_seq:
                view = apply(view, event)
        cursor = from_seq
        projected = _web_view(view)
        async for event in self.subscribe(session_id, cursor, client_id=client_id):
            if event.seq <= cursor:
                continue
            previous_view = view
            view = apply(view, event)
            after = _web_view(view, previous_view, projected)
            yield {
                "schema_version": 1,
                "session": session_id,
                "seq": event.seq,
                "ops": _json_patch(projected, after),
            }
            projected = after
            cursor = event.seq

    async def subscribe_workspace(self, interval: float = 0.5) -> AsyncIterator[dict[str, Any]]:
        """Publish workspace session summaries when another client changes them.

        Session logs are independently owned, so the workspace index uses a
        small bounded poll over their summary metadata. The initial snapshot is
        immediate; idle viewers incur no work more often than ``interval``.
        """
        delay = max(0.1, min(float(interval), 10.0))
        revision = 0
        previous: tuple[list[Any], int] | None = None
        while True:
            sessions = [jsonable(_asdict(item)) for item in self.list_sessions()]
            archived_count = archive_summary_count(self.runtime.sessions)
            if previous != (sessions, archived_count):
                revision += 1
                yield {"schema_version": 1, "revision": revision, "sessions": sessions,
                       "archived_count": archived_count}
                previous = (sessions, archived_count)
            await asyncio.sleep(delay)

    def agent_transcript(self, session_id: str, agent_id: str) -> dict[str, Any]:
        """Return one view-safe child transcript from the parent event log."""
        view, _seq = self.state(session_id)
        def locate(conversation: ConversationView):
            candidate = conversation.agents.get(agent_id)
            if candidate is not None:
                return candidate
            for nested in conversation.agents.values():
                found = locate(nested.body)
                if found is not None:
                    return found
            return None

        agent = locate(view)
        if agent is None:
            return {"found": False, "status": "not_found", "view": {}}
        payload = agent.to_dict()
        payload["body"] = agent.body.to_dict()
        context = project_agent_context(agent, self.runtime, self.list_agents())
        return {"found": True, "status": agent.status, "view": payload, "context": context}

    def resolve_permission(
        self,
        session_id: str,
        request_id: str,
        decision: Any,
        *,
        client_id: str | None = None,
    ) -> bool:
        """Answer one approval; the first responder wins (PLAN §14.6)."""
        handle = self._session(session_id)
        if not self.presence.claim(session_id, request_id, client_id):
            return False
        try:
            return bool(handle.resolve_permission(request_id, decision))
        finally:
            # The request is answered (or stale) either way, so the lease has
            # served its purpose; holding it would only block a future request.
            self.presence.release(session_id, request_id, client_id)

    async def answer_question(
        self, session_id: str, answer: str, *, call_id: str = "", question_id: str = ""
    ) -> tuple[bool, str | None]:
        """Answer one agent question; the first valid answer wins.

        A stale id, an ambiguous call id, or another session's question
        returns ``(False, None)``; an answer that fails the question's shape
        returns its reason. Answers never touch permission grants.
        """
        broker = self.runtime.questions
        if not question_id:
            matches = [q for q in broker.pending_for(session_id) if q.call_id == call_id]
            if len(matches) != 1:
                return False, None
            question_id = matches[0].question_id
        try:
            resolved = await broker.resolve(session_id, question_id, answer)
        except QuestionAnswerError as exc:
            return False, str(exc)
        return bool(resolved), None

    # -- extensions / models / agents -------------------------------------

    async def reload_extensions(self, trigger: str = "api") -> Any | None:
        """Run one serialized extension rebuild; return its report."""
        extensions = self.runtime.extensions
        if extensions is None:
            return None
        return await extensions.reload(trigger=trigger)

    def list_extensions(self) -> tuple[dict[str, Any], ...]:
        extensions = self.runtime.extensions
        return tuple(extensions.list_extensions()) if extensions is not None else ()

    def validate_extensions(self, target: str | None = None) -> dict[str, Any]:
        """Quarantine-check candidate files without swapping the manifest.

        Returns the manager's generation, an overall verdict, how many files
        were checked, and one sanitized row per candidate. A runtime without an
        extension manager validates nothing and reports that plainly.
        """
        extensions = self.runtime.extensions
        if extensions is None:
            return {
                "generation": 0,
                "valid": True,
                "checked": 0,
                "results": [],
            }
        report = extensions.validate(target=target)
        rows = list(getattr(report, "results", ()) or ())
        return {
            "generation": getattr(extensions, "generation", 0),
            "valid": bool(getattr(report, "valid", True)),
            "checked": len(rows),
            "results": [_asdict(row) for row in rows],
        }

    async def trash_extension(
        self, target: str, *, reason: str = "", force: bool = False
    ) -> Any:
        """Safely trash one managed extension file, then rebuild the manifest.

        Delegates the path scoping, atomic move/rollback, and pin-aware reload to
        the extension manager; a runtime without an extension manager (or one
        that cannot trash) is refused rather than silently no-op'd.
        """
        extensions = self.runtime.extensions
        if extensions is None:
            raise ExtensionTrashError("extension manager is disabled")
        trash = getattr(extensions, "trash", None)
        if not callable(trash):
            raise ExtensionTrashError("extension manager does not support trash")
        return await trash(target, reason=reason, force=force)

    def doctor(self, *, explain_reload: bool = False) -> dict[str, Any]:
        """A redacted health report over config, providers, registry, extensions.

        Counters and descriptors only; never a credential, endpoint userinfo, or
        environment value. ``explain_reload`` adds the hot-vs-restart boundary.

        ``registry_mismatches`` aggregates the durable catalogue defects the loop
        records when a provider rejects a claimed capability (PLAN §15.5). The
        scan is bounded and best-effort: a bounded set of session record tails
        is read, an inaccessible/corrupt session is skipped, and no
        session handle is opened. Only counts, provider/model/reason tallies, and
        bounded samples cross the boundary; the raw provider ``detail`` does not.
        """
        report = doctor_report(
            self.runtime,
            list_sessions=self.list_sessions,
            list_extensions=self.list_extensions,
            explain_reload=explain_reload,
        )
        report["daemon"] = dict(self.daemon_info)
        report["voice"] = doctor_voice(self.runtime)
        return report

    def update_status(self, *, announce: bool = False) -> dict[str, Any]:
        """The cached "newer release available" answer; never raises.

        ``announce`` claims the once-per-release toast (``claim_announcement``).
        """
        try:
            status = update_status(config_enabled=self.runtime.update_check_enabled())
            if announce:
                status["announce"] = claim_announcement(status.get("available"))
            return status
        except Exception:  # noqa: BLE001 - an advisory notice must not break a surface
            return {"enabled": False}

    async def refresh_models(self) -> Any | None:
        """Force a catalogue acquisition; return the registry status."""
        return await self.runtime.refresh_models()

    def list_models(
        self,
        *,
        provider: str | None = None,
        tier: str | None = None,
        selectable_only: bool = False,
        search: str | None = None,
        scope: str = "all",
    ) -> list[Any]:
        if scope not in {"main", "subagents", "all"}:
            raise ValueError("model scope must be 'main', 'subagents', or 'all'")
        registry = self.runtime.registry
        if registry is None:
            return []
        # Resolve configured aliases for every scope: registry kind may be
        # absent for a catalogued provider alias (e.g. studio -> agy).
        config = self.runtime._load_config()
        claude_worker_backend = False
        antigravity_sections: set[str] = set()
        configured_aliases: dict[str, str | None] = {}
        if config is not None:
            models = getattr(getattr(config, "v2", None), "models", None)
            claude_worker_backend = getattr(models, "claude_code_subagent_backend", "worker") == "worker"
            antigravity_sections = {
                name for name, section in (getattr(getattr(config, "v2", None), "providers", {}) or {}).items()
                if self.runtime._adapter_kind(name, section) == "antigravity"
            }
            configured_aliases = {
                name: self.runtime._adapter_kind(name, section)
                for name, section in (getattr(getattr(config, "v2", None), "providers", {}) or {}).items()
            }
        rows = list(
            registry.list(
                provider=provider,
                tier=tier,
                selectable_only=selectable_only,
                search=search,
            )
        )
        if scope == "subagents" and selectable_only:
            known = {(row.provider, row.id) for row in rows}
            for row in registry.list(
                provider=provider, tier=tier, selectable_only=False, search=search,
            ):
                kind = getattr(registry.provider_status(row.provider), "kind", None)
                kind = kind or configured_aliases.get(row.provider)
                if (kind == "antigravity" or row.provider in antigravity_sections or
                        (claude_worker_backend and (row.provider == "claude-code" or kind == "claude-code"))):
                    if (row.provider, row.id) not in known:
                        rows.append(row)
                        known.add((row.provider, row.id))
        if scope != "all":
            filtered = []
            for row in rows:
                kind = getattr(registry.provider_status(row.provider), "kind", None)
                kind = kind or configured_aliases.get(row.provider)
                is_agi = kind == "antigravity"
                is_claude_code = row.provider == "claude-code" or kind == "claude-code"
                if scope == "main" and is_agi:
                    continue
                if scope == "main" and is_claude_code and not getattr(
                    self.runtime, "claude_code_main_enabled", lambda: False
                )():
                    continue
                if scope == "main" and is_claude_code and claude_worker_backend:
                    continue
                if scope == "subagents":
                    # Ordinary provider rows remain valid child candidates.
                    # Permit catalogue-only external routes in addition.
                    if is_agi and row.provider not in antigravity_sections and kind != "antigravity":
                        continue
                filtered.append(row)
            rows = filtered
        return rows

    def _model_row(self, model: Any, *, selectable_only: bool) -> dict[str, Any]:
        """Project one catalogue row and attach runtime-route effort choices."""
        row = _asdict(model)
        registry = getattr(self.runtime, "_registry", None)
        status = registry.provider_status(model.provider) if registry is not None else None
        config = self.runtime._load_config()
        section = (getattr(getattr(config, "v2", None), "providers", {}) or {}).get(model.provider)
        kind = getattr(status, "kind", None) or self.runtime._adapter_kind(model.provider, section)
        models = getattr(getattr(config, "v2", None), "models", None)
        if kind == "antigravity":
            row["scope"] = "subagents"
            row["name"] = f"{row.get('name') or model.id} — Subagents only"
        elif kind == "claude-code" and (
            getattr(models, "claude_code_subagent_backend", "worker") == "worker"
            or not getattr(models, "claude_code_main_enabled", False)
        ):
            row["scope"] = "subagents"
        if selectable_only:
            query = getattr(self.runtime, "candidate_supported_efforts", None)
            efforts: object = ()
            if callable(query):
                try:
                    efforts = query(row.get("provider"), row.get("id"))
                except Exception:  # noqa: BLE001 - metadata cannot block model listing
                    efforts = ()
            if not isinstance(efforts, (tuple, list)):
                efforts = ()
            row["supported_efforts"] = [
                value
                for value in efforts
                if isinstance(value, str) and value in REASONING_EFFORTS
            ]
            recall = getattr(self.runtime, "remembered_model_effort", None)
            remembered = recall(row.get("provider"), row.get("id")) if callable(recall) else None
            if remembered is not None:
                row["remembered_effort"] = remembered[0] if remembered[0] in row["supported_efforts"] else None
        return row

    def model_info(self, ref: str) -> dict[str, Any] | None:
        """Resolve one model reference to its descriptive row, or ``None``.

        Never raises for an unknown reference and never carries a credential or
        endpoint: only the registry's descriptive fields cross the wire.
        """
        registry = self.runtime.registry
        if registry is None:
            return None
        info = registry.get(ref)
        return _asdict(info) if info is not None else None

    def _tiers_result(self, *, restart_required: bool = False) -> p.ModelTiersResult:
        """The tier table plus the Settings rows (refs, source, resolved model)."""
        tiers = self.model_tiers()
        rows = model_settings.tier_rows(self.runtime) if tiers["order"] else {"tiers": [], "max_tier": ""}
        return p.ModelTiersResult(
            order=tiers["order"],
            default=tiers["default"],
            builtin=tiers["builtin"],
            overrides=tiers["overrides"],
            tiers=rows["tiers"],
            max_tier=rows["max_tier"],
            restart_required=restart_required,
        )

    def model_tiers(self) -> dict[str, Any]:
        """The tier table's ordering, default, curated map, and user overrides."""
        tiers = self.runtime.tiers
        if tiers is None:
            return {"order": [], "default": "", "builtin": {}, "overrides": {}}
        return {
            "order": list(tiers.order),
            "default": tiers.default,
            "builtin": dict(tiers.builtin),
            "overrides": dict(tiers.overrides),
        }

    def select_model(self, session: str, ref: str) -> ModelSelection:
        """Validate and persist a per-session model selection (PLAN §14.11).

        Delegates resolution to the runtime, which uses the same router/tier
        rules as a configured default and refuses an unknown provider, a
        malformed reference, or a tier with no runnable model. The selection is
        durable and applies from the session's next turn; a turn already running
        is untouched.
        """
        return self.runtime.select_session_model(session, ref)

    def _fallback_chain(self, selection: ModelSelection | None = None) -> list[str]:
        """The configured fallback refs, redacted, deduped, minus the selection.

        Only descriptive reference strings cross the wire: each is sanitized and
        secret-redacted, duplicates collapse (first occurrence wins), and a
        reference equal to the selected one is dropped -- falling back to the
        model already in use is not a fallback. The list is empty when no
        fallback is configured or the router exposes none.
        """
        router = getattr(self.runtime, "router", None)
        chain = getattr(router, "fallback", None)
        if callable(chain):  # pragma: no cover - a callable fallback seam
            chain = chain()
        excluded: set[str] = set()
        if selection is not None:
            excluded.add(selection.reference)
            excluded.add(f"{selection.provider}/{selection.model}")
        seen: set[str] = set()
        out: list[str] = []
        for ref in chain or ():
            text = redact_secrets(sanitize_text(str(ref), limit=200))
            if not text or text in seen or text in excluded:
                continue
            seen.add(text)
            out.append(text)
        return out

    def list_agents(self) -> list[dict[str, Any]]:
        """A sanitized, transport-neutral index of discovered subagents."""
        agents = self.runtime.agents
        if agents is None:
            return []
        refresh = getattr(agents, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:  # noqa: BLE001, S110 - metadata must not block the UI
                pass
        entries = agents.index
        rows: list[dict[str, Any]] = []
        for entry in entries:
            name = self._agent_text(entry.name, limit=80)
            if not name:
                continue
            description = self._agent_text(entry.description, limit=500)
            source = self._agent_text(
                getattr(entry.source, "value", entry.source), limit=40
            )
            contexts = ["subagent"]
            get_agent = getattr(agents, "get", None)
            agent = get_agent(entry.name) if callable(get_agent) else None
            raw_contexts = getattr(agent, "contexts", None)
            if isinstance(raw_contexts, (list, tuple)):
                contexts = [
                    value
                    for value in (self._agent_text(item, limit=40) for item in raw_contexts)
                    if value
                ]
            rows.append(
                {
                    "name": name,
                    "description": description,
                    "source": source,
                    "provider": self._agent_text(
                        getattr(entry, "provider", None), limit=80
                    ) or None,
                    "model": self._agent_text(
                        getattr(entry, "model", None), limit=160
                    ) or None,
                    "reasoning_effort": self._agent_effort(
                        getattr(entry, "reasoning_effort", None)
                    ),
                    "color": self._agent_color(getattr(entry, "color", None)),
                    "read_only": bool(entry.read_only),
                    "contexts": contexts or ["subagent"],
                }
            )
        return rows

    def _refresh_agents(self) -> Any:
        agents = self.runtime.agents
        if agents is not None:
            agents.refresh()
        return agents

    def current_agent(self, session_id: str) -> tuple[str, str]:
        handle = self._session(session_id)
        return self.runtime.effective_session_agent(handle)

    def current_agent_metadata(self, session_id: str) -> dict[str, Any]:
        """Return sanitized metadata for the session's next root turn."""
        handle = self._session(session_id)
        name, source = self.runtime.effective_session_agent(handle)
        context = getattr(self.runtime, "context", None)
        effective_config = getattr(context, "effective_config", None)
        try:
            config = effective_config() if callable(effective_config) else None
        except Exception:  # noqa: BLE001 - metadata cannot block the UI
            config = None
        agent = self._root_agent(name)
        query = getattr(self.runtime, "root_route_metadata", None)
        route: Mapping[str, Any] = {}
        try:
            value = query(handle) if callable(query) else {}
        except Exception:  # noqa: BLE001 - descriptive metadata must not block UI
            value = {}
        if isinstance(value, Mapping):
            route = value
        color = self._agent_color(getattr(agent, "color", None))
        effort_metadata = self._root_reasoning_effort_metadata(handle)
        v2 = getattr(config, "v2", None)
        params = getattr(getattr(v2, "model", None), "params", None)
        budget = getattr(params, "thinking_budget", None)
        return {
            "name": self._agent_text(name, limit=80) or "orchestrator",
            "source": self._agent_text(source, limit=40) or "default",
            "color": color,
            "provider": self._agent_text(route.get("provider"), limit=80) or None,
            "model": self._agent_text(route.get("model"), limit=160) or None,
            "reasoning_effort": effort_metadata["effective_effort"],
            "supported_levels": effort_metadata["supported_levels"],
            "stored_override": effort_metadata["stored_override"],
            "reasoning_effort_source": effort_metadata["source"],
            "thinking_budget": budget if type(budget) is int else None,
        }

    def _root_reasoning_effort_metadata(self, handle: Any) -> dict[str, Any]:
        """Use runtime-owned capability metadata, with safe fake-runtime defaults."""
        query = getattr(self.runtime, "root_reasoning_effort_metadata", None)
        try:
            value = query(handle) if callable(query) else {}
        except Exception:  # noqa: BLE001 - descriptive metadata must not block UI
            value = {}
        if not isinstance(value, Mapping):
            value = {}
        levels = value.get("supported_levels", ())
        if not isinstance(levels, (tuple, list)):
            levels = ()
        supported = [level for level in levels if isinstance(level, str) and level in REASONING_EFFORTS]
        stored = value.get("stored_override")
        if not isinstance(stored, str) or stored not in REASONING_EFFORTS:
            stored = None
        effective = value.get("effective_effort")
        if not isinstance(effective, str) or effective not in supported:
            effective = None
        source = value.get("source")
        if source not in {"session", "agent"}:
            source = None
        return {
            "supported_levels": supported,
            "stored_override": stored,
            "effective_effort": effective,
            "source": source,
        }

    @staticmethod
    def _agent_text(value: object, *, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        return redact_secrets(sanitize_text(value, limit=limit)).strip()

    @staticmethod
    def _agent_effort(value: object) -> str | None:
        if not isinstance(value, str) or value not in REASONING_EFFORTS:
            return None
        return value

    @staticmethod
    def _agent_color(value: object) -> str | None:
        if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
            return None
        if any(char not in "0123456789abcdefABCDEF" for char in value[1:]):
            return None
        return value.upper()

    def _root_agent(self, name: str) -> Any | None:
        agents = getattr(self.runtime, "agents", None)
        refresh = getattr(agents, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:  # noqa: BLE001, S110 - unavailable agent has no metadata
                pass
        resolve = getattr(agents, "resolve", None)
        if callable(resolve):
            try:
                return resolve(name, context="root")
            except Exception:  # noqa: BLE001 - unavailable agent has no metadata
                return None
        get_agent = getattr(agents, "get", None)
        return get_agent(name) if callable(get_agent) else None

    def select_agent(self, session_id: str, name: str | None) -> tuple[str, str]:
        return self.runtime.select_session_agent(session_id, name)

    async def list_tools(self) -> list[dict[str, Any]]:
        """The model-facing tool catalog for the current config and manifest."""
        lister = getattr(self.runtime, "list_tools", None)
        if not callable(lister):
            return []
        return list(await lister())

    async def inspect_context(self, session_id: str) -> dict[str, Any]:
        """Read a next-turn context preview without changing session history."""
        session = self._session(session_id, create=False, recover=False)
        inspector = getattr(self.runtime, "inspect_context", None)
        if not callable(inspector):
            raise TypeError("runtime does not support context inspection")
        result = await inspector(session)
        if not isinstance(result, Mapping):
            raise TypeError("runtime returned invalid context inspection")
        return project_context_preview(result)

    def _skill_inspect(self, command: p.SkillInspect) -> p.SkillInspectResult:
        """Read only leased refresh-time bytes, never recover or invoke a skill."""
        from ..errors import SessionError
        from ..util import redact_secrets

        def display(value: Any) -> str:
            return redact_secrets(str(value))

        base = {"session": display(command.session), "name": display(command.name)}

        def error(message: str) -> p.SkillInspectResult:
            return p.SkillInspectResult(**base, status="error", error=message)

        if not 0 <= command.max_body_bytes <= 262_144:
            return error("max_body_bytes must be between 0 and 262144")
        try:
            # Read the log directly: opening a handle would unarchive it.
            from ..session.ids import validate_session_id

            session_id = validate_session_id(command.session)
            if not self.runtime.sessions.store.exists(session_id):
                return error("session not found")
            read = self.runtime.sessions.store.read(session_id)
            disabled: set[str] = set()
            for event in read.events():
                if event.type != "context.extension_selected" or not isinstance(event.data, Mapping):
                    continue
                name = event.data.get("name")
                if event.data.get("category") == "skills" and isinstance(name, str):
                    if event.data.get("enabled") is False:
                        disabled.add(name.casefold())
                    elif event.data.get("enabled") is True:
                        disabled.discard(name.casefold())
        except SessionError:
            return error("session not found")
        store = self.runtime.manifest_ref
        if store is None:
            return error("skill manifest unavailable")
        lease = store.pin()
        try:
            manifest = lease.manifest
            skill = next((value for name, value in manifest.skills.items()
                          if name.casefold() == command.name.casefold()), None)
            if skill is None:
                return error("skill not found in pinned manifest")
            parsed = getattr(skill, "parsed", None)
            body = getattr(skill, "body", None)
            if parsed is None or body is None or not skill.snapshotted:
                return error("skill source snapshot unavailable")
            provenance = skill.provenance
            text = display(body.decode("utf-8", errors="replace"))
            encoded = text.encode("utf-8")
            bounded = encoded[:command.max_body_bytes].decode("utf-8", errors="ignore")
            return p.SkillInspectResult(
                **{**base, "name": display(skill.name)},
                manifest_generation=manifest.generation,
                enabled=skill.name.casefold() not in disabled,
                scope="project" if "workspace" in str(provenance.tier).lower() else "global",
                origin=display(provenance.relpath),
                metadata={
                    "description": display(skill.description),
                    "allowed_tools": [display(v) for v in skill.allowed_tools],
                    "bundles": [display(v) for v in skill.bundles],
                    "model": display(skill.model) if skill.model is not None else None,
                    "version": display(skill.version),
                    "file_sha256": display(skill.file_sha256),
                },
                frontmatter_text=display(parsed.raw.decode("utf-8", errors="replace")),
                body=bounded,
                body_bytes=len(body),
                truncated=len(encoded) > command.max_body_bytes,
            )
        finally:
            lease.release()

    # -- health / shutdown -------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Daemon-level liveness: counters only, never a credential."""
        sessions = self.runtime.sessions
        try:
            total = len(sessions.list())
        except Exception:  # noqa: BLE001 - health must never raise
            total = 0
        jobs_total, jobs = 0, []
        probe = getattr(self.runtime, "persistent_shell_jobs", None)
        if callable(probe):
            try:
                jobs_total, jobs = probe()
            except Exception:  # noqa: BLE001 - health must never raise
                jobs_total, jobs = 0, []
        return {
            "ok": not self._closed,
            "version": p.PROTOCOL_VERSION,
            "sessions": total,
            "running": self.supervisor.running,
            "queued": self.supervisor.queued,
            "max_concurrent": self.supervisor.max_concurrent,
            "viewers": self.presence.total_viewers(),
            "uptime": max(0.0, time.time() - self._started),
            "dev": dev_enabled(),
            "persistent_jobs_total": jobs_total,
            "persistent_jobs": jobs,
        }

    def start_usage_polling(self) -> None:
        """Refresh plan usage in the background every ``[models] usage_refresh_seconds`` (the daemon calls this once)."""
        if not self._closed:
            self.usage.start()

    async def shutdown(self, reason: str = "") -> bool:
        """Stop scheduling, close every session, and (if owned) the runtime."""
        if self._closed:
            return False
        self._closed = True
        # End a GPT Live call first: its ``live.ended`` must be persisted while
        # the session handles are still writable.
        live = getattr(self.runtime, "live", None)
        if live is not None:
            await live.shutdown()
        await self.usage.aclose()  # the periodic poll and any in-flight fetch
        await self.titles.aclose()
        await self.shells.aclose()
        for task in list(self._wake_tasks):
            task.cancel()
        await asyncio.gather(*self._wake_tasks, return_exceptions=True)
        await self.supervisor.aclose()
        closer = getattr(self.runtime.sessions, "aclose_all", None)
        if callable(closer):
            await closer()
        if self._owns_runtime:
            await self.runtime.aclose()
        return True

    def focused_clients(self, session_id: str) -> tuple[str, ...]:
        """Bounded ephemeral foreground hints, expiring after five minutes."""
        now = time.monotonic()
        self._foreground = {k: v for k, v in self._foreground.items() if now - v[1] < 300}
        return tuple(k for k, (sid, _) in self._foreground.items() if sid == session_id)

    def report_focus(self, client_id: str, session: str | None) -> p.SessionFocusResult:
        if not client_id or len(client_id) > 128 or not client_id.isascii() or any(ord(c) < 33 for c in client_id):
            raise ValueError("Invalid focus client id")
        self.focused_clients("")
        if not session:
            self._foreground.pop(client_id, None)
        else:
            if not self.runtime.sessions.exists(session):
                raise ValueError("Unknown foreground session")
            if len(self._foreground) >= 64 and client_id not in self._foreground:
                oldest = min(self._foreground, key=lambda k: self._foreground[k][1])
                del self._foreground[oldest]
            self._foreground[client_id] = (session, time.monotonic())
        return p.SessionFocusResult(session=session, client_id=client_id)

    # -- wire dispatch -----------------------------------------------------

    async def handle(self, command: p.Command) -> p.Result:
        from ..resources import ResourceScope, current_scope
        token = None
        if self.resource_limits is not None:
            session = getattr(command, "session", "") or getattr(command, "session_id", "")
            token = current_scope.set(ResourceScope(self.resource_limits, self.project_key, session,
                                                   getattr(self.runtime, "_environ", {})))
        try:
            git_commands = (p.GitStatus, p.GitDiff, p.WorktreeList, p.WorktreeInspect,
                            p.WorktreeReview, p.WorktreeAcknowledge, p.WorktreeIntegrate,
                            p.WorktreeDiscard, p.SessionWorktreeStatus, p.SessionTurnDiff, p.SessionTurnRevert, p.SessionIntegrate)
            if isinstance(command, git_commands):
                from ..resources import process_slot
                async with process_slot():
                    return await self._handle(command)
            return await self._handle(command)
        finally:
            if token is not None:
                current_scope.reset(token)

    async def _handle(self, command: p.Command) -> p.Result:
        """Dispatch one protocol command, converting failures to a redacted error."""
        try:
            if self.resource_limits is not None and not Path(self.runtime.workspace).is_dir():
                readonly = (p.SessionState, p.SessionPreview, p.SessionExport, p.SessionTree,
                            p.SessionInspect, p.SessionActivity, p.SessionList, p.SessionListArchived,
                            p.SessionSearch, p.Doctor, p.LogsRead, p.Health)
                if not isinstance(command, readonly):
                    raise ValueError("Project root is missing; sessions are read-only")
            if isinstance(command, p.SessionFocus):
                return self.report_focus(command.client_id, command.session)
            return await self._dispatch(command)
        except Exception as exc:  # noqa: BLE001 - the wire never raises a traceback
            message = redact_secrets(str(exc))
            if isinstance(command, p.SettingsMcpAdd) and isinstance(command.entry, dict):
                from ..host_support.settings_inventory import _draft_secrets
                from ..mcp.client import redact_secrets as redact_draft

                message = redact_draft(message, _draft_secrets(command.entry), min_length=1)
            return p.ErrorResult(
                kind=type(exc).__name__, message=message
            )

    async def _dispatch(self, command: p.Command) -> p.Result:
        # Resolve only at the user-facing boundary. Authorization and lifecycle
        # checks below continue to operate on canonical ids, not capabilities.
        target_commands = (p.SessionTree, p.SessionInspect, p.SessionFork,
                           p.SessionDelete, p.SessionExport,
                           p.SessionArchive, p.SessionUnarchive)
        target_commands = (*target_commands, p.SessionWorktreeSet, p.SessionPromote,
                           p.SessionDetach, p.SessionWorktreeStatus, p.SessionWorktreeTrust,
                           p.SessionTurnDiff, p.SessionTurnRevert, p.SessionIntegrate)
        if isinstance(command, target_commands) and hasattr(self.runtime.sessions, "resolve"):
            namespace = getattr(command, "namespace", "main")
            if isinstance(command, p.SessionFork) and command.new_id == MANAGER_SESSION_ID:
                raise ValueError(f"session id {MANAGER_SESSION_ID!r} is reserved for the manager")
            try:
                resolved = self.runtime.sessions.resolve(command.session, namespace=namespace)
            except SessionError:
                # Unresolvable (unknown id, another project, a name that would address the
                # manager): leave the ref untouched so each handler keeps its own refusal.
                pass
            else:
                # Handlers take the stored id (a child's logical id is not a valid session id).
                command = msgspec.structs.replace(command, session=resolved.get("id") or resolved["logical_id"])
        if result := await dispatch_voice(command, self.runtime):
            return result
        # Import lazily: host_support.live also imports the host protocol, whose
        # package initializes this facade. Keep either import order valid.
        from ..host_support.live import dispatch_live

        if result := await dispatch_live(command, self):
            return result
        if isinstance(command, p.ManagerOpen):
            summary, created = self.open_manager()
            return p.ManagerOpenResult(session=summary, created=created)
        if isinstance(command, p.ManagerState):
            return await self.manager_state()
        if isinstance(command, (p.SpeechStatus, p.SpeechPrepare)):
            from ..host_support.speech import dispatch_speech  # lazy: it imports host.protocol

            return await dispatch_speech(command, self.runtime, None)
        if isinstance(command, p.SpeakStop):
            from ..host_support.speech import dispatch_speech  # lazy: it imports host.protocol

            return await dispatch_speech(command, self.runtime, None)
        if isinstance(command, p.Speak):
            # Only the latest completed answer of this session is spoken; the
            # worker is an isolated subprocess (host_support/speech.py).
            from ..host_support.speech import dispatch_speech  # lazy: it imports host.protocol

            view, _ = self.state(command.session_id)
            return await dispatch_speech(command, self.runtime, view)
        if result := await dispatch_settings(command, self.runtime):
            return result
        if isinstance(command, p.SkillsInstall):
            from ..host_support.skills_install import dispatch_skills_install  # lazy: it imports host.protocol

            return await dispatch_skills_install(command, self.runtime)
        if result := await dispatch_mock(command, self):
            return result
        if result := await dispatch_providers(command, self.runtime, lambda: not self.supervisor.running):
            if isinstance(result, p.ProvidersStatusResult):
                return p.ProvidersStatusResult(providers=_with_antigravity_row(result.providers))
            if isinstance(result, p.ProvidersUsageResult):
                # The panel's fresh numbers are the routing gate's numbers too.
                self.usage.ingest({"providers": result.providers, "fetched_at": result.fetched_at})
            return result
        if isinstance(command, (
            p.SessionArchive, p.SessionUnarchive, p.SessionListArchived,
            p.SessionPreview, p.SessionSearch,
        )):
            return dispatch_archive_command(command, self.runtime.sessions, self.supervisor)
        if isinstance(command, p.ProjectSessionsList):
            if type(command.limit) is not int:
                raise ValueError("project session limit must be an integer")
            if not isinstance(command.query, str) or len(command.query) > 256:
                raise ValueError("project session query must be at most 256 characters")
            limit = max(1, min(command.limit, 1000))
            if type(command.archived) is not int:
                raise ValueError("archived session count must be an integer")
            current = str(self.runtime.workspace)
            db = self.runtime.sessions.store.db
            rows = db.project_sessions(limit=limit + 1, query=command.query, hide_placeholders=True)
            archived_rows = []
            if command.archived > 0:
                # A search also matches archived sessions, so they are searchable from the picker.
                archived_rows = db.archived_project_sessions(
                    limit=min(command.archived, 100), query=command.query, hide_placeholders=True)
            if type(command.projects) is not int:
                raise ValueError("project count must be an integer")
            registered = []
            if command.projects > 0:
                wanted = min(command.projects, 500)
                known = self.runtime.sessions.store.db.registered_projects(limit=wanted)
                # The active workspace is listed first even when nothing is registered for it.
                registered = [next((r for r in known if r["workspace"] == current),
                                   {"project_id": "", "workspace": current, "updated": 0.0})]
                registered += [r for r in known if r["workspace"] != current][:wanted - 1]
            # A historical root that is gone from disk is flagged (and not inspected) so clients omit it.
            present = {r["workspace"]: r["workspace"] == current or os.path.isdir(r["workspace"]) for r in registered}
            # Sessions recorded under a root that is gone from disk cannot be opened: drop them (the
            # current workspace stays) so a deleted temp checkout never reappears as a project.
            for row in (*rows, *archived_rows):
                present.setdefault(row["workspace"], row["workspace"] == current or os.path.isdir(row["workspace"]))
            truncated = len(rows) > limit
            rows = [r for r in rows if present[r["workspace"]]]
            archived_rows = [r for r in archived_rows if present[r["workspace"]]]
            # Only returned rows need live metadata or workspace inspection.
            shown = [*rows[:limit], *archived_rows]
            wanted_roots = {*(row["workspace"] for row in shown), *(r["workspace"] for r in registered)}
            for workspace in wanted_roots:
                present.setdefault(workspace, workspace == current or os.path.isdir(workspace))
            trees = {workspace: _worktree_of(workspace) if present[workspace] else ("", "")
                     for workspace in wanted_roots}
            sessions = []
            for row in shown:
                pending = {}
                if row["workspace"] == current:
                    summary = self.runtime.sessions._summary_from_row(row)
                    if summary.state == "awaiting_permission":
                        from ..host_support.session_preview import pending_request
                        try:
                            pending = pending_request(self.state(summary.id)[0])
                        except Exception:  # a dashboard poll never fails on one session
                            pending = {}
                else:
                    summary = SessionSummary(
                        id=row["id"], name=row.get("name"), title=row["title"], last_activity=row["last_activity"],
                        last_seq=row["last_seq"], completion_seq=row["completion_seq"],
                        message_count=row["message_count"],
                        created_at=row["created_at"], parent_id=row["parent_id"], fork_seq=row["fork_seq"],
                        last_user_at=row["last_user_at"],
                    )
                repo, worktree = trees[row["workspace"]]
                sessions.append(p.ProjectSession(
                    workspace=row["workspace"], project_id=row["project_id"], session=summary,
                    repo=repo, worktree=worktree,
                    last_user=str(row.get("last_user") or "")[:240],
                    last_agent=str(row.get("last_agent") or "")[:240],
                    archived=row.get("archived_at") is not None, pending=pending,
                ))
            projects = [p.ProjectEntry(workspace=r["workspace"], project_id=r["project_id"],
                                       repo=trees.get(r["workspace"], ("", ""))[0],
                                       worktree=trees.get(r["workspace"], ("", ""))[1],
                                       updated=float(r.get("updated") or 0.0), exists=present[r["workspace"]])
                        for r in registered]
            return p.ProjectSessionsListResult(workspace=current, sessions=sessions, truncated=truncated or len(rows) > limit,
                                               projects=projects)
        if isinstance(command, p.ProjectSessionOpen):
            raise ValueError("Project navigation requires a daemon connection")
        if isinstance(command, p.SessionResolve):
            return p.SessionResolveResult(resolution=self.runtime.sessions.resolve(
                command.ref, namespace=command.namespace, include_retired=command.include_retired
            ))
        if isinstance(command, p.SessionTree):
            # Fixed, not ``agents.max_depth``: sessions recorded before spawning
            # became root-only may nest deeper and must still render.
            return p.SessionTreeResult(
                session=command.session,
                tree=self.runtime.sessions.tree(
                    command.session, namespace=command.namespace, max_depth=8,
                ),
            )
        if isinstance(command, p.SessionInspect):
            from ..host_support.session_inspect import inspect_session

            return p.SessionInspectResult(
                session=command.session, namespace=command.namespace,
                **inspect_session(self.runtime.sessions, command.session, command.namespace),
            )
        if isinstance(command, p.SessionHandoff):
            self._refuse_manager(command.session, "manual handoff")
            handle = self._session(command.session, create=False, recover=False)
            result = handle.handoff(summary=command.summary,
                continuation=command.continuation, request_id=command.request_id,
                _scheduled=bool(self.supervisor.running_for(command.session)
                    or self.supervisor.queued_for(command.session)))
            if result["fresh"] and result["queued_id"]:
                await self.supervisor.submit(command.session, handle,
                    queued_id=result["queued_id"], turn_id=result["turn_id"])
            return p.SessionHandoffResult(data=result)
        if isinstance(command, p.SessionHandoffRead):
            from ..host_support.session_handoff import read_handoff
            return p.SessionHandoffResult(data=read_handoff(
                self.runtime.sessions, command.session, command.namespace, command.handoff_id))
        if isinstance(command, p.SessionList):
            return p.SessionListResult(
                sessions=self.list_sessions(include_children=command.include_children),
                archived_count=archive_summary_count(self.runtime.sessions),
            )
        if isinstance(command, p.FileSearch):
            paths = await asyncio.to_thread(
                self.search_files,
                command.query,
                command.limit,
                include_directories=command.include_directories,
                session=command.session,
            )
            return p.FileSearchResult(paths=paths)
        if isinstance(command, p.GitDiff):
            patch, truncated = await git_diff(
                _session_workspace(self.runtime, command.session), staged=command.staged, ref=command.ref
            )
            return p.GitDiffResult(patch=patch, truncated=truncated)
        if isinstance(command, p.DirectoryList):
            path, entries, truncated = await asyncio.to_thread(
                list_directory, self.runtime, command.path, command.limit,
                session=command.session,
            )
            return p.DirectoryListResult(path=path, entries=entries, truncated=truncated)
        if isinstance(command, p.FilePreview):
            return p.FilePreviewResult(
                **await asyncio.to_thread(
                    preview_file, self.runtime, command.path, command.max_bytes,
                    session=command.session,
                )
            )
        if isinstance(command, p.GitStatus):
            return p.GitStatusResult(**await workspace_git_status(
                _session_workspace(self.runtime, command.session)
            ))
        if isinstance(command, p.SessionIntegrate):
            from ..session_integration import integration_preview, integrate_session
            self._refuse_manager(command.session, "session integration")
            controller = self.runtime._session_worktrees
            state = await asyncio.to_thread(integration_preview, controller, command.session)
            if command.confirm_token:
                self._consume_confirmation(command.confirm_token, "session_integrate", command.session, state, {})
                state = await asyncio.to_thread(integrate_session, controller, command.session, state)
            elif state.get("ready"):
                state = {**state, "token": self._confirmation_token("session_integrate", command.session, state, {})}
            return p.SessionWorktreeResult(session=command.session, state=state)
        if isinstance(command, p.SessionTurnDiff):
            from ..worktree_turns import turn_diff
            self._refuse_manager(command.session, "turn diff")
            state = await asyncio.to_thread(turn_diff, self.runtime._session_worktrees,
                command.session, command.seq, path=command.path, max_bytes=command.max_bytes)
            return p.SessionWorktreeResult(session=command.session, state=state)
        if isinstance(command, p.SessionTurnRevert):
            from ..worktree_turns import revert_preview, revert_turn
            self._refuse_manager(command.session, "turn restore")
            controller = self.runtime._session_worktrees
            state = await asyncio.to_thread(revert_preview, controller, command.session,
                command.seq, command.include_later)
            options = {"seq": command.seq, "include_later": command.include_later}
            if command.confirm_token:
                self._consume_confirmation(command.confirm_token, "turn_revert", command.session, state, options)
                state = await asyncio.to_thread(revert_turn, controller, command.session, state)
            else:
                state = {**state, "token": self._confirmation_token("turn_revert", command.session, state, options)}
            return p.SessionWorktreeResult(session=command.session, state=state)
        if isinstance(command, p.SessionWorktreeSet):
            self._refuse_manager(command.session, "session worktree selection")
            state = await self._invoke_runtime_worktree(
                "set_session_worktree", command.session, command.enabled, base=command.base
            )
            return p.SessionWorktreeResult(session=command.session, state=_worktree_state_dict(state))
        if isinstance(command, p.SessionPromote):
            self._refuse_manager(command.session, "session promotion")
            state = await self._invoke_runtime_worktree(
                "promote_session", command.session, reason=command.reason,
                continuation=command.continuation,
            )
            return p.SessionWorktreeResult(session=command.session, state=_worktree_state_dict(state))
        if isinstance(command, p.SessionDetach):
            self._refuse_manager(command.session, "session detach")
            state = await self._invoke_runtime_worktree("detach_session_worktree", command.session)
            return p.SessionWorktreeResult(session=command.session, state=_worktree_state_dict(state))
        if isinstance(command, p.SessionWorktreeStatus):
            self._refuse_manager(command.session, "session worktree status")
            state = await self._invoke_runtime_worktree("session_worktree_status", command.session)
            return p.SessionWorktreeResult(session=command.session, state=_worktree_state_dict(state))
        if isinstance(command, p.SessionWorktreeTrust):
            self._refuse_manager(command.session, "worktree setup trust")
            state = await self._invoke_runtime_worktree(
                "approve_worktree_setup", command.session, command.decision,
                digest=command.digest,
            )
            return p.SessionWorktreeResult(session=command.session, state=_worktree_state_dict(state))
        if isinstance(command, p.WorktreeList):
            rows, has_more = await asyncio.to_thread(self.list_worktrees)
            return p.WorktreeListResult(worktrees=rows, has_more=has_more)
        if isinstance(command, p.WorktreeInspect):
            record = await asyncio.to_thread(
                self.inspect_worktree, command.child_id
            )
            return p.WorktreeInspectResult(
                child_id=_worktree_text(command.child_id, 256),
                status=record["lifecycle"],
                record=record,
            )
        if isinstance(command, p.WorktreeReview):
            result = await asyncio.to_thread(
                self.review_worktree,
                command.child_id,
                review_id=command.review_id,
                cursor=command.cursor,
                limit=command.limit,
            )
            return p.WorktreeReviewResult(child_id=_worktree_text(command.child_id, 256), **result)
        if isinstance(command, p.WorktreeAcknowledge):
            result = await asyncio.to_thread(
                self.acknowledge_worktree,
                command.child_id,
                command.review_id,
                command.digest,
            )
            return p.WorktreeAcknowledgeResult(
                child_id=_worktree_text(command.child_id, 256), **result
            )
        if isinstance(command, p.WorktreeIntegrate):
            result = await _run_worktree_mutation(
                self.mutate_worktree,
                "integrate",
                command.child_id,
                review_id=command.review_id,
                digest=command.digest,
                confirmation_token=command.confirmation_token,
            )
            return p.WorktreeMutationResult(
                child_id=_worktree_text(command.child_id, 256), **result
            )
        if isinstance(command, p.WorktreeDiscard):
            result = await _run_worktree_mutation(
                self.mutate_worktree,
                "discard",
                command.child_id,
                force=command.force,
                review_id=command.review_id,
                confirmation_token=command.confirmation_token,
            )
            return p.WorktreeMutationResult(
                child_id=_worktree_text(command.child_id, 256), **result
            )
        if isinstance(command, p.SessionOpen):
            target = command.session
            if target is not None and hasattr(self.runtime.sessions, "resolve"):
                try:
                    target = self.runtime.sessions.resolve(target, namespace="main")["logical_id"]
                except SessionError:
                    from ..session.names import valid_name
                    if valid_name(target) or not command.create:
                        raise
            if command.root:
                root = Path(command.root)
                if not root.is_absolute():
                    raise ValueError("Session root must be an absolute path")
                if not command.create or self.runtime.sessions.exists(target):
                    raise ValueError("An external root can only be set when creating a new session")
                validate_linked_root = getattr(self.runtime, "_validate_linked_root", None)
                if callable(validate_linked_root):
                    validate_linked_root(root.resolve())
            summary = self.open_session(target, create=command.create, recover=command.recover)
            if command.root:
                setter = getattr(self.runtime, "set_session_root", None)
                if not callable(setter):
                    raise ValueError("External session roots are unavailable")
                setter(target, command.root, kind="external")
            return p.SessionOpenResult(session=summary)
        if isinstance(command, p.AttachmentPrepare):
            return await self.attachments.prepare(command)
        if isinstance(command, p.AttachmentPreview):
            return await self.attachments.preview(command)
        if isinstance(command, p.SessionEnqueue) and command.session == MANAGER_SESSION_ID:
            # A manager draft lives in memory until its first accepted message, so a
            # restart forgets it; recreate it through the same path as ManagerOpen
            # (which also enforces ``[manager] enabled``) rather than refusing.
            if not self.runtime.sessions.exists(MANAGER_SESSION_ID):
                self.open_manager()
        if isinstance(command, (p.SessionStart, p.SessionEnqueue)) and command.client_request_id:
            from ..session.submissions import submission_digest, validate_request_id
            validate_request_id(command.client_request_id)
            digest = submission_digest(
                "start" if isinstance(command, p.SessionStart) else "enqueue",
                command.content, getattr(command, "mode", "queue"),
                [command.blocks, command.attachments, command.attachment_labels],
            )
            handle = self._session(command.session)
            row = handle._store.submission(command.session, command.client_request_id, digest)
            if row is None:
                queued_id, turn_id = await self.enqueue(
                    command.session,
                    self.attachments.content(command.content, command.blocks, command.attachments, command.attachment_labels),
                    mode=getattr(command, "mode", "queue"),
                    client_request_id=command.client_request_id, _digest=digest,
                    title_text=command.content, title_labels=list(command.attachment_labels or []),
                )
                self.attachments.release(command.attachments)
            else:
                queued_id, turn_id = row[3], row[2]
            if isinstance(command, p.SessionStart):
                return p.SessionStartResult(session=command.session, turn_id=turn_id)
            admitted = handle._store.submission(command.session, command.client_request_id, digest)
            record = handle._store.record_at(command.session, admitted[1])
            depth = int(record.event.data.get("queue_depth", 0))
            return p.SessionEnqueueResult(
                session=command.session, queued_id=queued_id, turn_id=turn_id,
                depth=depth,
            )
        if isinstance(command, p.SessionStart):
            # Decided before the turn starts: the first message is not logged yet.
            title_text = self.titles.candidate(command.session, command.content, list(command.attachment_labels or []))
            turn_id = await self.start_turn(
                command.session, self.attachments.content(command.content, command.blocks, command.attachments, command.attachment_labels)
            )
            self.titles.start(command.session, title_text)
            self.attachments.release(command.attachments)
            return p.SessionStartResult(session=command.session, turn_id=turn_id)
        if isinstance(command, p.SessionEnqueue):
            queued_id, turn_id = await self.enqueue(
                command.session, self.attachments.content(command.content, command.blocks, command.attachments, command.attachment_labels), mode=command.mode,
                title_text=command.content, title_labels=list(command.attachment_labels or []),
            )
            self.attachments.release(command.attachments)
            return p.SessionEnqueueResult(
                session=command.session,
                queued_id=queued_id,
                depth=self.supervisor.queued_for(command.session),
                turn_id=turn_id,
            )
        if isinstance(command, p.SessionShell):
            self._refuse_manager(command.session, "shell commands")
            handle = self._session(command.session)
            return p.SessionShellResult(
                session=command.session, shell_id=self.shells.start(handle, command.command)
            )
        if isinstance(command, p.SessionJobs):
            return self.jobs.jobs(command.session)
        if isinstance(command, p.SessionJobLog):
            return await self.jobs.log(
                command.session, command.job_id, tail_lines=command.tail_lines,
                offset=command.offset, limit=command.limit,
            )
        if isinstance(command, p.SessionJobStop):
            return await self.jobs.stop(command.session, command.job_id)
        if isinstance(command, p.SessionJobBackground):
            return self.jobs.background(command.session, command.job_id)
        if isinstance(command, p.SessionCancel):
            returned_messages = []
            if command.return_queue:
                if not command.drop_queue:
                    raise ValueError("Returning queued messages requires removing them from the queue")
                # No await between this authoritative capture and Supervisor.cancel:
                # input consumed before the stop must never reappear in the draft.
                handle = self._session(command.session, create=False, recover=False)
                pending = set(handle.queued_ids)
                view, _ = self.state(command.session)
                returned_messages = [
                    "".join(block.get("text", "") for block in item.content
                            if isinstance(block, dict) and block.get("type") == "text")
                    for item in view.input_queue if item.queued_id in pending
                ]
            # Stop also kills the session's running ``!`` shell commands.
            stopped_shells = self.shells.cancel(command.session)
            cancelled, dropped = await self.cancel(
                command.session, reason=command.reason or None, drop_queue=command.drop_queue
            )
            cancelled = cancelled or stopped_shells > 0
            return p.SessionCancelResult(
                session=command.session, cancelled=cancelled, dropped=dropped,
                returned_messages=returned_messages,
            )
        if isinstance(command, p.SessionQueueMove):
            if command.offset not in {-1, 1}:
                raise ValueError("offset must be -1 or 1")
            changed = self.move_queued(command.session, command.queued_id, command.offset)
            return p.SessionQueueEditResult(session=command.session, changed=changed)
        if isinstance(command, p.SessionQueueRemove):
            if command.return_content:
                changed, content = self.remove_queued(
                    command.session, command.queued_id, return_content=True)
                return p.SessionQueueEditResult(
                    session=command.session, changed=changed, content=content)
            changed = self.remove_queued(command.session, command.queued_id)
            return p.SessionQueueEditResult(session=command.session, changed=changed)
        if isinstance(command, p.SessionQueueSetMode):
            changed = await self.set_queued_mode(command.session, command.queued_id, command.mode)
            return p.SessionQueueEditResult(session=command.session, changed=changed)
        if isinstance(command, p.SessionSubscribe):
            return p.SessionSubscribeResult(
                session=command.session, from_seq=command.from_seq
            )
        if isinstance(command, p.SessionState):
            view, seq = self.state(command.session, command.from_seq)
            return p.SessionStateResult(
                session=command.session, seq=seq, view=view.to_dict()
            )
        if isinstance(command, p.SessionActivity):
            return self.session_activity(command.session)
        if isinstance(command, p.LogsRead):
            pages = await asyncio.to_thread(
                self.read_logs,
                session_id=command.session,
                daemon_cursor=command.daemon_cursor,
                session_cursor=command.session_cursor,
                limit=command.limit,
                daemon_diagnostics=getattr(self, "daemon_diagnostics", None),
            )
            return p.LogsReadResult(
                daemon=_log_page(pages["daemon"], daemon=True),
                session=_log_page(pages["session"]),
            )
        if isinstance(command, p.AgentTranscript):
            result = self.agent_transcript(command.session, command.agent_id)
            return p.AgentTranscriptResult(
                session=command.session,
                agent_id=command.agent_id,
                found=result["found"],
                status=result["status"],
                view=result["view"],
                context=result.get("context", {}),
            )
        if isinstance(command, p.AgentCancel):
            found = self.agent_transcript(command.session, command.agent_id)
            if not found["found"]:
                return p.AgentCancelResult(
                    session=command.session, agent_id=command.agent_id,
                    message="No such agent in this session.",
                )
            reason = (command.reason or "cancelled by user")[:200]
            cancelled = cancel_child(command.agent_id, reason)
            return p.AgentCancelResult(
                session=command.session, agent_id=command.agent_id, cancelled=cancelled,
                message=(
                    "Cancellation requested for this agent only; its parent turn continues."
                    if cancelled else (
                        f"Agent is not running (status: {found['status']}). "
                        "Subagents end with their run."
                    )
                ),
            )
        if isinstance(command, p.AgentClose):
            found = self.agent_transcript(command.session, command.agent_id)
            if not found["found"]:
                return p.AgentCloseResult(
                    session=command.session, agent_id=command.agent_id,
                    message="No such agent in this session.",
                )
            reason = (command.reason or "closed by user")[:200]
            closed, message = await self.runtime.close_agent(
                command.session, command.agent_id, reason=reason, by="user"
            )
            return p.AgentCloseResult(
                session=command.session, agent_id=command.agent_id, closed=closed, message=message,
            )
        if isinstance(command, p.SessionFork):
            return p.SessionForkResult(
                session=self.fork(
                    command.session, command.at_seq, new_id=command.new_id
                )
            )
        if isinstance(command, p.SessionDelete):
            record = self.delete(
                command.session, force=command.force, reason=command.reason
            )
            return p.SessionDeleteResult(
                session=command.session,
                trash_id=record.trash_id,
                delete_after=record.delete_after,
            )
        if isinstance(command, p.SessionRestore):
            return p.SessionRestoreResult(session=self.restore(command.trash_id))
        if isinstance(command, p.SessionRename):
            return p.SessionRenameResult(session=self.rename(command.session_id, command.title))
        if isinstance(command, p.SessionExport):
            return p.SessionExportResult(
                session=command.session,
                format=command.format,
                content=self.export(command.session, format=command.format),
            )
        if isinstance(command, p.PermissionResolve):
            return p.PermissionResolveResult(
                session=command.session,
                request_id=command.request_id,
                resolved=self.resolve_permission(
                    command.session,
                    command.request_id,
                    command.decision,
                    client_id=command.client_id,
                ),
                client_id=command.client_id,
            )
        if isinstance(command, p.QuestionAnswer):
            resolved, error = await self.answer_question(
                command.session, command.answer,
                call_id=command.call_id, question_id=command.question_id,
            )
            return p.QuestionAnswerResult(
                session=command.session,
                call_id=command.call_id,
                resolved=resolved,
                error=error,
            )
        if isinstance(command, p.ExtensionsReload):
            return _reload_result(await self.reload_extensions(command.trigger))
        if isinstance(command, p.ExtensionsList):
            extensions = self.runtime.extensions
            return p.ExtensionsListResult(
                generation=getattr(extensions, "generation", 0),
                extensions=list(self.list_extensions()),
            )
        if isinstance(command, p.ExtensionsValidate):
            report = self.validate_extensions(command.target)
            return p.ExtensionsValidateResult(
                generation=report["generation"],
                valid=report["valid"],
                checked=report["checked"],
                results=report["results"],
            )
        if isinstance(command, p.ExtensionsTrash):
            outcome = await self.trash_extension(
                command.target, reason=command.reason, force=command.force
            )
            return _trash_result(command.target, outcome)
        if isinstance(command, p.ModelsRefresh):
            status = await self.refresh_models()
            return p.ModelsRefreshResult(status=_status_dict(status))
        if isinstance(command, p.SetupStatus):
            return p.SetupStatusResult(**await setup_status(self.runtime))
        if isinstance(command, p.SetupSave):
            return p.SetupSaveResult(**await setup_save(self.runtime, command.provider, command.model, reload=not self.supervisor.running))
        if isinstance(command, p.ModelsList):
            initializer = getattr(self.runtime, "ensure_models", None)
            if callable(initializer):
                await initializer()
            models = self.list_models(
                provider=command.provider,
                tier=command.tier,
                selectable_only=command.selectable_only,
                search=command.search,
                scope=command.scope,
            )
            return p.ModelsListResult(
                count=len(models),
                models=[
                    self._model_row(model, selectable_only=command.selectable_only)
                    for model in models
                ],
            )
        if isinstance(command, p.ModelShow):
            initializer = getattr(self.runtime, "ensure_models", None)
            if callable(initializer):
                await initializer()
            model = self.model_info(command.ref)
            return p.ModelShowResult(
                ref=command.ref, found=model is not None, model=model
            )
        if isinstance(command, (p.DefaultModelSettings, p.DefaultModelSet)):
            initializer = getattr(self.runtime, "ensure_models", None)
            if callable(initializer):
                await initializer()
            if isinstance(command, p.DefaultModelSet):
                state = await model_settings.default_models_set(
                    self.runtime, list(command.refs), reload=not self.supervisor.running
                )
            else:
                state = model_settings.default_settings(self.runtime)
            return p.DefaultModelSettingsResult(**state)
        if isinstance(command, p.ModelTiers):
            initializer = getattr(self.runtime, "ensure_models", None)
            if callable(initializer):
                await initializer()
            return self._tiers_result()
        if isinstance(command, (p.ModelTierSet, p.ModelTierReset, p.AgentMaxTierSet)):
            # Tier edits are a cheap in-place swap of the tier table, registry index and
            # router (Runtime.reload_tier_config); no provider is touched, so they apply
            # even while a turn runs. Running children keep their chosen model; the next
            # spawn and turn resolve the new order. Restart is needed only when the
            # runtime was built with injected routes and cannot reload.
            if isinstance(command, p.ModelTierSet):
                state = await model_settings.tier_set(self.runtime, command.tier, list(command.refs), efforts=command.efforts, reload=True)
            elif isinstance(command, p.ModelTierReset):
                state = await model_settings.tier_reset(self.runtime, command.tier, reload=True)
            else:
                state = await model_settings.agent_max_tier_set(self.runtime, command.tier, reload=True)
            return self._tiers_result(restart_required=bool(state.get("restart_required")))
        if isinstance(command, p.SessionTitleSettings):
            return p.SessionTitleSettingsResult(**model_settings.session_title_settings(self.runtime))
        if isinstance(command, p.SessionTitleSettingsSet):
            return p.SessionTitleSettingsResult(
                **await model_settings.session_title_settings_set(
                    self.runtime, enabled=command.enabled, model=command.model
                )
            )
        if isinstance(command, p.UsageRefreshSettings):
            return p.UsageRefreshSettingsResult(**model_settings.usage_refresh_settings(self.runtime))
        if isinstance(command, p.UsageRefreshSettingsSet):
            return p.UsageRefreshSettingsResult(
                **await model_settings.usage_refresh_settings_set(self.runtime, command.seconds)
            )
        if isinstance(command, p.ClaudeCodeSettings):
            return p.ClaudeCodeSettingsResult(**model_settings.claude_code_settings(self.runtime))
        if isinstance(command, p.ClaudeCodeSettingsSet):
            if self.supervisor.running:
                # The toggle gates which routes exist; writing it while a turn runs would
                # leave the config and the live router disagreeing until a restart.
                raise SessionBusy("Claude Code settings cannot change while a turn is running; try again when idle")
            return p.ClaudeCodeSettingsResult(
                **await model_settings.claude_code_settings_set(
                    self.runtime, main_enabled=command.main_enabled,
                    subagent_backend=command.subagent_backend, reload=True
                )
            )
        if isinstance(command, p.ModelSelect):
            self._refuse_manager(command.session, "model selection")
            selection = self.select_model(command.session, command.ref)
            return p.ModelSelectResult(
                session=command.session,
                accepted=True,
                reference=selection.reference,
                provider=selection.provider,
                model=selection.model,
                tier=selection.tier,
                tier_source=selection.tier_source,
                requested_tier=selection.requested_tier,
                clamped=bool(selection.clamped),
                fallback=self._fallback_chain(selection),
                apply_next_turn=True,
            )
        if isinstance(command, p.ReasoningEffortSelect):
            self._refuse_manager(command.session, "reasoning effort selection")
            handle = self._session(command.session)
            metadata = self._root_reasoning_effort_metadata(handle)
            if command.effort is not None:
                if command.effort not in REASONING_EFFORTS:
                    raise ValueError("unknown reasoning effort")
                if command.effort not in metadata["supported_levels"]:
                    raise ValueError("reasoning effort is not supported by the current model")
                handle.select_reasoning_effort(
                    ReasoningEffortSelection(effort=command.effort)
                )
            else:
                handle.clear_reasoning_effort()
            remember = getattr(self.runtime, "remember_agent_choice", None)
            if callable(remember):
                remember(handle)
            remember_effort = getattr(self.runtime, "remember_model_effort", None)
            if callable(remember_effort):
                remember_effort(handle)
            current = self._root_reasoning_effort_metadata(handle)
            return p.ReasoningEffortSelectResult(
                session=command.session,
                accepted=True,
                stored_override=current["stored_override"],
                effective_effort=current["effective_effort"],
                source=current["source"],
                supported_levels=current["supported_levels"],
                apply_next_turn=True,
            )
        if isinstance(command, p.AgentsList):
            agents = self.runtime.agents
            return p.AgentsListResult(
                generation=getattr(agents, "generation", 0),
                agents=self.list_agents(),
                default=getattr(self.runtime, "default_root_agent", lambda: "orchestrator")(),
            )
        if isinstance(command, p.AgentCurrent):
            metadata = self.current_agent_metadata(command.session)
            return p.AgentCurrentResult(session=command.session, **metadata)
        if isinstance(command, p.AgentSelect):
            self._refuse_manager(command.session, "role selection")
            name, source = self.select_agent(command.session, command.name)
            return p.AgentSelectResult(session=command.session, name=name, source=source)
        if isinstance(command, p.AgentReset):
            self._refuse_manager(command.session, "role selection")
            name, source = self.select_agent(command.session, None)
            return p.AgentSelectResult(session=command.session, name=name, source=source)
        if isinstance(command, p.ToolsList):
            tools = await self.list_tools()
            return p.ToolsListResult(count=len(tools), tools=tools)
        if isinstance(command, p.ContextMcpLoadingSelect):
            self._refuse_manager(command.session, "MCP, skills or extensions")
            session = self._session(command.session, create=False, recover=False)
            context = await self.inspect_context(command.session)
            if not any(row.get("name") == command.server for row in context["mcp_servers"]):
                raise ValueError("Unknown MCP server")
            session.select_mcp_loading(command.server, command.mode)
            return p.ContextInspectResult(session=command.session, **await self.inspect_context(command.session))
        if isinstance(command, p.ContextExtensionSelect):
            self._refuse_manager(command.session, "MCP, skills or extensions")
            session = self._session(command.session, create=False, recover=False)
            if session.context_locked or session.active:
                raise ValueError("Skills, MCP and agents are locked after the first turn to preserve the prompt cache. Start a new session to change them.")
            context = await self.inspect_context(command.session)
            rows = context[{"skills": "skills_index", "mcp": "mcp_servers", "tools": "tools"}[command.category]]
            if not any(row.get("name") == command.name for row in rows):
                raise ValueError("Unknown extension")
            session.select_extension(command.category, command.name, command.enabled)
            return p.ContextInspectResult(session=command.session, **await self.inspect_context(command.session))
        if isinstance(command, p.ContextInspect):
            result = await self.inspect_context(command.session)
            return p.ContextInspectResult(session=command.session, **result)
        if isinstance(command, p.McpServerRestart):
            from ..util import redact_secrets

            manager = getattr(self.runtime, "_mcp", None)
            if manager is None or command.name not in manager.server_names:
                return p.McpServerRestartResult(name=redact_secrets(command.name), error="Unknown MCP server")
            # Close then reconnect; connect never raises for a dead server, it records health.
            await manager.disconnect(command.name, reason="restart")
            await manager.connect(command.name)
            detail = manager.server_detail(command.name) or {}
            return p.McpServerRestartResult(
                name=redact_secrets(manager.redact_display(command.name)),
                status=str(detail.get("status") or "unknown"),
                error=redact_secrets(manager.redact_display(str(detail.get("error") or ""))),
            )
        if isinstance(command, p.McpServerShow):
            from ..util import redact_secrets

            manager = getattr(self.runtime, "_mcp", None)

            def display(text: str) -> str:
                if manager is not None:
                    text = manager.redact_display(text)
                return redact_secrets(text)

            name = display(command.name)
            if not 0 <= command.max_bytes <= 1_048_576:
                return p.McpServerShowResult(name=name, error="max_bytes must be between 0 and 1048576")
            # Do not ensure_started: even lazy initialization can connect servers.
            detail = manager.server_detail(command.name) if manager is not None else None
            if detail is None:
                return p.McpServerShowResult(name=name, error="MCP server state unavailable")
            extensions = getattr(self.runtime, "_extensions", None)
            detail["scope"] = getattr(extensions, "mcp_scopes", {}).get(command.name, "unavailable")
            # Peek only: opening a session could unarchive it or initialize state.
            sessions = getattr(self.runtime, "sessions", None)
            session = getattr(sessions, "_handles", {}).get(command.session)
            if session is not None:
                detail["enabled"] = detail["enabled"] and command.name not in session.disabled_extensions["mcp"]
                choices = session.mcp_loading_choices
                frozen = session.mcp_loading_frozen
                detail["tool_loading"] = (frozen.get(command.name, "search") if frozen is not None
                                          else choices.get(command.name, detail["tool_loading"]))
                if command.name in choices:
                    detail["tool_loading_source"] = "session"

            def scrub(value: Any) -> Any:
                if isinstance(value, str):
                    return display(value)
                if isinstance(value, dict):
                    return {display(str(key)): scrub(item) for key, item in value.items()}
                if isinstance(value, (tuple, list)):
                    return [scrub(item) for item in value]
                return value

            detail = scrub(detail)
            encoded = json.dumps(detail, ensure_ascii=False).encode("utf-8")
            clipped = len(encoded) > command.max_bytes
            fields = {key: detail[key] for key in (
                "status", "scope", "enabled", "transport", "command_label", "tool_loading",
                "tool_loading_source"
            ) if key in detail}
            if clipped:
                # Keep metadata, but omit the entire detail rather than returning
                # a raw JSON fragment or silently substituting compact schemas.
                detail = {}
                fields["error"] = (
                    "MCP snapshot exceeds requested byte limit; increase max_bytes "
                    "to include tools, resources, templates, prompts, instructions "
                    "and server_info (all omitted)."
                )
            else:
                fields.update({key: detail[key] for key in (
                    "tools", "resources", "prompts"
                ) if key in detail})
                fields["server_info"] = detail.get("server_info") or {}
                fields["instructions"] = detail.get("instructions") or ""
                if "error" in detail:
                    fields["error"] = detail["error"] or ""
            return p.McpServerShowResult(name=name, detail=detail, clipped=clipped, **fields)
        if isinstance(command, p.SkillInspect):
            return self._skill_inspect(command)
        if isinstance(command, p.Doctor):
            # Construction is lazy: without this, a fresh daemon reports an
            # empty generation rather than discovering workspace MCP config.
            await self.runtime.ensure_started()
            # The report can read a bounded set of session tails (up to 64 x
            # 512 KiB) and parse their JSON. ``doctor`` stays a synchronous
            # facade method -- its direct callers are unchanged -- but the wire
            # path runs it in a worker thread so a large scan cannot stall the
            # event loop and every other session.
            report = await asyncio.to_thread(
                self.doctor, explain_reload=command.explain_reload
            )
            return p.DoctorResult(ok=not self._closed, report=report)
        if isinstance(command, p.UpdateStatus):
            # One bounded HTTP request at most once a day (cached on disk), so
            # it runs off the event loop like ``Doctor``.
            return p.UpdateStatusResult(
                **await asyncio.to_thread(self.update_status, announce=command.announce)
            )
        if isinstance(command, p.Health):
            return p.HealthResult(**self.health())
        if isinstance(command, p.Shutdown):
            return p.ShutdownResult(stopping=await self.shutdown(command.reason))
        raise ValueError(f"unknown command {type(command).__name__}")

    # -- internals ---------------------------------------------------------

    def _session(
        self, session_id: str, *, create: bool = True, recover: bool = True
    ) -> Any:
        """Open a session and take over its queue scheduling exactly once."""
        if (
            create
            and session_id == MANAGER_SESSION_ID
            and not self.runtime.sessions.exists(session_id)
        ):
            raise ValueError(
                f"session id {MANAGER_SESSION_ID!r} is reserved for the manager; use ManagerOpen"
            )
        handle = self.runtime.session(session_id, create=create, recover=recover)
        if getattr(handle, "kind", None) == MANAGER_KIND and not self.manager_config().enabled:
            raise ConfigError(MANAGER_DISABLED_MESSAGE)
        if session_id not in self._managed:
            bind = getattr(handle, "bind", None)
            if callable(bind):
                # The supervisor owns promotion now, so a finished turn must not
                # auto-consume the queue and bypass the global cap.
                bind(auto_start_queued=False)
            self._managed.add(session_id)
        return handle


#: Worktree facts per workspace, reused for this long so the sidebar's frequent
#: ``ProjectSessionsList`` polls never touch the disk; bounded by ``_WORKTREE_CACHE_MAX``.
_WORKTREE_TTL = 300.0
_WORKTREE_CACHE_MAX = 4096
_worktree_cache: dict[str, tuple[float, tuple[str, str]]] = {}


def _with_antigravity_row(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Settings -> Providers row for Antigravity unless the auth layer already supplied one.

    The CLI owns its login (run ``agy`` in a terminal), so Nexus has no sign-in or
    sign-out flow and never reads its credentials; the route is subagent-only.
    """
    if any(row.get("id") == "antigravity" for row in rows):
        return rows
    return [*rows, {
        "id": "antigravity", "label": "Antigravity (agy)", "methods": ["cli"],
        "help": ("External CLI login, managed locally and manually: run `agy` in a terminal to sign in. "
                 "Nexus never reads or stores its credentials. Subagent-only route "
                 "(antigravity/<model>); never the main, title or fallback model."),
        "connected": False, "detail": "", "can_logout": False, "login": None,
        "capabilities": ["subagent"], "scope": "subagents",
    }]


def _worktree_of(workspace: str) -> tuple[str, str]:
    """``(main checkout, branch)`` when ``workspace`` is a linked Git worktree, else ``("", "")``.

    Cached for ``_WORKTREE_TTL`` seconds per workspace (a branch switch shows up then).
    """
    now = time.monotonic()
    cached = _worktree_cache.get(workspace)
    if cached and now - cached[0] < _WORKTREE_TTL:
        return cached[1]
    head = git_head(workspace)
    value = ("", "")
    if head.get("worktree") and head.get("main_root"):
        value = (head["main_root"], head.get("branch") or head.get("worktree_name") or "")
    if len(_worktree_cache) >= _WORKTREE_CACHE_MAX:
        _worktree_cache.clear()
    _worktree_cache[workspace] = (now, value)
    return value


def _content(content: str, blocks: list[dict[str, Any]]) -> Any:
    """Coerce a wire payload into session content (text or typed blocks)."""
    if blocks:
        return msgspec.convert(blocks, type=list[ContentBlock])
    return content


def _empty_log_page(cursor: str | int | None = None) -> dict[str, Any]:
    return {"entries": [], "next_cursor": cursor, "truncated": False, "has_more": False}


def _log_page(value: Mapping[str, Any], *, daemon: bool = False) -> Any:
    page_type = p.DaemonLogPage if daemon else p.SessionLogPage
    return page_type(
        entries=[p.LogEntry(**entry) for entry in value.get("entries", ())],
        next_cursor=value.get("next_cursor"),
        truncated=bool(value.get("truncated", False)),
        has_more=bool(value.get("has_more", False)),
    )


def _hex_id(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def _validate_worktree_child_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ValueError("child_id must be a non-empty string of at most 256 characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("child_id must be valid UTF-8 text") from exc
    return value


def _canonical_json(value: Any) -> bytes:
    return msgspec.json.encode(value)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


async def _run_worktree_mutation(
    function: Any, *args: Any, **kwargs: Any
) -> dict[str, Any]:
    """Drain the worker so a cancellation cannot hide a durable Git outcome."""
    cancelled = threading.Event()

    class Cancel:
        def raise_if_cancelled(self) -> None:
            if cancelled.is_set():
                raise _WorktreeMutationCancelled("worktree mutation cancelled")

    kwargs["cancel"] = Cancel()
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        return await asyncio.shield(task)


class _WorktreeMutationCancelled(Exception):
    """Internal cancellation signal understood by the worktree journals."""


def _worktree_state_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("runtime returned invalid session worktree state")
    return dict(value)


def _asdict(value: Any) -> dict[str, Any]:
    if isinstance(value, msgspec.Struct):
        return msgspec.structs.asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return dict(value)


def _status_dict(status: Any) -> dict[str, Any] | None:
    if status is None:
        return None
    return _asdict(status)


def _reload_result(report: Any) -> p.ExtensionsReloadResult:
    if report is None:
        return p.ExtensionsReloadResult()
    modules = getattr(getattr(report, "diff", None), "modules", None)
    return p.ExtensionsReloadResult(
        generation=getattr(report, "generation", 0),
        previous_generation=getattr(report, "previous_generation", 0),
        changed=bool(getattr(report, "changed", False)),
        loaded=list(getattr(modules, "added", ()) or ()),
        unloaded=list(getattr(modules, "removed", ()) or ()),
        failed=[_asdict(item) for item in getattr(report, "failed", ()) or ()],
    )


def _trash_result(target: str, outcome: Any) -> p.ExtensionsTrashResult:
    record = getattr(outcome, "record", None)
    report = getattr(outcome, "report", None)
    if record is None:
        raise ExtensionTrashError("extension trash returned no record")
    return p.ExtensionsTrashResult(
        target=sanitize_text(target, limit=400),
        trash_id=getattr(record, "trash_id", ""),
        source_path=sanitize_text(getattr(record, "source_path", ""), limit=400),
        relative_path=sanitize_text(getattr(record, "relative_path", ""), limit=400),
        origin=getattr(record, "origin", ""),
        names=list(getattr(record, "modules", ()) or ()),
        tools=list(getattr(record, "tools", ()) or ()),
        sha256=getattr(record, "sha256", ""),
        module_generation=getattr(record, "generation", 0),
        trashed_at=getattr(record, "trashed_at", 0.0),
        delete_after=getattr(record, "delete_after", 0.0),
        reason=sanitize_text(getattr(record, "reason", ""), limit=300),
        changed=bool(getattr(report, "changed", False)),
        generation=getattr(report, "generation", 0),
        previous_generation=getattr(report, "previous_generation", 0),
    )


__all__ = ["DEFAULT_MAX_CONCURRENT_TURNS", "HostFacade"]
