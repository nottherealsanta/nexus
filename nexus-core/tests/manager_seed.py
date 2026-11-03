"""Seeded sessions for the Manager's tests and the retrieval eval: a store with real projects, sessions, user
messages, final answers (indexed as `final_answer`) and, optionally, topic lines."""
from __future__ import annotations

from pathlib import Path

from nexus.llmhub.types import Message, Text
from nexus.store import Project, Session, Store

DAY = 86400.0


def project(store: Store, root: Path, name: str) -> Project:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return store.project_for_path(str(path), name)[0]


def seed(store: Store, proj: Project, *, title: str, user: str, answer: str | None = None, topic: str | None = None,
         now: float, age_days: float = 0.0, archived: bool = False, parent: Session | None = None) -> Session:
    """One finished session whose last activity was `age_days` before `now`."""
    session = store.create_session(proj, title=title, agent="build", model="mock/hello", parent=parent)
    store.append_message(session.id, Message("user", (Text(user),)))
    if answer is not None:
        turn = store.start_turn(session.id)
        store.save_assistant_message(session.id, Message("assistant", (Text(answer),)), turn_id=turn["id"])
        store.finish_turn(turn["id"], "completed", steps=1)
    if topic:
        store.set_topic(session.id, topic, store.last_message_seq(session.id))
    when = now - age_days * DAY
    with store.db:
        store.db.execute("update session set updated = ?, created = ?, archived = ? where id = ?",
                         (when, when, int(archived), session.id))
    return store.session(session.id)  # type: ignore[return-value]
