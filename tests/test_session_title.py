"""Automatic session titles: cleaning, the side call, storage, and the trigger.

Plan: plans/SESSION_TITLE_PLAN.md, Part 2.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.errors import ConfigError
from nexus.host_support.auto_title import AutoTitler
from nexus.model.message import Message, Text
from nexus.model.stream import MessageStop, TextDelta, Usage
from nexus.session import title as title_mod
from nexus.session.db import SCHEMA_VERSION, SqliteSessionStore, StateDatabase
from nexus.session.title import TITLE_MAX_CHARS, clean_title, generate_title, title_input


# -- clean_title -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Debugging production 500 errors", "Debugging production 500 errors"),
        ('"Rate limiting implementation"', "Rate limiting implementation"),
        ("`parser.py` bug fix", "parser.py bug fix"),
        ("**Auth refresh token support**", "Auth refresh token support"),
        ("# Config review", "Config review"),
        ("Title: Postgres API connection.", "Postgres API connection"),
        ("title - Greeting", "Greeting"),
        ("\n\n  Quick check-in\nsecond line ignored", "Quick check-in"),
        ("Dark   mode\ttoggle", "Dark mode toggle"),
        ("", ""),
        ("   \n  ", ""),
        ('""', ""),
        ("Zero‮width​ title", "Zerowidth title"),
    ],
)
def test_clean_title_table(raw, expected):
    assert clean_title(raw) == expected


def test_clean_title_cuts_long_replies_at_a_word_boundary():
    raw = "Investigating intermittent timeouts in the payment webhook retry worker queue"
    cleaned = clean_title(raw)
    assert len(cleaned) <= TITLE_MAX_CHARS
    assert raw.startswith(cleaned) and not cleaned.endswith(" ")
    assert cleaned == "Investigating intermittent timeouts in the payment"


def test_clean_title_hard_cuts_one_long_word():
    assert clean_title("x" * 80) == "x" * TITLE_MAX_CHARS


def test_clean_title_rejects_non_text():
    assert clean_title(None) == ""  # type: ignore[arg-type]


def test_title_input_is_bounded_with_a_visible_ellipsis():
    assert title_input("  hello  ") == "hello"
    long = title_input("a" * 5000)
    assert len(long) == title_mod.TITLE_INPUT_MAX_CHARS and long.endswith("…")


# -- generate_title --------------------------------------------------------


class _Provider:
    name = "fake"

    def __init__(self, text="Rate limiting implementation", *, delay=0.0, raises=None):
        self.requests = []
        self._text, self._delay, self._raises = text, delay, raises

    async def stream(self, request):
        self.requests.append(request)
        if self._raises:
            raise self._raises
        if self._delay:
            await asyncio.sleep(self._delay)
        yield TextDelta(self._text)
        yield Usage(input=210, output=7)
        yield MessageStop(stop_reason="end_turn")


class _Router:
    def __init__(self, provider, *, fail=False):
        self.provider, self._fail, self.asked = provider, fail, []

    def resolve(self, request):
        self.asked.append(request.model)
        if self._fail:
            raise ConfigError("no model")
        return SimpleNamespace(provider=self.provider, model="cheap-1", capabilities=None)


async def test_generate_title_sends_a_tool_free_bounded_request():
    provider = _Provider()
    result = await generate_title(_Router(provider), "low", "implement rate limiting")
    assert result.title == "Rate limiting implementation"
    assert (result.input_tokens, result.output_tokens) == (210, 7)
    assert result.model == "fake/cheap-1"
    request = provider.requests[0]
    assert request.tools == [] and request.model == "cheap-1"
    assert request.system == title_mod.TITLE_PROMPT
    assert request.params.max_output_tokens == title_mod.TITLE_MAX_OUTPUT_TOKENS
    assert request.params.thinking_budget is None and request.params.reasoning_effort is None
    [message] = request.messages
    assert message.role == "user"
    assert message.content[0].text == "<message>\nimplement rate limiting\n</message>"


@pytest.mark.parametrize(
    "case",
    [
        {"router": _Router(_Provider(), fail=True)},
        {"router": _Router(_Provider(raises=RuntimeError("boom")))},
        {"router": _Router(_Provider(text="   "))},
        {"router": _Router(_Provider(text='""'))},
    ],
)
async def test_failures_and_empty_replies_return_none(case):
    assert await generate_title(case["router"], "low", "hello") is None


async def test_a_slow_call_times_out(monkeypatch):
    monkeypatch.setattr(title_mod, "TITLE_TIMEOUT_S", 0.05)
    assert await generate_title(_Router(_Provider(delay=1.0)), "low", "hello") is None


async def test_empty_input_makes_no_call():
    provider = _Provider()
    assert await generate_title(_Router(provider), "low", "   ") is None
    assert provider.requests == []


async def test_the_outcome_is_logged_without_the_title_at_info(caplog):
    with caplog.at_level("INFO", logger="nexus.session.title"):
        await generate_title(_Router(_Provider()), "low", "hello")
    line = next(r.getMessage() for r in caplog.records if "outcome=" in r.getMessage())
    assert "outcome=ok" in line and "in=210" in line and "out=7" in line
    assert "Rate limiting" not in line


# -- storage ---------------------------------------------------------------


def _store(tmp_path: Path) -> SqliteSessionStore:
    db = StateDatabase(tmp_path / "state.db")
    return SqliteSessionStore(db, "project", "main", root=str(tmp_path), lock_dir=tmp_path)


def test_a_derived_title_can_be_replaced_once(tmp_path):
    store = _store(tmp_path)
    store.create("s1")
    store.append_message("s1", Message(role="user", content=[Text("hey can you look at the failing test")]))
    row = store.session_row("s1")
    assert row["title_source"] == "first_message" and row["title"].startswith("hey can you")
    assert store.set_auto_title("s1", "Failing CI test investigation") is True
    row = store.session_row("s1")
    assert (row["title"], row["title_source"]) == ("Failing CI test investigation", "auto")
    # Already written: a second call never overwrites it.
    assert store.set_auto_title("s1", "Something else") is False
    assert store.session_row("s1")["title"] == "Failing CI test investigation"


def test_a_title_before_the_first_message_is_not_written(tmp_path):
    store = _store(tmp_path)
    store.create("s1")
    assert store.set_auto_title("s1", "Too early") is False
    assert store.session_row("s1")["title_source"] == ""


def test_blank_titles_are_refused(tmp_path):
    store = _store(tmp_path)
    store.create("s1")
    store.append_message("s1", Message(role="user", content=[Text("hello there")]))
    assert store.set_auto_title("s1", "   ") is False


def test_schema_upgrade_keeps_old_rows_and_leaves_their_title_alone(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, root TEXT NOT NULL,
          created_at REAL NOT NULL, last_opened REAL NOT NULL);
        CREATE TABLE sessions (project_id TEXT NOT NULL, namespace TEXT NOT NULL DEFAULT 'main',
          id TEXT NOT NULL, created_at REAL NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
          last_activity REAL NOT NULL DEFAULT 0, message_count INTEGER NOT NULL DEFAULT 0,
          title TEXT NOT NULL DEFAULT '', parent_id TEXT NOT NULL DEFAULT '',
          fork_seq INTEGER NOT NULL DEFAULT 0, archived_at REAL, archive_reason TEXT,
          trash_id TEXT UNIQUE, trashed_at REAL, trash_expires_at REAL, trash_reason TEXT,
          PRIMARY KEY (project_id, namespace, id));
        CREATE TABLE records (project_id TEXT NOT NULL, namespace TEXT NOT NULL,
          session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL,
          body BLOB NOT NULL, PRIMARY KEY (project_id, namespace, session_id, seq)) WITHOUT ROWID;
        CREATE TABLE snapshots (project_id TEXT, namespace TEXT, session_id TEXT,
          seq INTEGER NOT NULL, body BLOB NOT NULL, PRIMARY KEY (project_id, namespace, session_id));
        CREATE TABLE kv (project_id TEXT, namespace TEXT, key TEXT, value TEXT,
          PRIMARY KEY (project_id, namespace, key));
        INSERT INTO projects VALUES ('project', '/x', 1, 1);
        INSERT INTO sessions(project_id, id, created_at, title) VALUES ('project', 'old', 1, 'Old title');
        PRAGMA user_version=1;
        """
    )
    conn.close()
    db = StateDatabase(path)
    assert db.schema_version() == SCHEMA_VERSION == 3
    store = SqliteSessionStore(db, "project", "main", root=str(tmp_path), lock_dir=tmp_path)
    row = store.session_row("old")
    assert (row["title"], row["title_source"]) == ("Old title", "")
    assert store.set_auto_title("old", "New") is False


