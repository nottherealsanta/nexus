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
* ``antigravity`` — the built-in ``agy -p /usage`` command (verified with agy
  1.3.4) prints strict TSV quota rows. Nexus inverts remaining percentages into
  used percentages and does not make a model turn.
* ``xai`` — ``GET cli-chat-proxy.grok.com/v1/billing?format=credits`` with the
  SuperGrok bearer and ``x-xai-token-auth: xai-grok-cli`` (CodexBar
  ``docs/grok.md``, ``GrokCreditsProxyFetcher.swift``): ``config.currentPeriod``
  (else ``billingPeriodStart/End``) is the window, ``creditUsagePercent`` (else
  ``onDemandUsed / onDemandCap``) its use. The JSON omits a zero percent, so a
  current period with no percent and no on-demand figures reads as 0% used.
  ``productUsage`` shares and ``prepaidBalance`` (USD cents) become notes; the
  plan is ``subscription_tier_display`` from ``/v1/settings`` (best effort).

Only connected providers are fetched, concurrently, each bounded in time and
response size. A failure becomes that row's redacted ``error``; nothing here
returns a credential. Row shape: ``id``, ``label``, ``plan``, ``source``,
``windows`` (``label``, ``used_percent``, ``resets_at`` epoch seconds or
``None``, ``reset_text``, ``detail``, ``period_seconds``), ``notes``, ``error``.

