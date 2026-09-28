from __future__ import annotations

import asyncio
import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled
from nexus.tools.questions import (
    QuestionAnswerError,
    QuestionBroker,
    QuestionBrokerError,
    QuestionChoice,
    QuestionRequest,
    QuestionTimeout,
)


def request_args(**overrides):
    return {
        "root_session_id": "root",
        "source_session_id": "root",
        "turn_id": "turn-1",
        "agent_id": "root-agent",
        "call_id": "call-1",
        "prompt": "What should happen next?",
        **overrides,
    }


async def wait_pending(broker: QuestionBroker, count: int = 1):
    async def wait():
        while len(broker.pending) != count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1)
    return broker.pending


async def test_sibling_questions_have_unique_ids_and_resolve_per_root():
    broker = QuestionBroker(timeout_s=1)
    root_waiter = asyncio.create_task(broker.request(**request_args()))
    child_waiter = asyncio.create_task(
        broker.request(
            **request_args(
                source_session_id="child-session",
                agent_id="child-agent",
                call_id="child-call",
            )
        )
    )
    root_request, child_request = await wait_pending(broker, 2)

    assert root_request.root_session_id == child_request.root_session_id == "root"
    assert root_request.source_session_id == "root"
    assert child_request.source_session_id == "child-session"
    assert root_request.question_id != child_request.question_id
    assert uuid.UUID(root_request.question_id).version == 4
    with pytest.raises(FrozenInstanceError):
        root_request.prompt = "mutated"

    assert await broker.resolve("root", child_request.question_id, "child answer")
    assert await broker.resolve("root", root_request.question_id, "root answer")
    assert await asyncio.gather(root_waiter, child_waiter) == [
        "root answer",
        "child answer",
    ]
    assert broker.pending == ()


async def test_wrong_root_stale_id_and_first_answer_wins():
    broker = QuestionBroker(timeout_s=1)
    waiter = asyncio.create_task(broker.request(**request_args()))
    (request,) = await wait_pending(broker)

    assert await broker.resolve("other-root", request.id, "wrong") is False
    assert await broker.resolve("root", "not-a-live-id", "stale") is False
    assert await broker.resolve("root", request.id, "first") is True
    assert await broker.resolve("root", request.id, "second") is False
    assert await waiter == "first"


async def test_choice_answers_must_use_exact_stable_choice_id():
    broker = QuestionBroker(timeout_s=1)
    choices = (QuestionChoice("keep", "Keep it"), QuestionChoice("remove", "Remove it"))
    waiter = asyncio.create_task(
        broker.request(**request_args(choices=choices, max_length=1))
    )
    (request,) = await wait_pending(broker)
    assert request.to_dict()["choices"] == [
        {"id": "keep", "label": "Keep it"},
        {"id": "remove", "label": "Remove it"},
    ]

    with pytest.raises(QuestionAnswerError):
        await broker.resolve("root", request.id, "Keep it")
    with pytest.raises(QuestionAnswerError):
        await broker.resolve("root", request.id, "unknown")
    assert await broker.resolve("root", request.id, "keep")
    assert await waiter == "keep"


async def test_choice_and_free_text_constraints_are_validated():
    with pytest.raises(QuestionBrokerError):
        QuestionChoice("bad id", "Bad")
    with pytest.raises(QuestionBrokerError):
        QuestionRequest(
            question_id=str(uuid.uuid4()),
            root_session_id="root",
            source_session_id="root",
            turn_id="turn",
            agent_id="agent",
            call_id="call",
            prompt="Choose",
            choices=(QuestionChoice("same", "One"), QuestionChoice("same", "Two")),
            created_at=datetime.now(UTC),
            deadline=datetime.now(UTC) + timedelta(seconds=1),
        )

    broker = QuestionBroker(timeout_s=1)
    waiter = asyncio.create_task(
        broker.request(
            **request_args(choices=(), max_length=5, pattern=r"[A-Z]{1,5}")
        )
    )
    (request,) = await wait_pending(broker)
    with pytest.raises(QuestionAnswerError):
        await broker.resolve("root", request.id, "toolong")
    with pytest.raises(QuestionAnswerError):
        await broker.resolve("root", request.id, "lower")
    with pytest.raises(QuestionAnswerError):
        await broker.resolve("root", request.id, "A\x00")
    assert await broker.resolve("root", request.id, "OK")
    assert await waiter == "OK"