# -- trigger ---------------------------------------------------------------


class _Sessions:
    """The slice of SessionManager the titler uses."""

    def __init__(self, *, message_count=0, parent_id="", exists=True, land_after=0):
        self.title = "first line of the message"
        self.source = "" if land_after else "first_message"
        self.count, self.parent, self.exists, self._land_after = message_count, parent_id, exists, land_after
        self.calls = 0

    def summary(self, session):
        from nexus.errors import SessionError

        if not self.exists:
            raise SessionError("missing")
        return SimpleNamespace(message_count=self.count, parent_id=self.parent)

    def set_auto_title(self, session, title):
        self.calls += 1
        if self.calls > self._land_after:
            self.source = self.source or "first_message"
        if self.source == "first_message":
            self.title, self.source = title, "auto"
            return True
        return False

    def title_source(self, session):
        return self.source


def _runtime(sessions, provider=None, *, enabled=True, model="low"):
    config = SimpleNamespace(
        v2=SimpleNamespace(sessions=SimpleNamespace(auto_title=enabled, title_model=model))
    )
    return SimpleNamespace(
        sessions=sessions, router=_Router(provider or _Provider()), _load_config=lambda: config
    )


def test_candidate_only_for_a_fresh_root_session():
    assert AutoTitler(_runtime(_Sessions())).candidate("s", "hello") == "hello"
    assert AutoTitler(_runtime(_Sessions(exists=False))).candidate("s", "hello") == "hello"
    assert AutoTitler(_runtime(_Sessions(message_count=1))).candidate("s", "hello") is None
    assert AutoTitler(_runtime(_Sessions(parent_id="p"))).candidate("s", "hello") is None
    assert AutoTitler(_runtime(_Sessions(), enabled=False)).candidate("s", "hello") is None
    assert AutoTitler(_runtime(_Sessions())).candidate("s", "   ") is None


