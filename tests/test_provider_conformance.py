"""Provider conformance harness: the pytest entry point (plan section 9).

Three layers run here:

1. **The matrix** — every ``(adapter, case)`` pair from the reusable catalogue,
   offline, over a mock/in-process transport. Cases an adapter lacks the
   capability or dialect for are reported as skips, never silently dropped.
2. **Dialect neutrality** — normalized semantic cases run on every dialect,
   while wire-shape assertions are gated to a dialect or supplied as a
   per-dialect overlay. This is asserted structurally, not by convention.
3. **Cross-cutting invariants** — protocol compliance, a mock-only transport,
   idempotent close, cancellation at a streaming wait point, count_tokens,
   secret redaction, and valid declared degradation policies. These cannot be
   expressed as a single normalized event plan, so they live here and are
   parametrized over the same adapter specs.

The harness package is importable because pytest prepends the ``tests/``
directory to ``sys.path``; ``provider_conformance`` is a sibling package.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from provider_conformance import (
    ADAPTER_SPECS,
    ALL_DIALECTS,
    CASES,
    AnthropicAdapter,
    GeminiAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    ScriptedAdapter,
    assert_case,
    run_all,
    run_case,
    skip_reason,
)
from provider_conformance.contract import (
    ALL_TAGS,
    COUNT_TOKENS,
    HTTP,
    SECRETS,
    EventsStep,
    ParkStep,
    StatusStep,
)
from provider_conformance.events import reply_text

from nexus.errors import ProviderError
from nexus.model.capabilities import CAPABILITY_FEATURES, Capabilities
from nexus.model.message import Message, Text
from nexus.model.provider import Provider
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStart, TextDelta

ADAPTER_IDS = [spec.name for spec in ADAPTER_SPECS]
CASE_IDS = [case.id for case in CASES]


def _request() -> ModelRequest:
    return ModelRequest(
        messages=[Message(role="user", content=[Text(text="hi")])]
    )


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ADAPTER_SPECS, ids=ADAPTER_IDS)
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


async def test_run_all_report_is_green_and_complete():
    report = await run_all()
    assert report.failed == 0, report.format()
    assert report.errors == 0, report.format()
    # Every adapter must pass at least one case and skip only by declared
    # dialect/capability gates.
    for adapter, outcomes in report.by_adapter().items():
        assert any(o.status == "pass" for o in outcomes), adapter
    expected_skips = sum(
        skip_reason(case, spec) is not None
        for spec in ADAPTER_SPECS
        for case in CASES
    )
    assert report.skipped == expected_skips
    rendered = report.format()
    assert "provider conformance report" in rendered
    for spec in ADAPTER_SPECS:
        assert spec.name in rendered


async def test_harness_rejects_a_non_conforming_stream():
    # Negative control: the suite must fail when an adapter produces the wrong
    # normalized output, or every green run would be meaningless.
    case = next(case for case in CASES if case.id == "text_only")
    adapter = ScriptedAdapter()
    try:
        adapter.queue(EventsStep(reply_text("not what the case expects")))
        result = await run_case(adapter, case)
        with pytest.raises(AssertionError):
            assert_case(result, case)
    finally:
        await adapter.aclose()


async def test_scripted_queue_after_provider_access():
    # Regression: the provider used to be built lazily from a snapshot, so a
    # script queued after the first ``provider`` access was silently ignored.
    adapter = ScriptedAdapter()
    provider = adapter.provider  # touch it first
    adapter.queue(EventsStep(reply_text("late")))
    try:
        events = [event async for event in provider.stream(_request())]
    finally:
        await adapter.aclose()
    assert any(isinstance(e, TextDelta) and e.text == "late" for e in events)


def test_case_catalogue_is_well_formed():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))
    for case in CASES:
        assert case.requires <= ALL_TAGS, case.id
        assert callable(case.request), case.id
        assert case.wire or case.check, case.id
        if case.dialects is not None:
            assert case.dialects <= ALL_DIALECTS, case.id


def test_wire_shape_assertions_are_dialect_gated():
    # A case that pins an exact wire body must be gated to a dialect or move the
    # assertion into a per-dialect overlay. This is what keeps Anthropic bodies
    # out of Gemini's run and vice versa.
    for case in CASES:
        if case.expect is not None and case.expect.request_contains is not None:
            assert case.dialects is not None, case.id
    normalized = [case for case in CASES if case.dialects is None]
    gated = [case for case in CASES if case.dialects is not None]
    assert normalized and gated


def test_dialect_overlays_target_known_dialects():
    for case in CASES:
        for dialect in case.overlays:
            assert dialect in ALL_DIALECTS, (case.id, dialect)


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ADAPTER_SPECS, ids=ADAPTER_IDS)
async def test_adapter_is_protocol_compliant(spec):
    adapter = spec.make()
    try:
        provider = adapter.provider
        assert isinstance(provider, Provider)
        assert provider.name
        assert adapter.dialect == spec.dialect
        assert adapter.dialect in ALL_DIALECTS
        caps = provider.capabilities(adapter.model)
        assert isinstance(caps, Capabilities)
        assert caps.streaming is True
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", ADAPTER_SPECS, ids=ADAPTER_IDS)
async def test_http_adapters_never_reach_the_network(spec):
    if HTTP not in spec.tags:
        pytest.skip(f"{spec.name} is in-process and has no HTTP transport")
    adapter = spec.make()
    try:
        client = adapter.provider.transport.client
        assert isinstance(client._transport, httpx.MockTransport)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", ADAPTER_SPECS, ids=ADAPTER_IDS)
async def test_close_is_idempotent_and_blocks_streaming(spec):
    adapter = spec.make()
    provider = adapter.provider
    await adapter.aclose()
    await adapter.aclose()
    with pytest.raises(ProviderError):
        [event async for event in provider.stream(_request())]


@pytest.mark.parametrize("spec", ADAPTER_SPECS, ids=ADAPTER_IDS)
async def test_cancellation_at_a_streaming_wait_point(spec):
    adapter = spec.make()
    try:
        adapter.queue(ParkStep(events=(MessageStart(),)))
        stream = adapter.provider.stream(_request())
        first = await asyncio.wait_for(anext(stream), timeout=2)
        assert isinstance(first, MessageStart)
        # Closing the consumer must release the parked stream, not hang.
        await asyncio.wait_for(stream.aclose(), timeout=2)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", ADAPTER_SPECS, ids=ADAPTER_IDS)
async def test_count_tokens_roundtrip(spec):
    if COUNT_TOKENS not in spec.tags:
        pytest.skip(f"{spec.name} has no count_tokens endpoint")
    adapter = spec.make()
    try:
        adapter.queue_count_tokens(42)
        assert await adapter.provider.count_tokens(_request()) == 42
        adapter.queue_count_tokens(None)
        assert await adapter.provider.count_tokens(_request()) is None
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("spec", ADAPTER_SPECS, ids=ADAPTER_IDS)
async def test_declared_degradation_policies_are_valid(spec):
    adapter = spec.make()
    try:
        caps = adapter.provider.capabilities(adapter.model)
        for feature, policy in caps.degradation.items():
            assert feature in CAPABILITY_FEATURES, feature
            assert policy in {"drop", "to_text", "error"}, policy
    finally:
        await adapter.aclose()


def _secret_adapter(spec, secret: str):
    if spec.name == "anthropic":
        return AnthropicAdapter(api_key=secret)
    if spec.name == "gemini":
        return GeminiAdapter(api_key=secret)
    if spec.name == "openai_responses":
        return OpenAIResponsesAdapter(api_key=secret)
    if spec.name == "openai_chat":
        return OpenAIChatAdapter(api_key=secret)
    if spec.name == "ollama":
        from provider_conformance.ollama import OllamaAdapter

        return OllamaAdapter(api_key=secret)
    if spec.name == "ollama-openai":
        from provider_conformance.ollama import OllamaOpenAIAdapter

        return OllamaOpenAIAdapter(api_key=secret)
    raise AssertionError(f"no secret-capable factory for {spec.name}")


@pytest.mark.parametrize("spec", ADAPTER_SPECS, ids=ADAPTER_IDS)
async def test_secrets_are_redacted_in_errors_repr_and_logs(spec, caplog):
    if SECRETS not in spec.tags:
        pytest.skip(f"{spec.name} does not carry credentials")
    secret = "sk-ant-CONFORMANCE-SECRET-0123456789abcdef"
    adapter = _secret_adapter(spec, secret)
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
        # The credential really was in play: it reached the wire as a header.
        # OpenAI uses a bearer Authorization header; the others send it raw.
        header = adapter.captured_requests()[0].headers
        value = header.get(adapter.api_key_header, "")
        assert value == secret or value == f"Bearer {secret}"
    finally:
        await adapter.aclose()
