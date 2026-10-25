"""Plan 06: the ``subagent`` roster's models/effort, its digests, and the tail notice.

The description is the source of truth. A change to the tier models or efforts it
advertises costs one ``system_tools`` cache miss; the notice is a user-role
message appended at the tail of history, never prefix bytes.
"""
from __future__ import annotations

from collections.abc import Mapping

from nexus.agents.runner import ROSTER_CLIP_NOTE
from nexus.core.loop import run_turn
from nexus.model.message import Message, MessageMeta, Text, ToolResult
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.model.tiers import (
    DEFAULT_TIER_MODELS,
    TierTable,
    effective_tier_effort,
)
from nexus.session import SessionManager
from nexus.session.export import derive_title, is_user_prompt

from tests.test_subagent_runner import Factory, make_runner
from tests.test_subagent_tiers import _write_role
from test_core_loop import (
    FakeAssembler,
    FakeLease,
    FakeSession,
    FakeSink,
    _FakeDispatcher,
    _FakeGate,
    make_resolver,
)

NOTICE_TEXT = (
    "[Nexus notice] Settings changed: {what}. The subagent tool description "
    "lists current tiers, models and effort; rely on it over earlier conversation."
)


def _digest(models: str, efforts: str) -> dict[str, str]:
    return {"models": models, "efforts": efforts}


def _notices(session) -> list[Message]:
    return [m for m in session.messages if m.meta.notice is not None]


def _notice_events(session) -> list:
    return [e for e in session.events if e.type == "context.notice"]


# -- shared effort helper ---------------------------------------------------


def test_effective_tier_effort_follows_the_router_rule():
    # An explicit selection always wins.
    assert effective_tier_effort("medium", "codex/gpt-6.1-sol", configured="high", pinned=True) == "high"
    # The shipped default applies to a built-in tier no pin has redefined...
    assert effective_tier_effort("medium", "codex/gpt-6.1-sol", configured=None, pinned=False) == "low"
    assert effective_tier_effort("medium", "opencode-go/deepseek-v4.1-flash", configured=None, pinned=False) == "max"
    # ...and not to a pinned tier, a custom tier, or a route without a default.
    assert effective_tier_effort("medium", "codex/gpt-6.1-sol", configured=None, pinned=True) is None
    assert effective_tier_effort("fast", "codex/gpt-6.1-sol", configured=None, pinned=False) is None
    assert effective_tier_effort("low", "codex/gpt-6-luna", configured=None, pinned=False) is None


# -- roster lines and digests -----------------------------------------------


def _runner(tmp_path, *, efforts=None, tiers=None, candidates=None, **kwargs):
    return make_runner(
        tmp_path,
        Factory(),
        tiers=tiers if tiers is not None else TierTable(),
        tier_candidates=candidates or (lambda tier: list(DEFAULT_TIER_MODELS.get(tier, ()))),
        tier_efforts=(lambda: efforts) if efforts is not None else None,
        **kwargs,
    )


def test_roster_lines_show_effective_effort_including_shipped_defaults(tmp_path):
    lines = _runner(tmp_path).tier_lines()
    medium = next(line for line in lines if line.startswith("medium models"))
    assert "codex/gpt-6.1-sol (effort: low)" in medium
    assert "opencode-go/deepseek-v4.1-flash (effort: max)" in medium
    low = next(line for line in lines if line.startswith("low models"))
    assert "(effort" not in low


def test_roster_effort_uses_the_live_selection_and_ignores_defaults_for_pinned_tiers(tmp_path):
    chosen = {"medium": {"codex/gpt-6.1-sol": "high"}}
    medium = next(line for line in _runner(tmp_path, efforts=chosen).tier_lines() if line.startswith("medium"))
    assert "codex/gpt-6.1-sol (effort: high)" in medium
    pinned = TierTable(overrides={"medium": ["codex/gpt-6.1-sol"]})
    runner = _runner(tmp_path, tiers=pinned, candidates=lambda tier: ["codex/gpt-6.1-sol"])
    medium = next(line for line in runner.tier_lines() if line.startswith("medium"))
    assert medium == "medium models (in order): codex/gpt-6.1-sol"


