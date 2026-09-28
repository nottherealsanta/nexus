"""Token budget accounting and the priority allocation algorithm (plan 5.2).

The budget is deliberately simple and **advisory**. Its job is to decide how
much of each part fits, not to fail a turn because an estimate was a few percent
off. Concretely:

1. ``input_budget = min(config.max_tokens, caps.max_context_tokens)
   - effective_max_output_tokens - safety_margin``, further capped at
   ``caps.max_input_tokens - safety_margin`` when the catalogue states a
   separate prompt limit (models.dev ``limit.input``).
   A capability ceiling is ignored when it is unknown (``<= 0``), so a
   provider that does not advertise a limit falls back to configuration.
2. Priority-0 parts (identity, soul, tools, the current user turn) are required.
   If they alone exceed the budget the turn fails with an actionable error that
   names every oversized part and its token count.
3. Lower priorities are allocated in ascending priority then fixed assembly
   order, each bounded by its configured cap and the remaining budget.
4. ``history`` receives whatever remains and compacts itself to fit.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import NexusError

__all__ = [
    "Allocation",
    "BudgetInputs",
    "BudgetPlan",
    "ContextOverflow",
    "PartRequest",
    "allocate",
    "compute_input_budget",
    "effective_max_output_tokens",
    "fit_suffix_count",
    "part_caps",
]
class ContextOverflow(NexusError):
    """Required (priority 0) context does not fit the input budget."""
def effective_max_output_tokens(
    config_max_output_tokens: int | None, provider_default_max_output_tokens: int | None, caps_max_output_tokens: int | None,
) -> int:
    requested = config_max_output_tokens if type(config_max_output_tokens) is int and config_max_output_tokens > 0 else provider_default_max_output_tokens
    if type(requested) is not int or requested <= 0:
        return 0
    if type(caps_max_output_tokens) is int and caps_max_output_tokens > 0:
        return min(requested, caps_max_output_tokens)
    return requested
def compute_input_budget(
    config_max_tokens: int,
    caps_max_context_tokens: int,
    config_max_output_tokens: int | None, provider_default_max_output_tokens: int | None, caps_max_output_tokens: int | None,
    safety_margin_tokens: int,
    caps_max_input_tokens: int = 0,
) -> int:
    """Exact input-budget formula (may be negative for a misconfigured setup)."""
    if type(config_max_tokens) is not int or config_max_tokens < 0:
        raise ValueError("config_max_tokens must be a non-negative integer")
    context_limit = config_max_tokens
    if type(caps_max_context_tokens) is int and caps_max_context_tokens > 0:
        context_limit = min(context_limit, caps_max_context_tokens)
    margin = max(0, safety_margin_tokens) if type(safety_margin_tokens) is int else 0
    budget = context_limit - effective_max_output_tokens(config_max_output_tokens, provider_default_max_output_tokens, caps_max_output_tokens) - margin
    if type(caps_max_input_tokens) is int and caps_max_input_tokens > 0:
        budget = min(budget, caps_max_input_tokens - margin)
    return budget
@dataclass(frozen=True)
class BudgetInputs:
    """Everything the allocation algorithm needs, frozen for one assembly."""

    config_max_tokens: int
    caps_max_context_tokens: int = 0
    config_max_output_tokens: int | None = None
    provider_default_max_output_tokens: int | None = None
    caps_max_output_tokens: int | None = None
    safety_margin_tokens: int = 0
    caps_max_input_tokens: int = 0
    @property
    def context_window(self) -> int:
        """The window usage is shown against: ``min(config.max_tokens, caps context)``."""
        caps = self.caps_max_context_tokens
        return min(self.config_max_tokens, caps) if caps > 0 else self.config_max_tokens
    @property
    def effective_max_output_tokens(self) -> int:
        return effective_max_output_tokens(
            self.config_max_output_tokens, self.provider_default_max_output_tokens, self.caps_max_output_tokens
        )
    @property
    def input_budget(self) -> int:
        return compute_input_budget(self.config_max_tokens, self.caps_max_context_tokens, self.config_max_output_tokens, self.provider_default_max_output_tokens, self.caps_max_output_tokens, self.safety_margin_tokens, self.caps_max_input_tokens)
    @classmethod
    def from_config_and_caps(
        cls, config: Any, capabilities: Any
    ) -> BudgetInputs:
        """Build inputs from a :class:`~nexus.config.Config` and capabilities."""
        v2 = getattr(config, "v2", None)
        context = getattr(v2, "context", None)
        if context is None:
            # Legacy v1 bridge: the old budget was measured in characters.
            config_max = max(1, int(getattr(config, "context_chars", 0)) // 4)
            safety = 0
        else:
            # Unset ``context.max_tokens`` means the model's own window.
            from ..config.schema import DEFAULT_CONTEXT_TOKENS
            caps_window = int(getattr(capabilities, "max_context_tokens", 0) or 0)
            config_max = context.max_tokens if context.max_tokens is not None else caps_window or DEFAULT_CONTEXT_TOKENS
            safety = context.safety_margin_tokens
        params = getattr(getattr(v2, "model", None), "params", None)
        config_output = getattr(params, "max_output_tokens", None)
        return cls(config_max, int(getattr(capabilities, "max_context_tokens", 0) or 0), config_output, int(getattr(capabilities, "default_max_output_tokens", 0) or 0), int(getattr(capabilities, "max_output_tokens", 0) or 0), safety, int(getattr(capabilities, "max_input_tokens", 0) or 0))


@dataclass(frozen=True)
class PartRequest:
    """A part's token demand entering allocation."""

    name: str
    priority: int
    tokens: int
    cap: int | None = None
    required: bool = False


