"""The Ollama / llama.cpp adapters through the shared conformance harness.

The adapters are added in a separate module (``provider_conformance.ollama``)
so no shared conformance file changes; the same normalized case catalogue and
cross-cutting invariants are then run against them here. Everything is offline
over ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from provider_conformance import (
    CASES,
    assert_case,
    run_all,
    run_case,
    skip_reason,
)
from provider_conformance.contract import (
    ALL_TAGS,
    COUNT_TOKENS,
    EventsStep,
    ParkStep,
    StatusStep,
)
from provider_conformance.events import reply_text
from provider_conformance.ollama import (
    OLLAMA_DIALECT,
    OLLAMA_SPECS,
    OPENAI_DIALECT,
)

from nexus.errors import ProviderError
from nexus.model.capabilities import CAPABILITY_FEATURES, Capabilities
from nexus.model.message import Message, Text
from nexus.model.provider import Provider
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStart

ADAPTER_IDS = [spec.name for spec in OLLAMA_SPECS]
CASE_IDS = [case.id for case in CASES]


def _request() -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content=[Text(text="hi")])])


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", OLLAMA_SPECS, ids=ADAPTER_IDS)
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_conformance_case(spec, case):
    reason = skip_reason(case, spec)
    if reason is not None:
        pytest.skip(reason)
    adapter = spec.make()
    try:
        result = await run_case(adapter, case)
        assert_case(result, case)
    finally:
        await adapter.aclose()


async def test_run_all_report_is_green():
    report = await run_all(OLLAMA_SPECS, CASES)
    assert report.failed == 0, report.format()
    assert report.errors == 0, report.format()
    for spec in OLLAMA_SPECS:
        assert any(
            outcome.status == "pass"
            for outcome in report.by_adapter()[spec.name]
        ), spec.name
    expected_skips = sum(
        skip_reason(case, spec) is not None
        for spec in OLLAMA_SPECS
        for case in CASES
    )
    assert report.skipped == expected_skips
    rendered = report.format()
    for spec in OLLAMA_SPECS:
        assert spec.name in rendered


def test_spec_dialects_and_tags_are_declared():
    for spec in OLLAMA_SPECS:
        assert spec.dialect in {OLLAMA_DIALECT, OPENAI_DIALECT}
        assert spec.tags <= ALL_TAGS
        # The local wire genuinely cannot carry a thinking signature or a
        # document, so those capability tags must not be claimed.
        assert "thinking" not in spec.tags
        assert "documents" not in spec.tags


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", OLLAMA_SPECS, ids=ADAPTER_IDS)
async def test_adapter_is_protocol_compliant(spec):
    adapter = spec.make()
    try:
        provider = adapter.provider
        assert isinstance(provider, Provider)
        assert provider.name == "ollama"
        assert adapter.dialect in {OLLAMA_DIALECT, OPENAI_DIALECT}
        caps = provider.capabilities(adapter.model)
        assert isinstance(caps, Capabilities)
        assert caps.streaming is True
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", OLLAMA_SPECS, ids=ADAPTER_IDS)
async def test_http_adapters_never_reach_the_network(spec):
    adapter = spec.make()
    try:
        client = adapter.provider.transport.client
        assert isinstance(client._transport, httpx.MockTransport)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", OLLAMA_SPECS, ids=ADAPTER_IDS)
async def test_close_is_idempotent_and_blocks_streaming(spec):
    adapter = spec.make()
    provider = adapter.provider
    await adapter.aclose()
    await adapter.aclose()
    with pytest.raises(ProviderError):
        [event async for event in provider.stream(_request())]


@pytest.mark.parametrize("spec", OLLAMA_SPECS, ids=ADAPTER_IDS)
async def test_cancellation_at_a_streaming_wait_point(spec):
    adapter = spec.make()
    try:
        adapter.queue(ParkStep(events=(MessageStart(),)))
        stream = adapter.provider.stream(_request())
        first = await asyncio.wait_for(anext(stream), timeout=2)
        assert isinstance(first, MessageStart)
        await asyncio.wait_for(stream.aclose(), timeout=2)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", OLLAMA_SPECS, ids=ADAPTER_IDS)
async def test_count_tokens_uses_heuristic_fallback(spec):
    # Not tagged COUNT_TOKENS: the local adapter returns None so the caller
    # falls back to the shared heuristic.
    assert COUNT_TOKENS not in spec.tags
    adapter = spec.make()
    try:
        assert await adapter.provider.count_tokens(_request()) is None
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", OLLAMA_SPECS, ids=ADAPTER_IDS)
async def test_declared_degradation_policies_are_valid(spec):
    adapter = spec.make()
    try:
        caps = adapter.provider.capabilities(adapter.model)
        for feature, policy in caps.degradation.items():
            assert feature in CAPABILITY_FEATURES, feature
            assert policy in {"drop", "to_text", "error"}, policy
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", OLLAMA_SPECS, ids=ADAPTER_IDS)
async def test_secrets_are_redacted_in_errors_repr_and_logs(spec, caplog):
    secret = "local-CONFORMANCE-SECRET-0123456789abcdef"
    adapter = spec.make(api_key=secret)
    try:
        adapter.queue(
            StatusStep(
                401,
                body=json.dumps(
                    {"error": {"message": f"bad key {secret}"}}
                ).encode(),
            )
        )
        with caplog.at_level(logging.DEBUG, logger="nexus.model.http"):
            error: BaseException | None = None
            try:
                async for _event in adapter.provider.stream(_request()):
                    pass
            except Exception as exc:  # noqa: BLE001 - the error is the observation
                error = exc
        assert isinstance(error, ProviderError)
        assert secret not in str(error)
        assert secret not in repr(error)
        assert secret not in repr(adapter.provider)
        assert secret not in caplog.text
        header = adapter.captured_requests()[0].headers
        assert header.get(adapter.api_key_header) == f"Bearer {secret}"
    finally:
        await adapter.aclose()


async def test_negative_control_rejects_wrong_output():
    # The suite must fail when an adapter produces the wrong normalized output.
    case = next(case for case in CASES if case.id == "text_only")
    adapter = OLLAMA_SPECS[0].make()
    try:
        adapter.queue(EventsStep(reply_text("not what the case expects")))
        result = await run_case(adapter, case)
        with pytest.raises(AssertionError):
            assert_case(result, case)
    finally:
        await adapter.aclose()
