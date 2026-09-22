"""Phase 3 async summarizer support (review fix 4).

A summarizer may be synchronous or asynchronous. Async summaries run on the
async assembly path without blocking the loop; failures and cancellation are
actionable and never leave a partial artifact.
"""
from __future__ import annotations

import asyncio

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection, ModelSection
from nexus.context import ContextManager
from nexus.context.compact import SummaryArtifact
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.session.manager import SessionManager


class AsyncSummarizer:
    def __init__(self):
        self.calls = 0

    async def summarize(self, messages):
        self.calls += 1
        await asyncio.sleep(0)
        return SummaryArtifact(text="ASYNC-SUM", source_count=len(messages), tokens=3)


class SyncSummarizer:
    def __init__(self):
        self.calls = 0

    def summarize(self, messages):
        self.calls += 1
        return SummaryArtifact(text="SYNC-SUM", source_count=len(messages), tokens=3)


class ErrorSummarizer:
    async def summarize(self, messages):
        raise RuntimeError("summarizer model down")


class CancellingSummarizer:
    def __init__(self):
        self.started = asyncio.Event()

    async def summarize(self, messages):
        self.started.set()
        await asyncio.sleep(3600)


class SyncReturningAwaitable:
    """A sync ``summarize`` method that returns an awaitable (not ``async def``)."""

    def __init__(self):
        self.calls = 0

    def summarize(self, messages):
        self.calls += 1
        return self._impl(messages)

    async def _impl(self, messages):
        await asyncio.sleep(0)
        return SummaryArtifact(text="SYNC-AWAIT", source_count=len(messages), tokens=3)


class SyncReturningError:
    def summarize(self, messages):
        return self._impl()

    async def _impl(self):
        raise RuntimeError("sync-await model down")


class SyncReturningCancellation:
    def __init__(self):
        self.started = asyncio.Event()

    def summarize(self, messages):
        return self._impl()

    async def _impl(self):
        self.started.set()
        await asyncio.sleep(3600)


def _config(max_tokens=400, strategy="summarize"):
    return Config(
        model="scripted/x",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/x"),
            context=ContextSection(
                max_tokens=max_tokens,
                safety_margin_tokens=0,
                compaction=strategy,
            ),
        ),
    )


def _session(tmp_path, context, name="s"):
    session = SessionManager(tmp_path, assemble=context).open(name)
    for index in range(8):
        role = "user" if index % 2 == 0 else "assistant"
        session.append_message(
            Message(role=role, content=[Text(text=f"m{index} " * 40)])
        )
    session.append_message(Message(role="user", content=[Text(text="current")]))
    return session


def test_async_summarizer_returns_awaitable_and_works(tmp_path):
    summarizer = AsyncSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = _session(tmp_path, context)

    result = context.for_turn().assemble(session)
    assert hasattr(result, "__await__")
    request = asyncio.run(result)
    assert "ASYNC-SUM" in request.messages[0].content[0].text
    assert summarizer.calls == 1
    assert len(session.summaries) == 1


def test_sync_summarizer_still_supported(tmp_path):
    summarizer = SyncSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = _session(tmp_path, context)
    request = context.for_turn().assemble(session)
    assert isinstance(request, object)
    assert "SYNC-SUM" in request.messages[0].content[0].text


def test_async_summarizer_error_is_actionable_and_appends_nothing(tmp_path):
    context = ContextManager(
        tmp_path, config=_config(), summarizer=ErrorSummarizer(), counter=len
    )
    session = _session(tmp_path, context, name="err")

    async def run():
        await context.for_turn().assemble(session)

    with pytest.raises(RuntimeError, match="model down"):
        asyncio.run(run())
    assert session.summaries == []


def test_async_summarizer_cancellation_appends_nothing(tmp_path):
    summarizer = CancellingSummarizer()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = _session(tmp_path, context, name="cancel")

    async def run():
        task = asyncio.ensure_future(context.for_turn().assemble(session))
        await summarizer.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert session.summaries == []


def test_sync_summarizer_returning_awaitable_is_awaited(tmp_path):
    summarizer = SyncReturningAwaitable()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = _session(tmp_path, context, name="sync-await")

    result = context.for_turn().assemble(session)
    assert hasattr(result, "__await__")  # returned to the caller, not a TypeError
    request = asyncio.run(result)
    assert "SYNC-AWAIT" in request.messages[0].content[0].text
    assert summarizer.calls == 1
    assert len(session.summaries) == 1


