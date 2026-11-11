"""Usage for every connected provider (PLAN K11).

Each signed-in provider gets a row: `{id, label, source, plan, windows, notes, error}`. Providers that expose plan
limits are asked for them concurrently; the rest say so in `notes` and still appear beside the others. A window is
`{label, used_percent, resets_at (epoch seconds or None), period_seconds, kind, detail}` where `kind` is
`five_hour`, `weekly`, `monthly` or `other` (per-model side quotas are `other`).

Limit sources, re-implemented from old Nexus `host_support/provider_usage.py` over the credentials `auth.json`
already holds:

* `codex`: `GET chatgpt.com/backend-api/wham/usage` with the ChatGPT headers. `rate_limit.primary_window` and
  `secondary_window` (and every `additional_rate_limits[]` entry) carry `used_percent`, `reset_at` and
  `limit_window_seconds`.
* `copilot`: `GET api.github.com/copilot_internal/user` with the stored GitHub token. `quota_snapshots` give a
  monthly `percent_remaining` per quota, reset at `quota_reset_date_utc`.
* `opencode`: `GET opencode.ai/zen/go/v1/usage` with the stored API key. `usage.rolling`, `weekly` and `monthly`
  carry a used percent and a reset time.
* `grok`: `GET cli-chat-proxy.grok.com/v1/billing?format=credits` and `GET …/v1/settings` with the proxy headers.
  The billing config gives the credit percent and period; settings give the subscription name when it can be read.
* `claude`: the Claude Code account's OAuth usage endpoint, with an interactive CLI `/usage` fallback.

Only signed-in providers are asked, each bounded in time and size. A failure is that row's `error` (a short reason,
never a credential). Providers with no plan-limit source (OpenAI, Groq, NVIDIA, Ollama) get a row with no windows
and a note; their per-request token usage is in the session ledger.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .claude_usage import interactive_usage, oauth_credentials
from .llmhub.auth import SIGN_INS, AuthError
from .llmhub.auth.claude import child_env, cli_path
from .llmhub.auth.claude import status as claude_status
from .llmhub.providers import PROVIDERS

CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
COPILOT_URL = "https://api.github.com/copilot_internal/user"
OPENCODE_URL = "https://opencode.ai/zen/go/v1/usage"
XAI_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
XAI_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
LABELS = {pid: info.label for pid, info in PROVIDERS.items() if pid != "mock"}
SOURCES = {
    "codex": "chatgpt.com · wham/usage",
    "claude": "Claude OAuth · api/oauth/usage (CLI fallback)",
    "grok": "cli-chat-proxy.grok.com · v1/billing",
    "copilot": "api.github.com · copilot_internal/user",
    "opencode": "opencode.ai · zen/go/v1/usage",
}
NO_LIMITS_NOTE = "Plan limits are not exposed here; token usage is in the session totals."
MAX_BODY = 512 * 1024
MAX_WINDOWS = 16
FIVE_HOURS, WEEK = 18_000, 604_800
ROW_TIMEOUT = 40.0
CLAUDE_USAGE_TIMEOUT = 30.0


class UsageError(Exception):
    """A short, user-facing reason a provider's usage could not be read."""


def _text(value: Any, limit: int = 160) -> str:
    return str(value)[:limit] if value not in (None, "") else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


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
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            return None
    return None


def window_kind(seconds: int | None) -> str:
    if seconds is None:
        return "other"
    return ("five_hour" if seconds <= FIVE_HOURS * 1.1 else "weekly" if seconds <= WEEK * 1.1
            else "monthly" if seconds <= 31 * 86_400 else "other")