def test_candidate_names_attachments_so_the_prompt_rules_apply():
    titler = AutoTitler(_runtime(_Sessions()))
    assert titler.candidate("s", "fix this", ["main.py"]) == "fix this @main.py"


async def test_the_title_is_stored_in_the_background():
    sessions = _Sessions()
    titler = AutoTitler(_runtime(sessions))
    titler.start("s", titler.candidate("s", "implement rate limiting"))
    await asyncio.wait_for(titler._tasks["s"], 2)
    assert sessions.title == "Rate limiting implementation" and sessions.source == "auto"
    assert "s" not in titler._tasks


async def test_it_waits_for_the_first_message_to_reach_the_log(monkeypatch):
    from nexus.host_support import auto_title

    monkeypatch.setattr(auto_title, "_STORE_DELAY_S", 0.01)
    sessions = _Sessions(land_after=3)
    titler = AutoTitler(_runtime(sessions))
    titler.start("s", "hello")
    await asyncio.wait_for(titler._tasks["s"], 2)
    assert sessions.title == "Rate limiting implementation" and sessions.calls == 4


async def test_a_user_title_is_never_overwritten():
    sessions = _Sessions()
    sessions.source, sessions.title = "user", "Mine"
    titler = AutoTitler(_runtime(sessions))
    titler.start("s", "hello")
    await asyncio.wait_for(titler._tasks["s"], 2)
    assert (sessions.title, sessions.source) == ("Mine", "user")


