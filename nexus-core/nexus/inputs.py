"""Durable pending session input, adapted from old Nexus session/session.py.

Inputs stay outside model context until a safe boundary commits their message.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .codec import message_from_json
from .llmhub.types import Message
from .store import Store, new_id


class InputNotFound(Exception):
    """No matching pending input in the requested session."""


class InputBusy(Exception):
    """An input is already starting a turn."""


class Inputs:
    def __init__(self, store: Store) -> None:
        self.store = store
        store.db.executescript("""
        create table if not exists pending_input (
          id text primary key, session_id text not null references session(id) on delete cascade,
          mode text not null, parts text not null, created real not null,
          position integer not null, state text not null, message_id text, turn_id text, event_id integer
        );
        create index if not exists pending_input_session on pending_input(session_id, state, position);
        create table if not exists input_hold (
          session_id text primary key references session(id) on delete cascade
        );
        """)
        columns = {row["name"] for row in store.db.execute("pragma table_info(pending_input)")}
        if "event_id" not in columns:
            store.db.execute("alter table pending_input add column event_id integer")

    def held(self, sid: str) -> bool:
        return self.store.db.execute("select 1 from input_hold where session_id = ?", (sid,)).fetchone() is not None

    def hold(self, sid: str, held: bool) -> None:
        with self.store.db:
            if held:
                self.store.db.execute("insert or ignore into input_hold values (?)", (sid,))
            else:
                self.store.db.execute("delete from input_hold where session_id = ?", (sid,))

    def _json(self, row: Any) -> dict[str, Any]:
        return {"id": row["id"], "session_id": row["session_id"], "mode": row["mode"],
                "parts": json.loads(row["parts"]), "created": row["created"], "position": row["position"],
                "state": row["state"], "held": self.held(row["session_id"])}

    def list(self, sid: str | None = None, mode: str | None = None) -> list[dict[str, Any]]:
        sql = "select * from pending_input where state = 'pending'"
        params: list[Any] = []
        if sid is not None:
            sql += " and session_id = ?"
            params.append(sid)
        if mode is not None:
            sql += " and mode = ?"
            params.append(mode)
        return [self._json(row) for row in self.store.db.execute(sql + " order by position, created, id", params)]

    def add(self, sid: str, mode: str, parts: list[dict[str, Any]], mutation: dict[str, Any] | None = None) -> dict[str, Any]:
        iid, now = new_id("inp"), time.time()
        with self.store.db:
            pos = self.store.db.execute("select coalesce(max(position), 0) + 1 from pending_input where session_id = ?", (sid,)).fetchone()[0]
            self.store.db.execute("insert into pending_input (id,session_id,mode,parts,created,position,state) values (?,?,?,?,?,?,'pending')",
                                  (iid, sid, mode, json.dumps(parts), now, pos))
            if mutation is not None:
                record = {**mutation, "state": "resolved", "status": 202, "resource": sid,
                          "resolved": now, "expires": now + 7 * 24 * 3600,
                          "body": {"input_id": iid, "mode": mode, "state": "pending", "idempotency_key": mutation["key"]}}
                self.store._insert_mutation(record, now=now)
        return self.get(sid, iid)

    def get(self, sid: str, iid: str) -> dict[str, Any]:
        row = self.store.db.execute("select * from pending_input where session_id = ? and id = ? and state = 'pending'", (sid, iid)).fetchone()
        if row is None:
            raise InputNotFound(f"no pending input {iid}")
        return self._json(row)

    def _check_unclaimed(self, iid: str) -> None:
        row = self.store.db.execute("select turn_id from pending_input where id = ?", (iid,)).fetchone()
        if row and row["turn_id"]:
            raise InputBusy("input is already starting a turn")

    def update(self, sid: str, iid: str, *, mode: str | None = None, parts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        item = self.get(sid, iid)
        self._check_unclaimed(iid)
        if mode is not None and (mode != "steer" or item["mode"] != "queue"):
            raise ValueError("mode can only be changed from queue to steer")
        with self.store.db:
            self.store.db.execute("update pending_input set mode = ?, parts = ? where id = ?",
                                  (mode or item["mode"], json.dumps(parts if parts is not None else item["parts"]), iid))
        return self.get(sid, iid)

    def remove(self, sid: str, iid: str) -> dict[str, Any]:
        item = self.get(sid, iid)
        self._check_unclaimed(iid)
        with self.store.db:
            self.store.db.execute("update pending_input set state = 'removed' where id = ?", (iid,))
        return item

    def move(self, sid: str, iid: str, before: str) -> dict[str, Any]:
        items = self.list(sid)
        order = [item["id"] for item in items]
        if iid not in order or before not in order:
            raise InputNotFound("input or destination not pending")
        self._check_unclaimed(iid)
        if iid != before:
            order.remove(iid)
            order.insert(order.index(before), iid)
            with self.store.db:
                for pos, item_id in enumerate(order, 1):
                    self.store.db.execute("update pending_input set position = ? where id = ?", (pos, item_id))
        return self.get(sid, iid)

    def message(self, item: dict[str, Any]) -> Message:
        return message_from_json({"role": "user", "parts": item["parts"]})

    def deliver(self, sid: str, iid: str, turn_id: str) -> Any:
        item = self.get(sid, iid)
        with self.store.db:
            message = self.store._insert_message(sid, self.message(item), time.time())
            self.store.db.execute("update message set delivery = ? where id = ?", ("steer" if item["mode"] == "steer" else "queued", message.id))
            self.store.db.execute("update pending_input set state = 'delivered', message_id = ?, turn_id = ? where id = ?", (message.id, turn_id, iid))
        from dataclasses import replace
        return replace(message, delivery="steer" if item["mode"] == "steer" else "queued")