def window_label(seconds: Any) -> str:
    """`18000` -> `5-hour`; `604800` -> `Weekly`."""
    value = _number(seconds)
    if value is None or value <= 0:
        return "Limit"
    if value == WEEK:
        return "Weekly"
    if value % 86_400 == 0:
        days = int(value // 86_400)
        return "Daily" if days == 1 else f"{days}-day"
    if value % 3600 == 0:
        return f"{int(value // 3600)}-hour"
    return f"{max(1, round(value / 60))}-minute"


def month_before(resets_at: int | None) -> int | None:
    """Seconds in the calendar month that ends at `resets_at` (UTC)."""
    if resets_at is None:
        return None
    end = datetime.fromtimestamp(resets_at, UTC)
    year, month = (end.year, end.month - 1) if end.month > 1 else (end.year - 1, 12)
    start = end.replace(year=year, month=month, day=min(end.day, calendar.monthrange(year, month)[1]))
    return int((end - start).total_seconds())


def _window(label: str, used: float | None, resets_at: int | None, *, period: float | None = None,
            kind: str | None = None, detail: str = "") -> dict[str, Any]:
    seconds = int(period) if period is not None and math.isfinite(period) and period > 0 else None
    return {"label": _text(label, 80) or "Limit", "used_percent": used, "resets_at": resets_at,
            "period_seconds": seconds, "kind": kind or window_kind(seconds), "detail": _text(detail)}


def parse_codex(data: Mapping[str, Any]) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []

    def add(limits: Any, prefix: str = "", kind: str | None = None) -> None:
        if not isinstance(limits, Mapping):
            return
        for key in ("primary_window", "secondary_window"):
            item = limits.get(key)
            if isinstance(item, Mapping):
                seconds = _number(item.get("limit_window_seconds"))
                windows.append(_window(prefix + window_label(seconds), _percent(item.get("used_percent")),
                                       _epoch(item.get("reset_at")), period=seconds, kind=kind))

    add(data.get("rate_limit"))
    monthly = codex_individual_limit(data)
    if monthly is not None:
        windows.append(monthly)
    extra = data.get("additional_rate_limits")
    for item in extra[:8] if isinstance(extra, list) else ():
        if isinstance(item, Mapping):
            name = _text(item.get("limit_name") or item.get("metered_feature"), 60)
            add(item.get("rate_limit"), f"{name} " if name else "", kind="other")  # side quotas never gate the provider
    add(data.get("code_review_rate_limit"), "Code review ", kind="other")
    notes: list[str] = []
    credits = data.get("credits")
    if isinstance(credits, Mapping):
        if credits.get("unlimited") is True:
            notes.append("Credits: unlimited")
        elif credits.get("has_credits") is True and (balance := _number(credits.get("balance"))) is not None:
            notes.append(f"Credits balance: {balance:g}")
    return {"plan": _text(data.get("plan_type"), 40).title(), "windows": windows[:MAX_WINDOWS], "notes": notes}


def codex_individual_limit(data: Mapping[str, Any]) -> dict[str, Any] | None:
    for parent in (data, data.get("rate_limit"), _first(data, ("spend_control", "spendControl"))):
        if not isinstance(parent, Mapping):
            continue
        item = _first(parent, ("individual_limit", "individualLimit"))
        if not isinstance(item, Mapping):
            continue
        limit, used = _number(item.get("limit")), _number(item.get("used"))
        if limit is None or limit <= 0:
            continue
        percent = _percent(_first(item, ("used_percent", "usedPercent")))
        remaining = _percent(_first(item, ("remaining_percent", "remainingPercent")))
        if percent is None and remaining is not None:
            percent = 100 - remaining
        if percent is None and used is not None and used >= 0:
            percent = _percent(used / limit * 100)
        reset = _epoch(_first(item, ("reset_at", "resets_at", "resetsAt")))
        detail = f"{used:,.2f} of {limit:,.2f} credits used" if used is not None and used >= 0 else f"{limit:,.2f} credit limit"
        return _window("Monthly credits", percent, reset, period=month_before(reset), kind="monthly", detail=detail)
    return None


def parse_codex_monthly(data: Mapping[str, Any]) -> dict[str, Any] | None:
    cap = data.get("effective_monthly_limit")
    if not isinstance(cap, Mapping):
        raise UsageError("monthly spend controls did not report a limit")
    if str(cap.get("enforcement_mode", "")).casefold() in ("none", "off", "disabled", "no_limit"):
        return None
    window = codex_individual_limit({"individual_limit": {
        "limit": cap.get("limit"), "used": data.get("current_month_usage")}})
    if window is None:
        raise UsageError("monthly spend controls reported an unreadable limit")
    return window


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
        if over := _number(item.get("overage_count")):
            detail = f"{detail} · {over:,.0f} over quota".lstrip(" ·")
        used = None if remaining_percent is None else round(100 - remaining_percent, 2)
        windows.append(_window(f"{label} (monthly)", used, resets_at, period=month_before(resets_at),
                               kind="monthly", detail=detail))
    credits = _number((snapshots.get("premium_interactions") or {}).get("credits_used")
                      if isinstance(snapshots.get("premium_interactions"), Mapping) else None)
    if data.get("token_based_billing") is True and credits is not None:
        notes.append(f"AI credits used this period: {credits:,.0f}")
    return {"plan": _text(data.get("copilot_plan"), 40).title(), "windows": windows, "notes": notes}


_GO_WINDOWS = (("rolling", "5-hour"), ("weekly", "Weekly"), ("monthly", "Monthly"))
_GO_PERCENT = ("usagePercent", "usedPercent", "percentUsed", "percent", "usage_percent", "used_percent")
_GO_RESET_IN = ("resetInSec", "resetInSeconds", "reset_in_sec", "resetsInSec")
_GO_RESET_AT = ("resetAt", "resetsAt", "reset_at", "resets_at")


def _first(item: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    return next((item[key] for key in keys if item.get(key) is not None), None)


def parse_opencode(data: Mapping[str, Any], now: float | None = None) -> dict[str, Any]:
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        raise UsageError("the OpenCode usage response had no usage")
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
        kind = {"rolling": "five_hour", "weekly": "weekly", "monthly": "monthly"}[key]
        windows.append(_window(label, used, resets_at, period=period, kind=kind))
    if not windows:
        raise UsageError("the OpenCode usage response had no windows")
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
    balance = config.get("prepaidBalance")
    if isinstance(balance, Mapping):
        cents = 0.0 if "val" not in balance else _number(balance.get("val"))
        if cents is not None and cents >= 0:
            notes.append(f"Prepaid credits: ${cents / 100:,.2f}")
    return {"plan": _text(plan or config.get("subscriptionTier") or data.get("subscriptionTier") or "SuperGrok", 40),
            "windows": windows, "notes": notes}


_CLAUDE_USAGE = re.compile(
    r"^\s*(?P<label>[A-Za-z][^:\n]{0,79}):\s*(?P<used>\d{1,3}(?:\.\d+)?)%\s+used(?:\s*·\s*(?P<reset>[^\n]{1,120}))?\s*$")
_CLAUDE_RESET = re.compile(
    r"(?:(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?\s+at\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>am|pm)\s*\((?P<zone>[A-Za-z0-9_+\-/]{1,64})\)",
    re.IGNORECASE)
_MONTHS = {name: index for index, name in enumerate(calendar.month_abbr) if name}


def claude_label(label: str) -> str:
    """`Current session` -> `5-hour session`; `Current week (Sonnet only)` -> `Weekly (Sonnet only)`."""
    lowered = label.casefold()
    if lowered.startswith("current session"):
        return "5-hour session" + label[len("current session"):]
    if lowered.startswith("current week"):
        return "Weekly" + label[len("current week"):]
    return label


def claude_reset_at(text: str, now: float | None = None) -> int | None:
    """`Oct 15 at 8:30pm (Asia/Calcutta)` or `12pm (Asia/Calcutta)` -> epoch seconds.

    The CLI omits the year (and the date for a reset later today), so the earliest matching moment no more than a
    day in the past is chosen.
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


def parse_claude(text: str, now: float | None = None) -> dict[str, Any]:
    lines = text.splitlines()[:200]
    normalized = []
    for index, line in enumerate(lines):
        if not re.fullmatch(r"\s*Current (?:session|week)(?:\s*\([^:\n]+\))?\s*", line, re.IGNORECASE):
            normalized.append(line)
            continue
        following = []
        for candidate in lines[index + 1:index + 13]:
            if re.search(r"Current (?:session|week)|Usage credits|Usage insights", candidate, re.IGNORECASE):
                break
            following.append(candidate)
        block = " ".join(candidate.strip() for candidate in following)
        percent = re.search(r"(\d{1,3}(?:\.\d+)?)%\s+used", block)
        if percent:
            reset = next((re.search(r"[Rr]esets\s+(.+)", candidate.strip()) for candidate in following
                          if re.search(r"[Rr]esets\s+", candidate)), None)
            normalized.append(f"{line.strip()}: {percent[1]}% used" + (f" · resets {reset[1].strip()}" if reset else ""))
    windows: list[dict[str, Any]] = []
    for line in normalized:
        match = _CLAUDE_USAGE.match(line)
        if match is None:
            continue
        if not re.fullmatch(r"Current (?:session|week)(?:\s*\([^:]+\))?", match["label"].strip(), re.IGNORECASE):
            continue
        label = claude_label(match["label"].strip())
        reset = (match["reset"] or "").strip().removeprefix("resets ").strip()
        period = FIVE_HOURS if label.casefold().startswith("5-hour") else WEEK if label.casefold().startswith("weekly") else None
        kind = ("five_hour" if label.casefold().startswith("5-hour")
                else "weekly" if label.casefold() in ("weekly", "weekly (all models)") else "other")
        resets_at = claude_reset_at(reset, now)
        windows.append(_window(label, _percent(match["used"]), resets_at, period=period, kind=kind,
                               detail="" if resets_at else reset))
        if len(windows) >= MAX_WINDOWS:
            break
    heading = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == "Usage credits"), None)
    if heading is not None:
        for line in lines[heading + 1:heading + 7]:
            match = re.fullmatch(r"\s*\$([\d,]+(?:\.\d+)?)\s*/\s*\$([\d,]+(?:\.\d+)?)\s+spent(?:\s*·\s*Resets[^\n]*)?\s*", line)
            if match:
                used, cap = (_number(v.replace(",", "")) for v in match.groups())
                if used is not None and cap is not None and cap > 0:
                    windows.append(_window("Spend limit", _percent(used / cap * 100), None, kind="monthly",
                                           detail=f"USD {used:,.2f} of {cap:,.2f}"))
                break
    if not windows:
        raise UsageError("the Claude CLI reported no recognizable subscription usage")
    return {"plan": "", "windows": windows, "notes": []}


async def _get(client: httpx.AsyncClient, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
    response = await client.get(url, headers=dict(headers), follow_redirects=False)
    if response.status_code in (401, 403):
        raise UsageError(f"HTTP {response.status_code}: sign in again in Settings")
    if response.status_code != 200:
        raise UsageError(f"HTTP {response.status_code} from the usage endpoint")
    if len(response.content) > MAX_BODY:
        raise UsageError("the usage response was too large")
    try:
        value = response.json()
    except ValueError:
        raise UsageError("the usage response was not JSON") from None
    if not isinstance(value, Mapping):
        raise UsageError("the usage response had an unexpected shape")
    return value


async def _codex(client: httpx.AsyncClient) -> dict[str, Any]:
    headers = await SIGN_INS["codex"]().headers()
    headers = {**headers, "accept": "application/json"}
    data = await _get(client, CODEX_URL, headers)
    result = parse_codex(data)
    account = next((v for k, v in headers.items() if k.lower() == "chatgpt-account-id"), "")
    response_account = data.get("account_id")
    if not account or (response_account and response_account != account):
        return result
    base = f"https://chatgpt.com/backend-api/accounts/{quote(account, safe='')}"
    spend = _first(data, ("spend_control", "spendControl"))
    if isinstance(spend, Mapping) and codex_individual_limit(data) is None:
        try:
            async with asyncio.timeout(4):
                monthly = parse_codex_monthly(await _get(client, base + "/spend-controls/current-user/monthly-usage", headers))
            if monthly is not None:
                result["windows"] = [monthly, *result["windows"]][:MAX_WINDOWS]
            else:
                result["notes"].append("No enforced individual monthly credit limit reported.")
        except (UsageError, httpx.HTTPError, TimeoutError):
            result["notes"].append("Individual monthly credit usage could not be read.")
    credits = data.get("credits")
    if (isinstance(credits, Mapping) and credits.get("has_credits") is True
            and credits.get("unlimited") is not True and _number(credits.get("balance")) is None):
        try:
            async with asyncio.timeout(4):
                balance = _number((await _get(client, base + "/remaining_balance", headers)).get("balance"))
            if balance is not None:
                result["notes"].append(f"Workspace credits balance: {balance:g}")
        except (UsageError, httpx.HTTPError, TimeoutError):
            result["notes"].append("Workspace credits balance is unavailable to this account.")
    return result


async def _copilot(client: httpx.AsyncClient) -> dict[str, Any]:
    headers = await SIGN_INS["copilot"]().headers()
    return parse_copilot(await _get(client, COPILOT_URL, {
        "authorization": headers["authorization"], "accept": "application/json",
        "user-agent": headers.get("user-agent", "nexus/0.10")}))


async def _opencode(client: httpx.AsyncClient) -> dict[str, Any]:
    headers = await SIGN_INS["opencode"]().headers()
    return parse_opencode(await _get(client, OPENCODE_URL, {**headers, "accept": "application/json"}))


async def _xai_plan(client: httpx.AsyncClient, headers: Mapping[str, str]) -> str:
    """`SuperGrok`/`SuperGrok Lite`/`SuperGrok Heavy` from settings; `""` on any failure."""
    try:
        async with asyncio.timeout(3.0):
            settings = await _get(client, XAI_SETTINGS_URL, headers)
    except (UsageError, TimeoutError):
        return ""
    return _text(settings.get("subscription_tier_display"), 40)


async def _grok(client: httpx.AsyncClient) -> dict[str, Any]:
    headers = await SIGN_INS["grok"]().headers()
    headers = {**headers, "accept": "application/json"}
    billing, plan = await asyncio.gather(_get(client, XAI_BILLING_URL, headers), _xai_plan(client, headers),
                                         return_exceptions=True)
    if isinstance(billing, BaseException):
        raise billing
    return parse_xai(billing, plan if isinstance(plan, str) else "")


def parse_claude_oauth(data: Mapping[str, Any], plan: str = "") -> dict[str, Any]:
    windows = []
    for key, item in data.items():
        if key != "five_hour" and not key.startswith("seven_day"):
            continue
        if not isinstance(item, Mapping):
            continue
        label = "5-hour session" if key == "five_hour" else "Weekly" + (
            " (" + key[len("seven_day_"):].replace("_", " ").title() + ")" if key != "seven_day" else "")
        windows.append(_window(label, _percent(item.get("utilization")), _epoch(item.get("resets_at")),
                               period=FIVE_HOURS if key == "five_hour" else WEEK,
                               kind="five_hour" if key == "five_hour" else "weekly" if key == "seven_day" else "other"))
    notes = []
    extra = data.get("extra_usage")
    if isinstance(extra, Mapping) and extra.get("is_enabled") is True:
        cap, used = _number(extra.get("monthly_limit")), _number(extra.get("used_credits"))
        percent = _percent(extra.get("utilization"))
        if percent is None and cap is not None and cap > 0 and used is not None and used >= 0:
            percent = _percent(used / cap * 100)
        currency = _text(extra.get("currency") or "USD", 8).upper()
        detail = f"{currency} {used / 100:,.2f} of {cap / 100:,.2f}" if cap is not None and used is not None else ""
        if cap is not None and cap > 0:
            label = "Spend limit" if not windows or plan.casefold() == "enterprise" else "Monthly extra usage"
            windows.append(_window(label, percent, None, kind="monthly", detail=detail))
        else:
            notes.append("Extra usage is enabled; no monthly cap reported.")
    if not windows and not notes:
        notes.append("No subscription usage or limit windows reported by Claude.")
    return {"plan": _text(plan, 40).title(), "windows": windows[:MAX_WINDOWS], "notes": notes}


async def _claude(client: httpx.AsyncClient) -> dict[str, Any]:
    executable = cli_path()
    if executable is None:
        raise UsageError("Claude Code is not installed")
    status = await asyncio.to_thread(claude_status)
    plan = status.get("subscriptionType") if isinstance(status, dict) else None
    credential = await asyncio.to_thread(oauth_credentials, child_env())
    if credential is not None:
        token, credential_plan = credential
        try:
            data = await _get(client, CLAUDE_URL, {
                "authorization": f"Bearer {token}", "accept": "application/json",
                "anthropic-beta": "oauth-2025-04-20", "user-agent": "claude-code/2.1.248"})
            return parse_claude_oauth(data, plan or credential_plan)
        except UsageError as exc:
            if not str(exc).startswith(("HTTP 401", "HTTP 403")):
                raise
    text = await interactive_usage(executable, child_env(), CLAUDE_USAGE_TIMEOUT)
    if text is None:
        raise UsageError("Claude CLI usage unavailable; open `claude` and run /usage to check sign-in or setup")
    result = parse_claude(text)
    result["plan"] = _text(plan, 40).title()
    return result


_LIMIT_SOURCES = {
    "codex": _codex,
    "claude": _claude,
    "grok": _grok,
    "copilot": _copilot,
    "opencode": _opencode,
}


def connected_providers() -> list[str]:
    """Every signed-in provider, in the provider table's order."""
    out: list[str] = []
    for pid in LABELS:
        if pid not in SIGN_INS:
            continue
        try:
            logged_in = SIGN_INS[pid]().logged_in()
        except Exception:  # noqa: BLE001 - an unreadable credential store means "not connected" here
            logged_in = False
        if logged_in:
            out.append(pid)
    return out


async def _row(provider: str, client: httpx.AsyncClient) -> dict[str, Any]:
    row: dict[str, Any] = {"id": provider, "label": LABELS[provider], "source": SOURCES.get(provider, ""),
                           "plan": "", "windows": [], "notes": [], "error": None}
    fetcher = _LIMIT_SOURCES.get(provider)
    if fetcher is None:
        row["notes"] = [NO_LIMITS_NOTE]
        return row
    try:
        async with asyncio.timeout(ROW_TIMEOUT):
            row |= await fetcher(client)
    except UsageError as exc:
        row["error"] = str(exc)
    except AuthError as exc:
        row["error"] = _text(exc, 120)
    except TimeoutError:
        row["error"] = "timed out"
    except (httpx.HTTPError, OSError):
        row["error"] = "could not reach the provider"
    except Exception:  # noqa: BLE001 - a row never leaks a traceback or a credential
        row["error"] = "could not read usage"
    return row


async def providers_usage(client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """One row per connected provider; limit windows where the provider reports them."""
    wanted = connected_providers()
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        rows = await asyncio.gather(*(_row(p, client) for p in wanted))
    finally:
        if own:
            await client.aclose()
    return {"fetched": datetime.now(UTC).timestamp(), "providers": list(rows)}


class UsageCache:
    """One durable snapshot shared by clients and the daemon's periodic refresh."""

    def __init__(self, db: sqlite3.Connection, interval: Callable[[], int]) -> None:
        self.db = db
        self.interval = interval
        self.lock = asyncio.Lock()
        self.db.execute("create table if not exists provider_usage_cache (id integer primary key, snapshot text not null)")
        self.db.commit()
        row = self.db.execute("select snapshot from provider_usage_cache where id = 1").fetchone()
        self.value = json.loads(row[0]) if row else {"fetched": None, "providers": []}

    def snapshot(self) -> dict[str, Any]:
        return self.value

    async def refresh(self) -> dict[str, Any]:
        previous = self.value
        async with self.lock:
            if self.value is not previous:
                return self.value
            value = await providers_usage()
            self.db.execute("insert or replace into provider_usage_cache (id, snapshot) values (1, ?)",
                            (json.dumps(value),))
            self.db.commit()
            self.value = value
            return value

    async def run(self) -> None:
        while True:
            fetched = self.value.get("fetched") or 0
            if time.time() >= fetched + self.interval() * 60:
                try:
                    await self.refresh()
                except Exception:  # noqa: BLE001 - retain the last snapshot and retry later
                    await asyncio.sleep(60)
            await asyncio.sleep(2)
