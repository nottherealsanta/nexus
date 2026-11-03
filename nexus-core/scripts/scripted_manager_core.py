#!/usr/bin/env python3
"""A real Nexus Core whose Manager is the scripted model, for client tests and screenshots.

It is the real thing: the real store, bus, runtime, HTTP API and event stream, the real Manager tools and the real
card validation. Only the model is scripted (`mock/manager_demo`, nexus/mock/scenarios/manager_demo.py), so a client
can show every card, navigation and `via Manager` label with no provider and no microphone.

    cd nexus-core
    NEXUS10_HOME=$(mktemp -d) NEXUS_PORT=4813 uv run python scripts/scripted_manager_core.py

It refuses to run against a real home (it needs `$NEXUS10_HOME`, a directory you can throw away). It writes
`<home>/daemon.json` exactly like `nexus daemon start`, so the TUI and NSH discover it the same way, and enables
`[manager]` with `model = "mock/manager_demo"`.

World it seeds (projects `nexus` and `storefront`, directories under `<home>/projects/`):

    needs you   auth-refactor      a bash approval: `alembic upgrade head`
                flaky-test         a question: "Which database should the test use?" (Postgres / SQLite)
    running     dictation-cue      a turn that never ends
    finished    sidebar-polish, rail-tooltips (nexus), checkout-bug (storefront), each with a final answer
    archived    old-billing-work

Then type or say these to the Manager (see the scenario's docstring for the whole table):

    what's going on?              board card (needs you / running / finished)
    list sidebar                  find_sessions + sessions card
    approval                      the approval card for auth-refactor, with the exact command
    question                      the question card for flaky-test
    yes / no                      decide the approval last shown (the Manager checks it was shown first)
    diagram | note                a diagram card | a note card
    excerpt sidebar-polish        an excerpt card from that session's last answer
    open the second one           manager.navigate to row 2 of the card on the stage (answer it with
                                  POST /api/manager/navigate/result)
    go back                       manager.navigate with back
    start fix the cue in nexus    a new session whose first message is labelled origin "manager"
    what am i looking at          current_view, from POST /api/client/view
    clear                         clear the card
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

os.environ["NEXUS10_DEV"] = "1"  # registers the offline `mock` provider; must happen before nexus is imported

from nexus.daemon import _write_info, info_path
from nexus.llmhub.types import Message, Text
from nexus.paths import default_home, home
from nexus.questions import PendingQuestion, normalize_request
from nexus.server import build_app, make_runtime
from nexus.store import Store, new_id

MODEL = "mock/manager_demo"


def seed(rt, root: Path):
    store = rt.store
    projects = {}
    for name in ("nexus", "storefront"):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        projects[name] = rt.open_project(str(path), name)

    def session(project, title, user, answer=None, age_hours=1.0, archived=False):
        made = store.create_session(projects[project], title=title, agent="build", model="mock/hello")
        store.append_message(made.id, Message("user", (Text(user),)))
        if answer:
            turn = store.start_turn(made.id)
            store.save_assistant_message(made.id, Message("assistant", (Text(answer),)), turn_id=turn["id"])
            store.finish_turn(turn["id"], "completed", steps=1)
        when = time.time() - age_hours * 3600
        with store.db:
            store.db.execute("update session set updated = ?, created = ?, archived = ? where id = ?",
                             (when, when, int(archived), made.id))
        return made

    return {
        "auth": session("nexus", "Auth refactor", "refactor token refresh", "Token refresh is shared now.", 0.5),
        "flaky": session("nexus", "Flaky test", "the sync test is flaky", None, 0.4),
        "cue": session("nexus", "Dictation cue", "play a tone when dictation starts", None, 0.2),
        "sidebar": session("nexus", "Sidebar polish", "polish the sidebar spacing",
                           "Tightened the sidebar spacing and fixed the hover state.", 2),
        "tooltips": session("nexus", "Rail tooltips", "tooltips clip in the rail",
                            "Clamped the tooltips to the viewport.", 5),
        "checkout": session("storefront", "Checkout bug", "coupon applied twice",
                            "The coupon now applies once.", 3),
        "old": session("storefront", "Old billing work", "billing report", "Done.", 24 * 20, archived=True),
    }


def live(rt, sessions) -> None:
    """Pending approval, question and a running turn: in-memory state, so it is set up inside the server's loop."""
    loop = asyncio.get_running_loop()
    approval = new_id("per")
    request = {"id": approval, "session_id": sessions["auth"].id, "tool": "bash", "call_id": "c1",
               "args": {"command": "alembic upgrade head"}, "grant": "bash: alembic upgrade head"}
    rt.approvals[approval] = (sessions["auth"].id, loop.create_future(), request)
    rt.bus.publish("permission.asked", request, session_id=sessions["auth"].id)
    question = new_id("qst")
    questions = normalize_request({"questions": [{"id": "db", "question": "Which database should the test use?",
                                                  "options": [{"id": "pg", "label": "Postgres"},
                                                              {"id": "lite", "label": "SQLite"}]}]})
    rt.questions[question] = PendingQuestion(question, sessions["flaky"].id, None, questions, time.time(),
                                             loop.create_future())
    rt.bus.publish("question.asked", {"id": question, "session_id": sessions["flaky"].id, "questions": questions},
                   session_id=sessions["flaky"].id)
    rt.turns[sessions["cue"].id] = asyncio.ensure_future(asyncio.Event().wait())
    rt.bus.publish("session.status", {"status": "busy"}, session_id=sessions["cue"].id)


def main() -> None:
    import uvicorn

    if "NEXUS10_HOME" not in os.environ or home() == default_home():
        sys.exit("scripted_manager_core.py needs NEXUS10_HOME set to a scratch directory; it never uses ~/.nexus")
    h = home()
    h.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(h, 0o700)
    if not (h / "config.toml").exists():
        (h / "config.toml").write_text(f'[manager]\nenabled = true\nmodel = "{MODEL}"\n')
    port = int(os.environ.get("NEXUS_PORT", "4813"))
    store = Store(h / "nexus.db")
    store.set_preference("model.last", "mock/hello")
    rt = make_runtime(store)
    sessions = seed(rt, h / "projects")
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{port}"
    app = build_app(rt, token=token, url=url)
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        live(rt, sessions)  # in-memory state needs the server's running loop
        async with inner(application) as state:
            yield state

    app.router.lifespan_context = lifespan
    _write_info({"url": url, "pid": os.getpid(), "token": token, "started": time.time()})
    print(f"scripted Manager core on {url} (home {h}); model {MODEL}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=2)
    finally:
        try:
            if json.loads(info_path().read_text()).get("pid") == os.getpid():
                info_path().unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
