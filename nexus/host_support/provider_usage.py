"""Plan usage and limits for every connected provider (``ProvidersUsage``).

The endpoints and field mappings follow CodexBar (github.com/steipete/CodexBar,
``docs/{codex,claude,copilot,opencode}.md``), re-implemented over the
credentials Nexus already holds:

* ``codex`` — ``GET chatgpt.com/backend-api/wham/usage`` with the ChatGPT OAuth
  headers. ``rate_limit.primary_window`` / ``secondary_window`` (and each
  ``additional_rate_limits[]`` entry) carry ``used_percent``, ``reset_at`` and
  ``limit_window_seconds``.
* ``claude-agent`` — the official CLI's ``/usage`` command
  (:mod:`nexus.model.providers.claude_agent_auth`). It makes no model request
  and Nexus never reads Claude's tokens, so this is parsed text, not JSON.
* ``github-copilot`` — ``GET api.github.com/copilot_internal/user`` with the
  stored GitHub token; ``quota_snapshots`` give a monthly ``percent_remaining``
  and ``entitlement`` per quota, reset at ``quota_reset_date_utc``.
* ``opencode-go`` — ``GET opencode.ai/zen/go/v1/usage`` with the stored API key;
  ``usage.rolling|weekly|monthly`` carry ``usagePercent`` and ``resetInSec``.
  Not verified against a live account.

Only connected providers are fetched, concurrently, each bounded in time and
response size. A failure becomes that row's redacted ``error``; nothing here
returns a credential. Row shape: ``id``, ``label``, ``plan``, ``source``,
``windows`` (``label``, ``used_percent``, ``resets_at`` epoch seconds or
``None``, ``reset_text``, ``detail``), ``notes``, ``error``.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from ..util import redact_secrets
from . import provider_auth

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
COPILOT_USAGE_URL = "https://api.github.com/copilot_internal/user"
OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
LABELS = {
    "codex": "ChatGPT (Codex)",
    "claude-agent": "Claude",
    "github-copilot": "GitHub Copilot",
    "opencode-go": "OpenCode Go",
}
_HTTP_TIMEOUT = 15.0
_TOTAL_TIMEOUT = 40.0
_MAX_BODY = 512 * 1024
_MAX_WINDOWS = 16
_MAX_TEXT = 160


class UsageError(Exception):
    """A safe, user-facing reason a provider's usage could not be read."""


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    return redact_secrets(str(value))[:limit] if value not in (None, "") else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _percent(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else max(0.0, min(number, 100.0))


def _epoch(value: Any) -> int | None:
    """Epoch seconds from seconds, milliseconds or an ISO-8601 string."""
    number = _number(value)
    if number is not None:
        number = number / 1000 if number > 1e11 else number
        return int(number) if number > 0 else None
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def window_label(seconds: Any) -> str:
    """``18000`` → ``5-hour``; ``604800`` → ``Weekly``."""
    value = _number(seconds)
    if value is None or value <= 0:
        return "Limit"
    if value == 604_800:
        return "Weekly"
    if value % 86_400 == 0:
        days = int(value // 86_400)
        return "Daily" if days == 1 else f"{days}-day"
    if value % 3600 == 0:
        return f"{int(value // 3600)}-hour"
    return f"{max(1, round(value / 60))}-minute"


def _window(label: str, used: float | None, resets_at: int | None = None, reset_text: str = "", detail: str = "") -> dict[str, Any]:
    return {"label": _text(label, 80) or "Limit", "used_percent": used, "resets_at": resets_at,
            "reset_text": _text(reset_text, 120), "detail": _text(detail)}


def _json(response: httpx.Response) -> Mapping[str, Any]:
    if response.status_code in (401, 403):
        raise UsageError(f"HTTP {response.status_code}: sign in again in Settings → Providers")
    if response.status_code != 200:
        raise UsageError(f"HTTP {response.status_code} from the usage endpoint")
    if len(response.content) > _MAX_BODY:
        raise UsageError("the usage response was too large")
    try:
        value = response.json()
    except ValueError:
        raise UsageError("the usage response was not JSON") from None
    if not isinstance(value, Mapping):
        raise UsageError("the usage response had an unexpected shape")
    return value


def parse_codex(data: Mapping[str, Any]) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []

    def add(limits: Any, prefix: str = "") -> None:
        if not isinstance(limits, Mapping):
            return
        for key in ("primary_window", "secondary_window"):
            item = limits.get(key)
            if isinstance(item, Mapping):
                label = window_label(item.get("limit_window_seconds"))
                windows.append(_window(f"{prefix}{label}", _percent(item.get("used_percent")), _epoch(item.get("reset_at"))))

    add(data.get("rate_limit"))
    extra = data.get("additional_rate_limits")
    for item in extra[:8] if isinstance(extra, list) else ():
        if isinstance(item, Mapping):
            name = _text(item.get("limit_name") or item.get("metered_feature"), 60)
            add(item.get("rate_limit"), f"{name} " if name else "")
    add(data.get("code_review_rate_limit"), "Code review ")
    notes: list[str] = []
    limits = data.get("rate_limit")
    if isinstance(limits, Mapping) and limits.get("limit_reached") is True:
        notes.append("Limit reached: new requests wait for the next reset")
    credits = data.get("credits")
    if isinstance(credits, Mapping):
        if credits.get("unlimited") is True:
            notes.append("Credits: unlimited")
        elif credits.get("has_credits") is True and (balance := _number(credits.get("balance"))) is not None:
            notes.append(f"Credits balance: {balance:g}")
    resets = data.get("rate_limit_reset_credits")
    if isinstance(resets, Mapping) and (count := _number(resets.get("available_count"))):
        notes.append(f"Usage reset credits available: {int(count)}")
    return {"plan": _text(data.get("plan_type"), 40).title(), "windows": windows[:_MAX_WINDOWS], "notes": notes}


_COPILOT_QUOTAS = (("premium_interactions", "Premium requests"), ("chat", "Chat"), ("completions", "Completions"))


def parse_copilot(data: Mapping[str, Any]) -> dict[str, Any]:
    snapshots = data.get("quota_snapshots")
    snapshots = snapshots if isinstance(snapshots, Mapping) else {}
    resets_at = _epoch(data.get("quota_reset_date_utc") or data.get("quota_reset_date"))
    windows: list[dict[str, Any]] = []
    notes: list[str] = []
    for key, label in _COPILOT_QUOTAS:
        item = snapshots.get(key)
        if not isinstance(item, Mapping):
            continue
        if item.get("unlimited") is True:
            notes.append(f"{label}: unlimited")
            continue
        remaining_percent = _percent(item.get("percent_remaining"))
        entitlement, remaining = _number(item.get("entitlement")), _number(item.get("remaining"))
        if remaining_percent is None and entitlement and remaining is not None:
            remaining_percent = _percent(remaining / entitlement * 100)
        detail = f"{remaining:,.0f} of {entitlement:,.0f} left" if entitlement and remaining is not None else ""
        if (used := _number(item.get("overage_count"))):
            detail = f"{detail} · {used:,.0f} over quota".lstrip(" ·")
        windows.append(_window(f"{label} (monthly)", None if remaining_percent is None else round(100 - remaining_percent, 2), resets_at, detail=detail))
    credits = _number((snapshots.get("premium_interactions") or {}).get("credits_used") if isinstance(snapshots.get("premium_interactions"), Mapping) else None)
    if data.get("token_based_billing") is True and credits is not None:
        notes.append(f"AI credits used this period: {credits:,.0f}")
    return {"plan": _text(data.get("copilot_plan"), 40).title(), "windows": windows, "notes": notes}


_GO_WINDOWS = (("rolling", "5-hour"), ("weekly", "Weekly"), ("monthly", "Monthly"))
_GO_PERCENT = ("usagePercent", "usedPercent", "percentUsed", "percent", "usage_percent", "used_percent")
_GO_RESET_IN = ("resetInSec", "resetInSeconds", "reset_in_sec", "resetsInSec")
_GO_RESET_AT = ("resetAt", "resetsAt", "reset_at", "resets_at")


def _first(item: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    return next((item[key] for key in keys if item.get(key) is not None), None)


def parse_opencode_go(data: Mapping[str, Any], now: float | None = None) -> dict[str, Any]:
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        raise UsageError("the OpenCode Go response had no usage")
    now = time.time() if now is None else now
    windows: list[dict[str, Any]] = []
    for key, label in _GO_WINDOWS:
        item = usage.get(key)
        if not isinstance(item, Mapping):
            continue
        used = _percent(_first(item, _GO_PERCENT))
        if used is None and (limit := _number(item.get("limit"))):
            used = _percent((_number(item.get("used")) or 0) / limit * 100)
        reset_in = _number(_first(item, _GO_RESET_IN))
        resets_at = int(now + reset_in) if reset_in is not None and reset_in >= 0 else _epoch(_first(item, _GO_RESET_AT))
        windows.append(_window(label, used, resets_at))
    if not windows:
        raise UsageError("the OpenCode Go response had no usage windows")
    return {"plan": "", "windows": windows, "notes": []}


def claude_label(label: str) -> str:
    """``Current session`` → ``5-hour session``; ``Current week (Sonnet only)`` → ``Weekly (Sonnet only)``."""
    lowered = label.casefold()
    if lowered.startswith("current session"):
        return "5-hour session" + label[len("current session"):]
    if lowered.startswith("current week"):
        return "Weekly" + label[len("current week"):]
    return label


def parse_claude(text: str) -> dict[str, Any]:
    from ..model.providers.claude_agent_auth import parse_usage

    windows = [_window(claude_label(row["label"]), row["used_percent"], None, row["reset_text"]) for row in parse_usage(text)]
    if not windows:
        line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        raise UsageError(_text(line) or "the Claude CLI printed no usage limits")
    return {"plan": "", "windows": windows, "notes": []}


async def _get(client: httpx.AsyncClient, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
    try:
        response = await client.get(url, headers=dict(headers), follow_redirects=False)
    except httpx.HTTPError:
        raise UsageError("could not reach the usage endpoint; check connectivity") from None
    return _json(response)


async def _fetch(runtime: object, provider: str, client: httpx.AsyncClient) -> dict[str, Any]:
    if provider == "codex":
        headers = await provider_auth._codex(runtime).headers()
        return parse_codex(await _get(client, CODEX_USAGE_URL, {**headers, "accept": "application/json"}))
    if provider == "github-copilot":
        headers = await provider_auth._copilot(runtime).headers()
        return parse_copilot(await _get(client, COPILOT_USAGE_URL, {
            "authorization": headers["authorization"], "accept": "application/json",
            "user-agent": "Nexus/0.1", "x-github-api-version": "2025-04-01",
        }))
    if provider == "opencode-go":
        headers = await provider_auth._api_key(runtime, provider).headers()
        return parse_opencode_go(await _get(client, OPENCODE_GO_USAGE_URL, {**headers, "accept": "application/json"}))
    if provider == "claude-agent":
        manager = provider_auth._manager(runtime, provider)
        workspace = getattr(runtime, "workspace", None)
        try:
            text, plan = await asyncio.gather(manager.usage(cwd=str(workspace) if workspace else None), manager.plan())
        except RuntimeError as exc:
            raise UsageError(str(exc)) from None
        return {**parse_claude(text), "plan": _text(plan, 40).title()}
    raise UsageError("usage is not available for this provider")


_SOURCES = {
    "codex": "chatgpt.com · wham/usage",
    "claude-agent": "claude CLI · /usage",
    "github-copilot": "api.github.com · copilot_internal/user",
    "opencode-go": "opencode.ai · zen/go/v1/usage",
}


async def _row(runtime: object, provider: str, client: httpx.AsyncClient) -> dict[str, Any]:
    row: dict[str, Any] = {"id": provider, "label": LABELS[provider], "source": _SOURCES[provider],
                           "plan": "", "windows": [], "notes": [], "error": ""}
    try:
        async with asyncio.timeout(_TOTAL_TIMEOUT):
            row.update(await _fetch(runtime, provider, client))
    except UsageError as exc:
        row["error"] = _text(exc, 240)
    except TimeoutError:
        row["error"] = "timed out reading usage"
    except Exception as exc:  # noqa: BLE001 - redacted, never a traceback or credential
        row["error"] = provider_auth._safe(exc)
    return row


async def providers_usage(runtime: object, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """One row per connected provider, fetched concurrently."""
    providers = tuple(LABELS)
    flags = await asyncio.gather(*(provider_auth.connected(runtime, provider) for provider in providers))
    connected = [provider for provider, ok in zip(providers, flags, strict=True) if ok]
    owned = client is None
    client = client or getattr(runtime, "_usage_http_client", None) or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    owned = owned and getattr(runtime, "_usage_http_client", None) is None
    try:
        rows = await asyncio.gather(*(_row(runtime, provider, client) for provider in connected))
    finally:
        if owned:
            await client.aclose()
    missing = [LABELS[provider] for provider in providers if provider not in connected]
    return {"providers": list(rows), "not_connected": missing, "fetched_at": time.time()}


__all__ = [
    "LABELS",
    "UsageError",
    "parse_claude",
    "parse_codex",
    "parse_copilot",
    "parse_opencode_go",
    "providers_usage",
    "window_label",
]