``period_seconds`` is the window's length when it is known (Codex reports it;
OpenCode Go and Claude windows have fixed lengths; a monthly window runs one
calendar month back from its reset). With ``resets_at`` it lets a client say how
much of the window has elapsed, and so how much usage is in reserve. Claude's
``reset_text`` (``Oct 15 at 8:30pm (Asia/Calcutta)``) is parsed into
``resets_at`` in its named time zone; text that does not parse keeps
``resets_at = None``.
"""

from __future__ import annotations

import asyncio
import calendar
import math
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ..model.providers.antigravity_cli import AntigravityError
from ..util import redact_secrets
from . import provider_auth

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
COPILOT_USAGE_URL = "https://api.github.com/copilot_internal/user"
OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
XAI_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
XAI_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
LABELS = {
    "codex": "ChatGPT (Codex)",
    "claude-agent": "Claude",
    "github-copilot": "GitHub Copilot",
    "opencode-go": "OpenCode Go",
    "xai": "xAI",
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


FIVE_HOURS, WEEK = 18_000, 604_800


def month_before(resets_at: int | None) -> int | None:
    """Length of the calendar month that ends at ``resets_at`` (UTC), in seconds."""
    if resets_at is None:
        return None
    end = datetime.fromtimestamp(resets_at, timezone.utc)
    year, month = (end.year, end.month - 1) if end.month > 1 else (end.year - 1, 12)
    start = end.replace(year=year, month=month, day=min(end.day, calendar.monthrange(year, month)[1]))
    return int((end - start).total_seconds())


def _window(label: str, used: float | None, resets_at: int | None = None, reset_text: str = "", detail: str = "",
            period: float | None = None) -> dict[str, Any]:
    period_seconds = int(period) if period is not None and math.isfinite(period) and period > 0 else None
    return {"label": _text(label, 80) or "Limit", "used_percent": used, "resets_at": resets_at,
            "reset_text": _text(reset_text, 120), "detail": _text(detail), "period_seconds": period_seconds}


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
                windows.append(_window(f"{prefix}{label}", _percent(item.get("used_percent")), _epoch(item.get("reset_at")),
                                       period=_number(item.get("limit_window_seconds"))))

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
        windows.append(_window(f"{label} (monthly)", None if remaining_percent is None else round(100 - remaining_percent, 2),
                               resets_at, detail=detail, period=month_before(resets_at)))
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
        period = {"rolling": FIVE_HOURS, "weekly": WEEK}.get(key) or month_before(resets_at)
        windows.append(_window(label, used, resets_at, period=period))
    if not windows:
        raise UsageError("the OpenCode Go response had no usage windows")
    return {"plan": "", "windows": windows, "notes": []}


def _period_label(seconds: int | None) -> str:
    if seconds is None:
        return "Credits"
    days = seconds / 86_400
    return "Weekly" if 6.5 <= days <= 7.5 else "Monthly" if 27.5 <= days <= 31.5 else "Credits"


def parse_xai(data: Mapping[str, Any], plan: str = "", now: float | None = None) -> dict[str, Any]:
    config = data.get("config")
    if not isinstance(config, Mapping):
        raise UsageError("the xAI billing response had no config")
    now = time.time() if now is None else now
    current = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), Mapping) else {}
    end = _epoch(current.get("end"))
    # Never mix the current period's end with the billing period's start.
    start = _epoch(current.get("start")) if end is not None else _epoch(config.get("billingPeriodStart"))
    end = end if end is not None else _epoch(config.get("billingPeriodEnd"))
    period = end - start if start is not None and end is not None and start < end and start <= now else None
    used = _percent(config.get("creditUsagePercent"))
    detail = ""
    if used is None:
        cap, spent = config.get("onDemandCap"), config.get("onDemandUsed")
        cap = _number(cap.get("val")) if isinstance(cap, Mapping) else None
        spent = _number(spent.get("val")) if isinstance(spent, Mapping) else None
        if cap and spent is not None:
            used = _percent(spent / cap * 100)
        elif end is not None and cap is None and spent is None:
            used, detail = 0.0, "xAI omits a zero usage percent"
    if used is None and end is None:
        raise UsageError("the xAI billing response had no usage or period")
    windows = [_window(_period_label(period), used, end, detail=detail, period=period)]
    notes: list[str] = []
    products = config.get("productUsage")
    for item in products[:8] if isinstance(products, list) else ():
        if isinstance(item, Mapping) and (name := _text(item.get("product"), 40)) and (share := _percent(item.get("usagePercent"))) is not None:
            notes.append(f"{name}: {share:g}% of credits")
    balance = config.get("prepaidBalance")
    if isinstance(balance, Mapping):
        cents = 0.0 if "val" not in balance else _number(balance.get("val"))
        if cents is not None and cents >= 0:
            notes.append(f"Prepaid credits: ${cents / 100:,.2f}")
    return {"plan": _text(plan or config.get("subscriptionTier") or data.get("subscriptionTier") or "SuperGrok", 40),
            "windows": windows, "notes": notes}


def claude_label(label: str) -> str:
    """``Current session`` → ``5-hour session``; ``Current week (Sonnet only)`` → ``Weekly (Sonnet only)``."""
    lowered = label.casefold()
    if lowered.startswith("current session"):
        return "5-hour session" + label[len("current session"):]
    if lowered.startswith("current week"):
        return "Weekly" + label[len("current week"):]
    return label


_CLAUDE_RESET = re.compile(
    r"(?:(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?\s+at\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>am|pm)\s*\((?P<zone>[A-Za-z0-9_+\-/]{1,64})\)",
    re.IGNORECASE)
_MONTHS = {name: index for index, name in enumerate(calendar.month_abbr) if name}


def claude_reset_at(text: str, now: float | None = None) -> int | None:
    """``Oct 15 at 8:30pm (Asia/Calcutta)`` or ``12pm (Asia/Calcutta)`` → epoch seconds.

    The CLI omits the year (and the date for a reset later today), so the
    earliest matching moment no more than a day in the past is chosen.
    """
    match = _CLAUDE_RESET.fullmatch(text.strip())
    if match is None:
        return None
    try:
        zone = ZoneInfo(match["zone"])
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(match["hour"]) % 12 + (12 if match["half"].lower() == "pm" else 0)
    minute = int(match["minute"] or 0)
    if minute > 59:
        return None
    now = time.time() if now is None else now
    local_now = datetime.fromtimestamp(now, zone)
    candidates: list[datetime] = []
    try:
        if match["month"]:
            month = _MONTHS.get(match["month"].title())
            if month is None:
                return None
            years = [int(match["year"])] if match["year"] else [local_now.year - 1, local_now.year, local_now.year + 1]
            for year in years:
                try:
                    candidates.append(datetime(year, month, int(match["day"]), hour, minute, tzinfo=zone))
                except ValueError:
                    continue
        else:
            today = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            candidates = [today, today + timedelta(days=1)]
    except ValueError:
        return None
    # A dated reset may be slightly stale (a day of slack); a bare time is the next one.
    floor = now - 86_400 if match["month"] else now
    future = sorted(int(moment.timestamp()) for moment in candidates if moment.timestamp() >= floor)
    return future[0] if future else None


def _claude_period(label: str) -> int | None:
    lowered = label.casefold()
    return FIVE_HOURS if lowered.startswith("5-hour") else WEEK if lowered.startswith("weekly") else None


def parse_claude(text: str, now: float | None = None) -> dict[str, Any]:
    from ..model.providers.claude_agent_auth import parse_usage

    windows = []
    for row in parse_usage(text):
        label = claude_label(row["label"])
        windows.append(_window(label, row["used_percent"], claude_reset_at(row["reset_text"], now), row["reset_text"],
                               period=_claude_period(label)))
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
    if provider == "xai":
        authorization = (await provider_auth._xai(runtime).headers())["authorization"]
        headers = {"authorization": authorization, "x-xai-token-auth": "xai-grok-cli",
                   "accept": "application/json", "user-agent": "Nexus/0.1"}
        billing, settings = await asyncio.gather(_get(client, XAI_BILLING_URL, headers),
                                                 _xai_plan(client, headers), return_exceptions=True)
        if isinstance(billing, BaseException):
            raise billing
        return parse_xai(billing, settings if isinstance(settings, str) else "")
    if provider == "claude-agent":
        manager = provider_auth._manager(runtime, provider)
        workspace = getattr(runtime, "workspace", None)
        try:
            text, plan = await asyncio.gather(manager.usage(cwd=str(workspace) if workspace else None), manager.plan())
        except RuntimeError as exc:
            raise UsageError(str(exc)) from None
        return {**parse_claude(text), "plan": _text(plan, 40).title()}
    raise UsageError("usage is not available for this provider")


async def _xai_plan(client: httpx.AsyncClient, headers: Mapping[str, str]) -> str:
    """``SuperGrok``/``SuperGrok Lite``/``SuperGrok Heavy`` from settings; ``""`` on any failure."""
    try:
        async with asyncio.timeout(3.0):
            settings = await _get(client, XAI_SETTINGS_URL, headers)
    except (UsageError, TimeoutError):
        return ""
    return _text(settings.get("subscription_tier_display"), 40)


_SOURCES = {
    "codex": "chatgpt.com · wham/usage",
    "claude-agent": "claude CLI · /usage",
    "github-copilot": "api.github.com · copilot_internal/user",
    "opencode-go": "opencode.ai · zen/go/v1/usage",
    "xai": "cli-chat-proxy.grok.com · v1/billing",
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


ANTIGRAVITY_ID = "antigravity"


def _antigravity_configured(runtime: object) -> bool:
    """An injected helper or a ``[providers.antigravity]`` section means the CLI is wanted."""
    from .setup import _load_config

    if getattr(runtime, "_antigravity_cli_factory", None) is not None:
        return True
    providers = getattr(getattr(_load_config(runtime), "v2", None), "providers", None)
    return isinstance(providers, Mapping) and ANTIGRAVITY_ID in providers


async def _antigravity_row(runtime: object) -> dict[str, Any]:
    """Fetch Antigravity quota from the configured CLI even when auth is unknown."""
    row: dict[str, Any] = {"id": ANTIGRAVITY_ID, "label": "Antigravity", "source": "agy CLI · /usage", "plan": "",
                           "windows": [], "notes": [], "error": ""}
    try:
        cli = provider_auth._antigravity(runtime)
        async with asyncio.timeout(_TOTAL_TIMEOUT):
            row.update(await cli.usage())
    except AntigravityError as exc:
        row["error"] = _text(exc, 240)
    except TimeoutError:
        row["error"] = "timed out reading usage"
    except Exception as exc:  # noqa: BLE001 - redacted, never a traceback
        row["error"] = provider_auth._safe(exc)
    return row


async def providers_usage(runtime: object, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """One row per connected provider, fetched concurrently; plus Antigravity when its CLI is configured."""
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
    rows = list(rows)
    if _antigravity_configured(runtime):
        rows.append(await _antigravity_row(runtime))
    return {"providers": rows, "not_connected": missing, "fetched_at": time.time()}


__all__ = [
    "LABELS",
    "UsageError",
    "claude_reset_at",
    "month_before",
    "parse_claude",
    "parse_codex",
    "parse_copilot",
    "parse_opencode_go",
    "parse_xai",
    "providers_usage",
    "window_label",
]