def test_digests_separate_models_from_efforts_and_ignore_role_descriptions(tmp_path):
    base = _runner(tmp_path).roster_digests()
    assert _runner(tmp_path).roster_digests() == base
    models = {"low": ["a/one"], "medium": ["a/two"], "high": ["a/three"]}
    moved = _runner(tmp_path, candidates=lambda tier: models[tier]).roster_digests()
    assert moved.models != base.models
    efforts = {"medium": {"codex/gpt-6.1-sol": "high"}}
    tuned = _runner(tmp_path, efforts=efforts).roster_digests()
    assert tuned.models == base.models and tuned.efforts != base.efforts
    _write_role(tmp_path, "newrole", "tiers: [low]\n")
    assert _runner(tmp_path).roster_digests() == base


def test_digest_covers_only_the_tier_lines_the_description_advertises(tmp_path):
    runner = _runner(tmp_path)
    # A budget that clips every tier line advertises none of them.
    assert runner.roster_digests(max_chars=len(ROSTER_CLIP_NOTE) + 10) != runner.roster_digests()


# -- whole-line clipping ----------------------------------------------------


def test_over_budget_roster_keeps_whole_lines_every_role_name_and_a_clip_note_line(tmp_path):
    for index in range(6):
        _write_role(tmp_path, f"role{index}", "tiers: [low, medium]\nreasoning_effort: high\n")
    runner = _runner(tmp_path)
    full = runner.role_index()
    budget = len(full) - 60
    text = runner.role_index(max_chars=budget)
    lines = text.splitlines()
    assert len(text) <= budget
    assert lines[-1] == ROSTER_CLIP_NOTE
    for name in [f"role{i}" for i in range(6)] + ["quick", "scout", "task", "advisor"]:
        assert any(line == name or line.startswith(name + " ") for line in lines), name
    # No line is cut mid-way: each is a whole full-roster line or a reserved role prefix.
    full_lines = full.splitlines()
    for line in lines[:-1]:
        assert (
            not line
            or line in full_lines
            or any(whole.startswith(line + ": ") for whole in full_lines)
        ), line
    assert "role0 [tiers: low (default), medium; effort: high]" in text


def test_tier_lines_drop_whole_from_the_end_but_role_names_stay(tmp_path):
    many = lambda tier: [f"prov/{tier}-model-{n}" for n in range(8)]  # noqa: E731
    runner = _runner(tmp_path, candidates=many)
    text = runner.role_index(max_chars=700)
    lines = text.splitlines()
    assert len(text) <= 700 and lines[-1] == ROSTER_CLIP_NOTE
    tier_lines = [line for line in lines if " models (in order): " in line]
    assert all(line.endswith("model-7") for line in tier_lines)  # whole, never sliced
    assert 0 < len(tier_lines) < 3
    assert {"quick", "scout", "task", "advisor"} <= {line.split(" ")[0] for line in lines}


def test_unclipped_roster_has_no_clip_note(tmp_path):
    assert ROSTER_CLIP_NOTE not in _runner(tmp_path).role_index()


# -- the session notice -----------------------------------------------------


def test_baseline_records_an_event_but_no_message(tmp_path):
    session = SessionManager(tmp_path).open("demo")
    session.append_message(Message(role="user", content=[Text(text="hello")]))
    assert session.note_subagent_roster(_digest("m1", "e1")) is False
    assert _notices(session) == []
    [event] = _notice_events(session)
    assert event.data["kind"] == "subagent_roster" and event.data["baseline"] is True


def test_baseline_and_change_events_reach_the_attached_send_stream(tmp_path):
    from nexus.session.session import _Fanout

    session = SessionManager(tmp_path).open("demo")
    fanout = session._attached_fanout = _Fanout()
    session.note_subagent_roster(_digest("m1", "e1"))
    session.note_subagent_roster(_digest("m2", "e1"))
    streamed = [fanout._items.popleft() for _ in range(len(fanout._items))]
    assert [e.seq for e in streamed] == [e.seq for e in _notice_events(session)]
    assert [e.data.get("baseline") or e.data.get("changed") for e in streamed] == [
        True,
        ["subagent tier models"],
    ]


