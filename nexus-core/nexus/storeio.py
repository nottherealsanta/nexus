"""Threading for the SQLite store (plan 50, phase 1a; docs/daemon.md § Storage threading).

The store has one write connection and, for reads off the event loop, one
read-only connection per reader thread (WAL lets readers run beside the writer).

* `LockedConnection` wraps a connection and one re-entrant lock. Every statement
  runs under the lock and its rows are fetched before the lock is released, and
  `with conn:` holds the lock for the whole transaction. Store methods hold the
  same lock for their whole body, so one thread at a time is inside a
  transaction on the write connection, whether it is the loop thread (a
  synchronous call site that has not moved yet) or the writer thread.
* `StoreThreads` owns the writer thread (a single-worker executor: a FIFO queue,
  so writes happen in call order) and the reader pool, and gives the async
  facade: `await store.w.name(...)` runs a store method on the writer thread;
  `await store.r.name(...)` runs a read method in a read-only snapshot on a
  reader thread; `.run(fn, *args)` runs `fn(store, *args)` the same way, which
  is how several statements stay one transaction.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import sqlite3
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from .store import Store

READERS = 4

# Inside `Runtime.read()` this is the read-only store of the thread running the call; components that keep a
# store (`Runtime`, `Inputs`) answer `.store` with it, so shared read code runs unchanged on a reader.
READER: contextvars.ContextVar[Store | None] = contextvars.ContextVar("nexus_reader_store", default=None)


class Rows:
    """A statement's result, fetched under the lock so no other thread interleaves with it."""

    __slots__ = ("_pos", "_rows", "description", "lastrowid", "rowcount")

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.description = cursor.description
        self._rows = cursor.fetchall() if cursor.description is not None else []
        self._pos = 0
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid

    def fetchone(self) -> Any:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int = 1) -> list[Any]:
        rows = self._rows[self._pos:self._pos + size]
        self._pos += len(rows)
        return rows

    def fetchall(self) -> list[Any]:
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class LockedConnection:
    """A `sqlite3.Connection` whose every use is serialized by one re-entrant lock."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self.lock = lock

    def execute(self, sql: str, params: Any = ()) -> Rows:
        with self.lock:
            return Rows(self._conn.execute(sql, params))

    def executemany(self, sql: str, params: Any) -> Rows:
        with self.lock:
            return Rows(self._conn.executemany(sql, params))

    def executescript(self, script: str) -> Any:
        with self.lock:
            return self._conn.executescript(script)

    def commit(self) -> None:
        with self.lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self.lock:
            self._conn.rollback()

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    def __enter__(self) -> Self:
        self.lock.acquire()
        try:
            self._conn.__enter__()
        except BaseException:
            self.lock.release()
            raise
        return self

    def __exit__(self, *exc: object) -> Any:
        try:
            return self._conn.__exit__(*exc)
        finally:
            self.lock.release()

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    @property
    def row_factory(self) -> Any:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._conn.row_factory = value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _Calls:
    """`await calls.name(...)`: run the store method `name` on this facade's thread."""

    def __init__(self, threads: StoreThreads, kind: str) -> None:
        self._threads = threads
        self._kind = kind

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        if self._kind == "read" and name not in self._threads.read_methods and name != "run":
            raise AttributeError(f"{name} is not a read method of the store")
        if name == "run":
            return self._run

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await self._submit(lambda store: getattr(store, name)(*args, **kwargs))
        call.__name__ = name
        return call

    async def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run `fn(store, *args, **kwargs)` on this thread; one lock hold, so its statements are one unit."""
        return await self._submit(lambda store: fn(store, *args, **kwargs))

    async def _submit(self, job: Callable[[Store], Any]) -> Any:
        if self._kind == "write":
            # Shielded: a cancelled caller must not drop a queued write that its own earlier steps depend on.
            future = self._threads.writer.submit(self._threads.write_job, job)
            return await asyncio.shield(asyncio.wrap_future(future))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._threads.readers, self._threads.read_job, job)


class StoreThreads:
    """The writer thread, the reader pool and the async facade over a `Store`."""

    def __init__(self, store: Store, read_methods: frozenset[str]) -> None:
        self.store = store
        self.read_methods = read_methods
        self.writer = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-writer")
        self.readers = concurrent.futures.ThreadPoolExecutor(max_workers=READERS, thread_name_prefix="store-reader")
        self._local = threading.local()
        self._opened: list[Store] = []
        self._guard = threading.Lock()
        self.w = _Calls(self, "write")
        self.r = _Calls(self, "read")

    def write_job(self, job: Callable[[Store], Any]) -> Any:
        return job(self.store)

    def read_job(self, job: Callable[[Store], Any]) -> Any:
        reader = getattr(self._local, "store", None)
        if reader is None:
            reader = self.store._open_reader()
            self._local.store = reader
            with self._guard:
                self._opened.append(reader)
        with reader.read_transaction():  # one snapshot for the whole call
            return job(reader)

    def shutdown(self) -> None:
        self.writer.shutdown(wait=True)
        self.readers.shutdown(wait=True)
        with self._guard:
            opened, self._opened = self._opened, []
        for reader in opened:
            try:
                reader.db.close()
            except sqlite3.Error:
                pass

