"""Signed kernel checkpoints: a cache beside the session log (plan code-mode-kernel §3.8).

The session log stays the source of truth; a checkpoint is Monty's ``dump()`` bytes in the
shared ``~/.nexus/nexus.db`` table ``code_kernel_snapshots`` (``session_id, kernel_id,
after_n, monty_version, bytes, mac, created_at``), one newest row per kernel. It is machine
state: never part of a session record, JSONL export or replay.

Monty documents snapshots as unauthenticated ("only load unmodified bytes"), so every row is
signed with HMAC-SHA256 under a per-home key (``<nexus home>/code-kernel.key``, created on first
use with mode 0600, like the other daemon secrets). The MAC binds ``session_id``, ``kernel_id``,
``after_n``, ``monty_version`` and the bytes, each length-prefixed, and is compared in constant
time; a row that fails it is refused, never loaded. The table is created with
``CREATE TABLE IF NOT EXISTS`` rather than a ``_SCHEMA_STEPS`` step: bumping ``SCHEMA_VERSION``
(a reset version) would back up and replace every user's database for a disposable cache.

``db`` is any object with ``transaction()`` and ``read()`` context managers yielding a
``sqlite3.Connection`` (``nexus.session.db.StateDatabase``).
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

KEY_BYTES = 32
KEY_NAME = 'code-kernel.key'
MAX_ID_CHARS = 512

_TABLE = (
    'CREATE TABLE IF NOT EXISTS code_kernel_snapshots ('
    ' session_id TEXT NOT NULL, kernel_id TEXT NOT NULL, after_n INTEGER NOT NULL,'
    ' monty_version TEXT NOT NULL, bytes BLOB NOT NULL, mac BLOB NOT NULL, created_at REAL NOT NULL,'
    ' PRIMARY KEY (session_id, kernel_id))')


def load_key(path: Path | str) -> bytes:
    """The per-home MAC key, created (0600, atomically) on first use."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(secrets.token_bytes(KEY_BYTES))
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    key = path.read_bytes()
    if len(key) != KEY_BYTES:
        raise ValueError(f'{path} is not a {KEY_BYTES}-byte key')
    return key


def sign(key: bytes, session_id: str, kernel_id: str, after_n: int, monty_version: str,
         data: bytes) -> bytes:
    """HMAC-SHA256 over the length-prefixed binding fields and the snapshot bytes."""
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for part in (session_id.encode(), kernel_id.encode(), str(int(after_n)).encode(),
                 (monty_version or '').encode(), data):
        mac.update(len(part).to_bytes(8, 'big'))
        mac.update(part)
    return mac.digest()


@dataclass(frozen=True)
class Checkpoint:
    """A stored row (not yet verified unless it came from ``SnapshotStore.verified``)."""

    session_id: str
    kernel_id: str
    after_n: int
    monty_version: str
    data: bytes
    mac: bytes
    created_at: float

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class SnapshotStore:
    """The ``code_kernel_snapshots`` table with signing, retention and deletion."""

    def __init__(self, db, key: bytes):
        if len(key) < 16:
            raise ValueError('snapshot key too short')
        self.db = db
        self._key = key
        with db.transaction() as conn:
            conn.execute(_TABLE)

    @staticmethod
    def _ids(session_id: str, kernel_id: str) -> None:
        if (not isinstance(session_id, str) or not isinstance(kernel_id, str) or not session_id
                or not kernel_id or len(session_id) > MAX_ID_CHARS or len(kernel_id) > MAX_ID_CHARS):
            raise ValueError('bounded nonempty session_id and kernel_id required')

    def put(self, session_id: str, kernel_id: str, after_n: int, monty_version: str | None,
            data: bytes) -> Checkpoint:
        """Replace the kernel's checkpoint (only the newest row is kept)."""
        self._ids(session_id, kernel_id)
        if type(data) is not bytes or type(after_n) is not int or after_n < 0:
            raise ValueError('invalid checkpoint')
        version = monty_version or ''
        mac = sign(self._key, session_id, kernel_id, after_n, version, data)
        now = time.time()
        with self.db.transaction() as conn:
            conn.execute(
                'INSERT INTO code_kernel_snapshots(session_id, kernel_id, after_n, monty_version, bytes, mac,'
                ' created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id, kernel_id) DO UPDATE SET'
                ' after_n=excluded.after_n, monty_version=excluded.monty_version, bytes=excluded.bytes,'
                ' mac=excluded.mac, created_at=excluded.created_at',
                (session_id, kernel_id, after_n, version, data, mac, now))
        return Checkpoint(session_id, kernel_id, after_n, version, data, mac, now)

    def get(self, session_id: str, kernel_id: str) -> Checkpoint | None:
        """The stored row, unverified."""
        self._ids(session_id, kernel_id)
        with self.db.read() as conn:
            row = conn.execute(
                'SELECT after_n, monty_version, bytes, mac, created_at FROM code_kernel_snapshots'
                ' WHERE session_id=? AND kernel_id=?', (session_id, kernel_id)).fetchone()
        if row is None:
            return None
        return Checkpoint(session_id, kernel_id, int(row[0]), str(row[1]), bytes(row[2]), bytes(row[3]),
                          float(row[4]))

    def info(self, session_id: str, kernel_id: str) -> tuple[int, int] | None:
        """``(after_n, bytes)`` of the stored row without reading the blob, or ``None``."""
        self._ids(session_id, kernel_id)
        with self.db.read() as conn:
            row = conn.execute(
                'SELECT after_n, length(bytes) FROM code_kernel_snapshots WHERE session_id=? AND kernel_id=?',
                (session_id, kernel_id)).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def verified(self, session_id: str, kernel_id: str, monty_version: str | None
                 ) -> tuple[Checkpoint | None, str | None]:
        """``(checkpoint, None)`` when usable, else ``(None, reason)`` with reason ``missing | invalid | version``.

        The MAC is checked first (it covers the stored version and kernel id), then the Monty
        version of this process: a validly signed snapshot of another Monty is ``version``.
        """
        row = self.get(session_id, kernel_id)
        if row is None:
            return None, 'missing'
        expected = sign(self._key, row.session_id, row.kernel_id, row.after_n, row.monty_version, row.data)
        if not hmac.compare_digest(expected, row.mac):
            return None, 'invalid'
        if row.monty_version != (monty_version or ''):
            return None, 'version'
        return row, None

    def delete_kernel(self, session_id: str, kernel_id: str) -> None:
        self._ids(session_id, kernel_id)
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM code_kernel_snapshots WHERE session_id=? AND kernel_id=?',
                         (session_id, kernel_id))

    def delete_session(self, session_id: str) -> int:
        """Delete every checkpoint of a session; returns the number of rows removed."""
        self._ids(session_id, 'x')
        with self.db.transaction() as conn:
            return conn.execute('DELETE FROM code_kernel_snapshots WHERE session_id=?', (session_id,)).rowcount


__all__ = ['Checkpoint', 'KEY_NAME', 'SnapshotStore', 'load_key', 'sign']