def test_change_appends_one_precise_tail_notice_without_model_ids(tmp_path):
    session = SessionManager(tmp_path).open("demo")
    session.append_message(Message(role="user", content=[Text(text="hello")]))
    session.note_subagent_roster(_digest("m1", "e1"))
    assert session.note_subagent_roster(_digest("m2", "e1")) is True
    assert session.note_subagent_roster(_digest("m3", "e2")) is True
    assert session.note_subagent_roster(_digest("m3", "e3")) is True
    texts = [m.content[0].text for m in _notices(session)]
    assert texts == [
        NOTICE_TEXT.format(what="subagent tier models"),
        NOTICE_TEXT.format(what="subagent tier models and subagent effort"),
        NOTICE_TEXT.format(what="subagent effort"),
    ]
    assert all("/" not in text for text in texts)  # no model ids
    assert session.messages[-1].meta.notice == "subagent_roster"
    assert [e.data.get("changed") for e in _notice_events(session)][1:] == [
        ["subagent tier models"],
        ["subagent tier models", "subagent effort"],
        ["subagent effort"],
    ]


def test_unchanged_digest_and_resume_do_not_duplicate(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("demo")
    session.append_message(Message(role="user", content=[Text(text="hello")]))
    session.note_subagent_roster(_digest("m1", "e1"))
    session.note_subagent_roster(_digest("m2", "e1"))
    before = len(session.records)
    assert session.note_subagent_roster(_digest("m2", "e1")) is False
    assert len(session.records) == before
    # A fresh handle reads the stored digests from the log instead of re-emitting.
    resumed = SessionManager(tmp_path).open("demo")
    assert resumed.note_subagent_roster(_digest("m2", "e1")) is False
    assert len(resumed.records) == before
    assert resumed.note_subagent_roster(_digest("m2", "e2")) is True
    assert len(_notices(resumed)) == 2


def test_notice_is_not_a_user_prompt_title_or_count(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("demo")
    session.append_message(Message(role="user", content=[Text(text="real prompt")]))
    session.append_message(Message(role="assistant", content=[Text(text="ok")]))
    session.note_subagent_roster(_digest("m1", "e1"))
    session.note_subagent_roster(_digest("m2", "e1"))
    notice = _notices(session)[0]
    assert not is_user_prompt(notice)
    assert derive_title([notice]) == ""
    row = manager.store.session_row("demo")
    assert row["message_count"] == 1
    assert row["last_user"] == "real prompt" and row["title"] == "real prompt"


def test_manager_and_child_sessions_never_get_notices(tmp_path):
    session = SessionManager(tmp_path).open("demo")
    session._kind = "child"
    assert session.note_subagent_roster(_digest("m1", "e1")) is False
    assert _notice_events(session) == []


def test_notice_leaves_the_earlier_history_bytes_unchanged(tmp_path):
    from nexus.model.providers.anthropic import _messages_to_wire

    session = SessionManager(tmp_path).open("demo")
    session.append_message(Message(role="user", content=[Text(text="hello")]))
    session.append_message(Message(role="assistant", content=[Text(text="hi")]))
    session.append_message(Message(role="user", content=[Text(text="next")]))
    before = _messages_to_wire(session.messages)
    session.note_subagent_roster(_digest("m1", "e1"))
    session.note_subagent_roster(_digest("m2", "e1"))
    after = _messages_to_wire(session.messages)
    # Adjacent user messages coalesce; everything before the notice is identical.
    assert after[:-1] == before[:-1]
    assert after[-1]["content"][: len(before[-1]["content"])] == before[-1]["content"]
    assert len(after[-1]["content"]) == len(before[-1]["content"]) + 1


# -- the loop ---------------------------------------------------------------


class _DigestDispatcher(_FakeDispatcher):
    def __init__(self, digests):
        super().__init__()
        self._digests = list(digests)
        self.index = 0

    @property
    def subagent_roster_digest(self):
        digest = self._digests[min(self.index, len(self._digests) - 1)]
        self.index += 1
        return digest


class _NoticeSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.noted: list[tuple[Mapping, int]] = []

    def note_subagent_roster(self, digest):
        self.noted.append((dict(digest), len(self.messages)))
        return False


async def test_loop_notes_the_roster_each_iteration_after_the_prompt_and_before_assembly():
    digests = [_digest("m1", "e1"), _digest("m1", "e1")]
    dispatcher = _DigestDispatcher(digests)
    session, assembler = _NoticeSession(), FakeAssembler()
    provider = ScriptedProvider(tool_response(("c1", "Read", {})), text_response("done"))
    outcome = await run_turn(
        session=session, user_input="go", assemble=assembler,
        provider_for=make_resolver(provider), emit=FakeSink(), lease=FakeLease("t"),
        tools=dispatcher, gate=_FakeGate([("allow", "allow", "ok")]),
    )
    assert outcome.ok
    # Iteration 1 saw the prompt only; iteration 2 saw prompt, tool call and result.
    assert [count for _digest_, count in session.noted] == [1, 3]
    assert len(assembler.calls) == 2


async def test_loop_skips_dispatchers_without_a_roster():
    session = _NoticeSession()
    provider = ScriptedProvider(text_response("done"))
    await run_turn(
        session=session, user_input="go", assemble=FakeAssembler(),
        provider_for=make_resolver(provider), emit=FakeSink(), lease=FakeLease("t"),
        tools=_FakeDispatcher(), gate=_FakeGate([]),
    )
    assert session.noted == []


async def test_midturn_change_is_in_the_very_next_request_after_the_tool_result():
    class ChangingSession(_NoticeSession):
        def note_subagent_roster(self, digest):
            super().note_subagent_roster(digest)
            if digest["efforts"] == "e2":
                self.append_message(Message(
                    role="user", content=[Text(text="notice")],
                    meta=MessageMeta(notice="subagent_roster"),
                ))
            return False

    dispatcher = _DigestDispatcher([_digest("m1", "e1"), _digest("m1", "e2")])
    session, assembler = ChangingSession(), FakeAssembler()
    provider = ScriptedProvider(tool_response(("c1", "Read", {})), text_response("done"))
    outcome = await run_turn(
        session=session, user_input="go", assemble=assembler,
        provider_for=make_resolver(provider), emit=FakeSink(), lease=FakeLease("t"),
        tools=dispatcher, gate=_FakeGate([("allow", "allow", "ok")]),
    )
    assert outcome.ok
    first, second = assembler.calls[0].messages, assembler.calls[1].messages
    assert all(m.meta.notice is None for m in first)
    # The earlier request is an exact prefix of the later one; the notice is last.
    assert second[: len(first)] == first
    assert [m.role for m in second[-2:]] == ["user", "user"]
    assert isinstance(second[-2].content[0], ToolResult)
    assert second[-1].meta.notice == "subagent_roster"


def _adjacent_users():
    notice = Message(
        role="user", content=[Text(text="notice text")],
        meta=MessageMeta(notice="subagent_roster"),
    )
    return [
        Message(role="assistant", content=[Text(text="earlier answer")]),
        Message(role="user", content=[Text(text="the real prompt")]),
        notice,
    ]


def test_opencode_prompt_keeps_the_real_prompt_beside_a_trailing_notice():
    from nexus.model.providers.opencode import render_prompt
    from nexus.model.request import ModelRequest

    rendered = render_prompt(ModelRequest(messages=_adjacent_users()))
    current = rendered.split("Current request:")[1]
    assert "the real prompt" in current and "notice text" in current
    assert "the real prompt" not in rendered.split("Current request:")[0]


def test_anthropic_coalesces_the_notice_into_the_user_turn():
    from nexus.model.providers.anthropic import _messages_to_wire

    wire = _messages_to_wire(_adjacent_users())
    assert [item["role"] for item in wire] == ["assistant", "user"]
    assert [block["text"] for block in wire[-1]["content"]] == ["the real prompt", "notice text"]


def test_context_tail_starts_at_the_real_user_message_not_the_notice():
    from nexus.context.parts import current_user_index

    assert current_user_index(_adjacent_users()) == 1


def test_message_meta_notice_defaults_to_none():
    assert MessageMeta().notice is None


def test_tool_call_nudge_is_atomic_durable_context_not_input(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("demo")
    session.note_tool_call_nudge(103, 100, turn_id="t")
    message = session.messages[-1]
    assert message.role == "user"
    assert message.meta.notice == "tool_call_nudge"
    assert not is_user_prompt(message)
    assert _notice_events(session)[-1].data["threshold"] == 100
    replay = SessionManager(tmp_path).open("demo")
    assert replay.messages[-1] == message
    assert _notice_events(replay)[-1].data["tool_calls"] == 103


def test_tool_call_nudge_config_is_nonnegative():
    import msgspec
    import pytest

    from nexus.config.schema import AgentSection

    assert AgentSection().tool_call_nudge == 100
    assert msgspec.convert({"tool_call_nudge": 0}, type=AgentSection).tool_call_nudge == 0
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"tool_call_nudge": -1}, type=AgentSection)
