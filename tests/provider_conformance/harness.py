"""The conformance runner, expectation assertions, and report API.

``run_case`` drives one adapter through one case and returns a
:class:`CaseResult`. ``assert_case`` checks the declared expectation and any
custom check. ``run_all`` runs the full adapter x case matrix, skipping cases an
adapter does not declare the capability for, and returns a
:class:`ConformanceReport` that prints as a matrix.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from nexus.model.stream import MessageStart, StreamEvent, TextDelta

from .adapters import ADAPTER_SPECS
from .cases import CASES, ConformanceCase, Expectation
from .contract import AdapterSpec, AdapterUnderTest, CapturedRequest
from .events import (
    event_types,
    is_subset,
    signatures_of,
    stop_reason_of,
    text_of,
    thinking_of,
    tool_calls_of,
    usage_of,
)

__all__ = [
    "CaseOutcome",
    "CaseResult",
    "ConformanceReport",
    "assert_case",
    "run_all",
    "run_case",
    "skip_reason",
]


@dataclass
class CaseResult:
    """Everything observable about one run of one case on one adapter."""

    case: ConformanceCase
    adapter: AdapterUnderTest
    events: list[StreamEvent]
    error: BaseException | None
    requests: list[CapturedRequest]
    retry_delays: tuple[float, ...]

    def text(self) -> str:
        return text_of(self.events)

    def text_chunks(self) -> list[str]:
        return [e.text for e in self.events if isinstance(e, TextDelta)]

    def thinking(self) -> list[str]:
        return thinking_of(self.events)

    def signatures(self) -> list[str]:
        return signatures_of(self.events)

    def tool_calls(self) -> list[tuple[str, str, dict[str, Any]]]:
        return tool_calls_of(self.events)

    def usage(self):
        return usage_of(self.events)

    def stop_reason(self) -> str | None:
        return stop_reason_of(self.events)

    def event_types(self) -> list[str]:
        return event_types(self.events)

    def last_request_json(self) -> dict[str, Any] | None:
        for request in reversed(self.requests):
            if request.json is not None:
                return request.json
        return None


async def run_case(
    adapter: AdapterUnderTest, case: ConformanceCase
) -> CaseResult:
    """Queue the case's wire steps, stream once, and collect the outcome."""
    for step in case.wire:
        adapter.queue(step)
    events: list[StreamEvent] = []
    error: BaseException | None = None
    try:
        async for event in adapter.provider.stream(case.request()):
            events.append(event)
    except Exception as exc:  # noqa: BLE001 - the error itself is the observation
        error = exc
    return CaseResult(
        case=case,
        adapter=adapter,
        events=events,
        error=error,
        requests=adapter.captured_requests(),
        retry_delays=tuple(adapter.retry_delays),
    )


def _assert_expectation(result: CaseResult, expect: Expectation) -> None:
    if expect.error is not None:
        assert result.error is not None, (
            f"expected {expect.error.__name__}, stream completed normally"
        )
        assert isinstance(result.error, expect.error), (
            f"expected {expect.error.__name__}, got "
            f"{type(result.error).__name__}: {result.error}"
        )
        if expect.error_match:
            assert expect.error_match in str(result.error), str(result.error)
    elif result.error is not None:
        raise AssertionError(
            f"unexpected {type(result.error).__name__}: {result.error}"
        )

    if expect.text is not None:
        assert result.text() == expect.text, (result.text(), expect.text)
    if expect.text_chunks is not None:
        assert tuple(result.text_chunks()) == expect.text_chunks
    if expect.thinking is not None:
        assert tuple(result.thinking()) == expect.thinking
    if expect.signatures is not None:
        assert tuple(result.signatures()) == expect.signatures
    if expect.tool_calls is not None:
        expected = [tuple(call) for call in expect.tool_calls]
        assert result.tool_calls() == expected, (result.tool_calls(), expected)
    if expect.tool_calls_loose is not None:
        actual = [(name, data) for _, name, data in result.tool_calls()]
        expected = [tuple(call) for call in expect.tool_calls_loose]
        assert actual == expected, (actual, expected)
    if expect.usage is not None:
        assert result.usage() == expect.usage, (result.usage(), expect.usage)
    if expect.stop_reason is not None:
        assert result.stop_reason() == expect.stop_reason, result.stop_reason()
    if expect.requests is not None:
        assert len(result.requests) == expect.requests, [
            r.path or "<in-process>" for r in result.requests
        ]
    if expect.retry_delays is not None:
        assert result.retry_delays == expect.retry_delays, result.retry_delays
    if expect.saw_message_start is not None:
        saw = any(isinstance(e, MessageStart) for e in result.events)
        assert saw is expect.saw_message_start, saw
    if expect.request_contains is not None:
        body = result.last_request_json()
        assert body is not None, "adapter did not expose a wire request body"
        assert is_subset(expect.request_contains, body), json.dumps(
            body, indent=2, sort_keys=True
        )
    if expect.request_excludes:
        body = result.last_request_json()
        assert body is not None, "adapter did not expose a wire request body"
        dumped = json.dumps(body)
        for needle in expect.request_excludes:
            assert needle not in dumped, f"{needle!r} unexpectedly present"