async def test_one_task_per_session_and_cancel_on_close():
    provider = _Provider(delay=5.0)
    titler = AutoTitler(_runtime(_Sessions(), provider))
    titler.start("s", "hello")
    titler.start("s", "hello again")
    assert len(titler._tasks) == 1
    assert titler.candidate("s", "x") is None  # already titling
    await asyncio.sleep(0.05)
    await titler.aclose()
    assert titler._tasks == {}


async def test_cancel_stops_a_pending_call():
    titler = AutoTitler(_runtime(_Sessions(), _Provider(delay=5.0)))
    titler.start("s", "hello")
    task = titler._tasks["s"]
    await asyncio.sleep(0.02)
    titler.cancel("s")
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


async def test_at_most_two_calls_run_at_once():
    running = peak = 0

    class Slow(_Provider):
        async def stream(self, request):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            yield TextDelta("Title")
            yield MessageStop(stop_reason="end_turn")

    sessions = _Sessions()
    titler = AutoTitler(_runtime(sessions, Slow()))
    for index in range(5):
        titler.start(f"s{index}", "hello")
    await asyncio.gather(*titler._tasks.values())
    assert peak == 2


# -- end to end through the host -------------------------------------------


async def test_a_new_session_gets_a_model_written_title_through_the_host(tmp_path):
    """SessionStart -> background call -> the stored title replaces the derived one."""
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ModelSection, SessionsSection
    from nexus.host import HostFacade
    from nexus.host import protocol as p
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime

    runtime = Runtime(tmp_path, config=Config(version=2, v2=ConfigV2(model=ModelSection(default="scripted/m"))), providers={"scripted": ScriptedProvider(text_response("sure"))})
    facade = HostFacade(runtime)
    facade.open_session("s1")
    # The title call gets its own router so the scripted turn provider is untouched.
    facade.titles = AutoTitler(SimpleNamespace(
        sessions=runtime.sessions, router=_Router(_Provider("Failing CI test investigation")),
        _load_config=lambda: SimpleNamespace(v2=ConfigV2(sessions=SessionsSection(title_model="scripted/title"))),
    ))
    await facade.handle(p.SessionStart(session="s1", content="hey can you look at the failing test in ci"))
    await facade.wait_idle(timeout=5.0)
    await asyncio.wait_for(asyncio.gather(*facade.titles._tasks.values()), 5)

    summary = runtime.sessions.summary("s1")
    assert summary.title == "Failing CI test investigation"
    assert runtime.sessions.title_source("s1") == "auto"

    # A second message never starts another title call.
    assert facade.titles.candidate("s1", "and another thing") is None
    await facade.shutdown()
    await runtime.aclose()


def test_no_title_call_when_the_tier_cannot_run():
    """A plain config has no tiers: never send ``low`` to a provider as a model id."""
    runtime = _runtime(_Sessions())
    runtime.router.tier_runnable = lambda tier: False
    assert AutoTitler(runtime).candidate("s", "hello") is None
    runtime.router.tier_runnable = lambda tier: True
    assert AutoTitler(runtime).candidate("s", "hello") == "hello"
    # An explicit model reference is not a tier, so the tier probe does not apply.
    pinned = _runtime(_Sessions(), model="openai/gpt-5-mini")
    pinned.router.tier_runnable = lambda tier: False
    assert AutoTitler(pinned).candidate("s", "hello") == "hello"
