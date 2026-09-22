import asyncio

from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled


def test_token_starts_uncancelled():
    token = CancelToken()
    assert token.cancelled is False
    assert token.reason is None
    token.raise_if_cancelled()


async def test_cancel_is_idempotent_and_first_reason_wins():
    token = CancelToken()
    token.cancel("first")
    token.cancel("second")
    assert token.cancelled is True
    assert token.reason == "first"
    await asyncio.wait_for(token.wait(), timeout=1)


async def test_token_is_directly_awaitable():
    token = CancelToken()

    async def cancel_soon():
        await asyncio.sleep(0)
        token.cancel("go")

    task = asyncio.create_task(cancel_soon())
    await asyncio.wait_for(token, timeout=1)
    await task
    assert token.reason == "go"


async def test_wait_returns_immediately_when_cancelled():
    token = CancelToken()
    token.cancel()
    await asyncio.wait_for(token.wait(), timeout=1)


def test_raise_if_cancelled_propagates_reason():
    token = CancelToken()
    token.cancel("stop now")
    try:
        token.raise_if_cancelled()
        assert False
    except OperationCancelled as exc:
        assert "stop now" in str(exc)