async def test_request_and_resolve_events_are_durable_boundaries():
    events = []

    async def emit(event_type, data):
        await asyncio.sleep(0)
        events.append((event_type, data))

    broker = QuestionBroker(timeout_s=1, emit=emit)
    waiter = asyncio.create_task(broker.request(**request_args()))
    (request,) = await wait_pending(broker)
    assert events[0] == ("question.requested", request.to_dict())

    assert await broker.resolve("root", request.id, "answer")
    assert events[1] == (
        "question.resolved",
        {
            "question_id": request.id,
            "root_session_id": "root",
            "answer": "answer",
            "answer_label": "answer",
        },
    )
    assert await waiter == "answer"


async def test_emit_failures_do_not_publish_or_claim_resolution():
    fail_request = True
    fail_resolution = False

    async def emit(event_type, _data):
        if (event_type == "question.requested" and fail_request) or (
            event_type == "question.resolved" and fail_resolution
        ):
            raise RuntimeError("durable sink unavailable")

    broker = QuestionBroker(timeout_s=1, emit=emit)
    with pytest.raises(RuntimeError, match="durable sink"):
        await broker.request(**request_args())
    assert broker.pending == ()

    fail_request = False
    waiter = asyncio.create_task(broker.request(**request_args()))
    (request,) = await wait_pending(broker)
    fail_resolution = True
    with pytest.raises(RuntimeError, match="durable sink"):
        await broker.resolve("root", request.id, "answer")
    assert broker.pending == (request,)
    fail_resolution = False
    assert await broker.resolve("root", request.id, "answer")
    assert await waiter == "answer"


async def test_cancel_one_and_cancel_session_wake_only_owned_waiters():
    broker = QuestionBroker(timeout_s=1)
    first = asyncio.create_task(broker.request(**request_args()))
    sibling = asyncio.create_task(
        broker.request(**request_args(call_id="root-sibling"))
    )
    second = asyncio.create_task(
        broker.request(**request_args(root_session_id="other", source_session_id="other"))
    )
    pending = await wait_pending(broker, 3)
    root_request = next(item for item in pending if item.root_session_id == "root")

    assert await broker.cancel("other", root_request.id) is False
    assert await broker.cancel("root", root_request.id, reason="closed") is True
    with pytest.raises(OperationCancelled, match="closed"):
        await first

    assert await broker.cancel_session("root", reason="root shutdown") == 1
    with pytest.raises(OperationCancelled, match="root shutdown"):
        await sibling
    other_request = next(item for item in broker.pending if item.root_session_id == "other")
    assert await broker.cancel_session("other", reason="shutdown") == 1
    with pytest.raises(OperationCancelled, match="shutdown"):
        await second
    assert broker.pending == ()
    assert other_request.root_session_id == "other"


async def test_cooperative_cancellation_timeout_and_waiter_cancellation_cleanup():
    broker = QuestionBroker(timeout_s=0.03)
    token = CancelToken()
    cancelled_waiter = asyncio.create_task(
        broker.request(**request_args(call_id="cancelled"), cancel=token)
    )
    await wait_pending(broker)
    token.cancel("turn stopped")
    with pytest.raises(OperationCancelled, match="turn stopped"):
        await cancelled_waiter
    assert broker.pending == ()

    with pytest.raises(QuestionTimeout):
        await broker.request(**request_args(call_id="unattended"))
    assert broker.pending == ()

    task = asyncio.create_task(broker.request(**request_args(call_id="task-cancel")))
    await wait_pending(broker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.pending == ()


async def test_simulated_id_collision_retries_without_overwriting_waiter():
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    ids = iter((first_id, first_id, second_id))
    broker = QuestionBroker(timeout_s=1, id_factory=lambda: next(ids))
    first = asyncio.create_task(broker.request(**request_args(call_id="one")))
    (first_request,) = await wait_pending(broker)
    second = asyncio.create_task(broker.request(**request_args(call_id="two")))
    pending = await wait_pending(broker, 2)
    second_request = next(item for item in pending if item.call_id == "two")
    assert first_request.question_id == str(first_id)
    assert second_request.question_id == str(second_id)

    await broker.cancel_session("root")
    await asyncio.gather(first, second, return_exceptions=True)