def test_sync_summarizer_returning_error_is_actionable_without_artifact(tmp_path):
    context = ContextManager(
        tmp_path, config=_config(), summarizer=SyncReturningError(), counter=len
    )
    session = _session(tmp_path, context, name="sync-err")

    async def run():
        await context.for_turn().assemble(session)

    with pytest.raises(RuntimeError, match="sync-await model down"):
        asyncio.run(run())
    assert session.summaries == []


def test_sync_summarizer_returning_cancellation_appends_nothing(tmp_path):
    summarizer = SyncReturningCancellation()
    context = ContextManager(
        tmp_path, config=_config(), summarizer=summarizer, counter=len
    )
    session = _session(tmp_path, context, name="sync-cancel")

    async def run():
        task = asyncio.ensure_future(context.for_turn().assemble(session))
        await summarizer.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert session.summaries == []


# ---------------------------------------------------------------------------
# Summarizer failure policy (review blocker 6)
# ---------------------------------------------------------------------------


class FailingSyncSummarizer:
    def summarize(self, messages):
        raise RuntimeError("sync summarizer failed")


class FailingAsyncSummarizer:
    async def summarize(self, messages):
        raise RuntimeError("async summarizer failed")


class CancellingHybridSummarizer:
    def __init__(self):
        self.started = asyncio.Event()

    async def summarize(self, messages):
        self.started.set()
        await asyncio.sleep(3600)


def _tool_session(tmp_path, context, name):
    session = SessionManager(tmp_path, assemble=context).open(name)
    for index in range(4):
        session.append_message(
            Message(
                role="assistant",
                content=[ToolUse(id=f"c{index}", name="Read", input={"path": "a"})],
            )
        )
        session.append_message(
            Message(
                role="user",
                content=[
                    ToolResult(
                        tool_use_id=f"c{index}",
                        content=[Text(text="R" * 3000)],
                        context_note=f"[note {index}]",
                    )
                ],
            )
        )
    session.append_message(Message(role="user", content=[Text(text="current")]))
    return session


def test_hybrid_sync_summarizer_failure_degrades_to_eviction_drop(tmp_path):
    context = ContextManager(
        tmp_path,
        config=_config(max_tokens=200, strategy="hybrid"),
        summarizer=FailingSyncSummarizer(),
        counter=len,
        identity="I",
        keep_recent=0,
    )
    session = _tool_session(tmp_path, context, "hybrid-sync-fail")

    request = context.for_turn().assemble(session)
    ctx = request.metadata["context"]
    assert ctx["compacted"] is None
    assert ctx["summary_reused"] is None
    assert ctx["evicted"] > 0
    assert ctx["history_dropped"] > 0
    assert session.summaries == []
    # The degraded request still fits and carries evicted content.
    assert ctx["used_tokens"] <= ctx["input_budget"]


def test_hybrid_async_summarizer_failure_degrades_to_eviction_drop(tmp_path):
    context = ContextManager(
        tmp_path,
        config=_config(max_tokens=200, strategy="hybrid"),
        summarizer=FailingAsyncSummarizer(),
        counter=len,
        identity="I",
        keep_recent=0,
    )
    session = _tool_session(tmp_path, context, "hybrid-async-fail")

    request = asyncio.run(context.for_turn().assemble(session))
    ctx = request.metadata["context"]
    assert ctx["compacted"] is None
    assert ctx["summary_reused"] is None
    assert ctx["evicted"] > 0
    assert ctx["history_dropped"] > 0
    assert session.summaries == []


def test_summarize_strategy_failure_is_actionable(tmp_path):
    context = ContextManager(
        tmp_path,
        config=_config(max_tokens=200, strategy="summarize"),
        summarizer=FailingAsyncSummarizer(),
        counter=len,
        identity="I",
        keep_recent=0,
    )
    session = _tool_session(tmp_path, context, "summarize-fail")

    async def run():
        await context.for_turn().assemble(session)

    with pytest.raises(RuntimeError, match="async summarizer failed"):
        asyncio.run(run())
    assert session.summaries == []


def test_hybrid_cancellation_always_propagates(tmp_path):
    summarizer = CancellingHybridSummarizer()
    context = ContextManager(
        tmp_path,
        config=_config(max_tokens=200, strategy="hybrid"),
        summarizer=summarizer,
        counter=len,
        identity="I",
        keep_recent=0,
    )
    session = _tool_session(tmp_path, context, "hybrid-cancel")

    async def run():
        task = asyncio.ensure_future(context.for_turn().assemble(session))
        await summarizer.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert session.summaries == []