@dataclass(frozen=True)
class Allocation:
    """What one part was granted."""

    name: str
    priority: int
    requested_tokens: int
    cap_tokens: int | None
    granted_tokens: int
    truncated: bool


@dataclass(frozen=True)
class BudgetPlan:
    """The full allocation result; all fields are content-free."""

    input_budget: int
    required_tokens: int
    history_budget: int
    allocations: tuple[Allocation, ...]
    oversized: tuple[str, ...] = ()

    def granted(self, name: str) -> int:
        for allocation in self.allocations:
            if allocation.name == name:
                return allocation.granted_tokens
        return 0

    def metadata(self) -> dict[str, Any]:
        return {
            "input_budget": self.input_budget,
            "required_tokens": self.required_tokens,
            "history_budget": self.history_budget,
            "oversized": list(self.oversized),
            "parts": [
                {
                    "name": allocation.name,
                    "priority": allocation.priority,
                    "requested": allocation.requested_tokens,
                    "cap": allocation.cap_tokens,
                    "granted": allocation.granted_tokens,
                    "truncated": allocation.truncated,
                }
                for allocation in self.allocations
            ],
        }


def allocate(inputs: BudgetInputs, requests: Sequence[PartRequest]) -> BudgetPlan:
    """Allocate the input budget across parts by priority then declared order."""
    budget = inputs.input_budget
    required = [request for request in requests if request.required]
    required_tokens = sum(request.tokens for request in required)
    if required_tokens > budget:
        detail = ", ".join(
            f"{request.name} ({request.tokens} tokens)"
            for request in sorted(required, key=lambda r: r.tokens, reverse=True)
        )
        raise ContextOverflow(
            f"Context budget exceeded: priority-0 parts require {required_tokens} "
            f"tokens but only {max(0, budget)} are available after the output "
            f"reserve and safety margin. Oversized: {detail}. Reduce the named "
            f"part(s) or raise context.max_tokens."
        )
    remaining = budget - required_tokens

    allocations: list[Allocation] = [
        Allocation(
            name=request.name,
            priority=request.priority,
            requested_tokens=request.tokens,
            cap_tokens=request.cap,
            granted_tokens=request.tokens,
            truncated=False,
        )
        for request in required
    ]

    optional = [
        request
        for request in requests
        if not request.required and request.name != "history"
    ]
    order = {id(request): index for index, request in enumerate(requests)}
    optional.sort(key=lambda request: (request.priority, order[id(request)]))
    for request in optional:
        granted = request.tokens
        if request.cap is not None:
            granted = min(granted, max(0, request.cap))
        granted = max(0, min(granted, remaining))
        remaining -= granted
        allocations.append(
            Allocation(
                name=request.name,
                priority=request.priority,
                requested_tokens=request.tokens,
                cap_tokens=request.cap,
                granted_tokens=granted,
                truncated=granted < request.tokens,
            )
        )

    return BudgetPlan(
        input_budget=budget,
        required_tokens=required_tokens,
        history_budget=max(0, remaining),
        allocations=tuple(allocations),
    )


def part_caps(config: Any) -> Mapping[str, int | None]:
    """Configured per-part token caps (``context.limits``), content-free."""
    v2 = getattr(config, "v2", None)
    context = getattr(v2, "context", None)
    limits = getattr(context, "limits", None)
    if limits is None:
        return {}
    return {
        "environment": limits.environment,
        "skills_index": limits.skills_index,
        "memory": limits.memory,
        "attachments": limits.attachments,
    }


def fit_suffix_count(costs: Sequence[int], budget: int) -> int:
    """Largest ``k`` whose trailing ``k`` costs sum to at most ``budget``.

    Whole items only: iteration stops at the first message that would overflow,
    so the retained set is always a contiguous suffix.
    """
    if budget <= 0:
        return 0
    total = 0
    keep = 0
    for cost in reversed(costs):
        if total + cost > budget:
            break
        total += cost
        keep += 1
    return keep