def assert_case(result: CaseResult, case: ConformanceCase) -> None:
    """Assert the shared expectation, then the dialect overlay, then any check."""
    if case.expect is not None:
        _assert_expectation(result, case.expect)
    overlay = case.overlays.get(result.adapter.dialect)
    if overlay is not None:
        _assert_expectation(result, overlay)
    if case.check is not None:
        case.check(result)


def skip_reason(case: ConformanceCase, spec: AdapterSpec) -> str | None:
    """Why ``case`` does not apply to ``spec``, or ``None`` if it does.

    Two independent gates: the adapter's wire *dialect* must be one the case is
    written for, and the adapter must declare every *capability* the case
    requires. A skip is always reported with its reason.
    """
    if not case.applies_to_dialect(spec.dialect):
        dialects = ", ".join(sorted(case.dialects or ()))
        return f"dialect {spec.dialect!r} not in {{{dialects}}}"
    missing = case.requires - spec.tags
    if missing:
        return f"missing tags: {', '.join(sorted(missing))}"
    return None


# ---------------------------------------------------------------------------
# Report API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseOutcome:
    adapter: str
    case: str
    status: str  # "pass" | "fail" | "error" | "skip"
    detail: str = ""


@dataclass
class ConformanceReport:
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(o.status == "pass" for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(o.status == "fail" for o in self.outcomes)

    @property
    def errors(self) -> int:
        return sum(o.status == "error" for o in self.outcomes)

    @property
    def skipped(self) -> int:
        return sum(o.status == "skip" for o in self.outcomes)

    def by_adapter(self) -> dict[str, list[CaseOutcome]]:
        grouped: dict[str, list[CaseOutcome]] = {}
        for outcome in self.outcomes:
            grouped.setdefault(outcome.adapter, []).append(outcome)
        return grouped

    def format(self) -> str:
        lines = ["provider conformance report", "=" * 60]
        for adapter, outcomes in self.by_adapter().items():
            summary = {
                status: sum(o.status == status for o in outcomes)
                for status in ("pass", "fail", "error", "skip")
            }
            lines.append(
                f"\n{adapter}: "
                f"{summary['pass']} pass, {summary['fail']} fail, "
                f"{summary['error']} error, {summary['skip']} skip"
            )
            for outcome in outcomes:
                marker = {
                    "pass": "PASS",
                    "fail": "FAIL",
                    "error": "ERR ",
                    "skip": "skip",
                }[outcome.status]
                suffix = f"  ({outcome.detail})" if outcome.detail else ""
                lines.append(f"  [{marker}] {outcome.case}{suffix}")
        lines.append(
            f"\ntotal: {self.passed} pass, {self.failed} fail, "
            f"{self.errors} error, {self.skipped} skip"
        )
        return "\n".join(lines)


async def run_all(
    specs: tuple[AdapterSpec, ...] = ADAPTER_SPECS,
    cases: tuple[ConformanceCase, ...] = CASES,
) -> ConformanceReport:
    """Run the full adapter x case matrix and return a report."""
    report = ConformanceReport()
    for spec in specs:
        for case in cases:
            reason = skip_reason(case, spec)
            if reason is not None:
                report.outcomes.append(
                    CaseOutcome(spec.name, case.id, "skip", reason)
                )
                continue
            adapter = spec.make()
            try:
                result = await run_case(adapter, case)
                assert_case(result, case)
            except AssertionError as exc:
                report.outcomes.append(
                    CaseOutcome(spec.name, case.id, "fail", str(exc))
                )
            except Exception as exc:  # noqa: BLE001 - report, never crash the suite
                report.outcomes.append(
                    CaseOutcome(
                        spec.name,
                        case.id,
                        "error",
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                report.outcomes.append(CaseOutcome(spec.name, case.id, "pass"))
            finally:
                await adapter.aclose()
    return report
