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
* `claude`: the official CLI's `-p /usage` command, a local command that makes no model request. The output is
  `Label: N% used · resets …` lines.

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
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .llmhub.auth import SIGN_INS, AuthError
from .llmhub.auth.claude import child_env, cli_path
from .llmhub.auth.claude import status as claude_status
from .llmhub.providers import PROVIDERS

CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
COPILOT_URL = "https://api.github.com/copilot_internal/user"
OPENCODE_URL = "https://opencode.ai/zen/go/v1/usage"
XAI_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
XAI_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
LABELS = {pid: info.label for pid, info in PROVIDERS.items() if pid != "mock"}
SOURCES = {
    "codex": "chatgpt.com · wham/usage",
    "claude": "claude CLI · /usage",
    "grok": "cli-chat-proxy.grok.com · v1/billing",
    "copilot": "api.github.com · copilot_internal/user",
    "opencode": "opencode.ai · zen/go/v1/usage",
}
NO_LIMITS_NOTE = "No plan limits; token usage is in the session totals."
MAX_BODY = 512 * 1024
MAX_WINDOWS = 16
FIVE_HOURS, WEEK = 18_000, 604_800
ROW_TIMEOUT = 40.0
CLAUDE_USAGE_TIMEOUT = 30.0
CLAUDE_MAX_OUTPUT = 256 * 1024


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
    extra = data.get("additional_rate_limits")
    for item in extra[:8] if isinstance(extra, list) else ():
        if isinstance(item, Mapping):
            name = _text(item.get("limit_name") or item.get("metered_feature"), 60)
            add(item.get("rate_limit"), f"{name} " if name else "", kind="other")  # side quotas never gate the provider
    add(data.get("code_review_rate_limit"), "Code review ", kind="other")
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
    return {"plan": _text(data.get("plan_type"), 40).title(), "windows": windows[:MAX_WINDOWS], "notes": notes}


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
    windows: list[dict[str, Any]] = []
    for line in text.splitlines()[:200]:
        match = _CLAUDE_USAGE.match(line)
        if match is None:
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
    if not windows:
        line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        raise UsageError(_text(line) or "the Claude CLI printed no usage limits")
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
    return parse_codex(await _get(client, CODEX_URL, {**headers, "accept": "application/json"}))


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


async def _run_claude(argv: list[str], timeout: float) -> tuple[int, bytes] | None:
    process = None
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                *argv, env=child_env(), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > CLAUDE_MAX_OUTPUT:
                    return None
            return await process.wait(), bytes(output)
    except (OSError, TimeoutError):
        return None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def _claude(_client: httpx.AsyncClient) -> dict[str, Any]:
    executable = cli_path()
    if executable is None:
        raise UsageError("Claude Code is not installed")
    argv = [executable, "-p", "/usage", "--output-format", "json", "--setting-sources", "",
            "--no-session-persistence", "--strict-mcp-config"]
    ran = await _run_claude(argv, CLAUDE_USAGE_TIMEOUT)
    if ran is None:
        raise UsageError("the Claude CLI did not answer /usage in time")
    code, output = ran
    try:
        value = json.loads(output)
    except ValueError:
        raise UsageError("the Claude CLI returned unreadable /usage output") from None
    text = value.get("result") if isinstance(value, dict) else None
    if code != 0 or not isinstance(text, str) or value.get("is_error"):
        raise UsageError("the Claude CLI could not read usage; run `claude auth login`")
    result = parse_claude(text)
    status = await asyncio.to_thread(claude_status)
    plan = status.get("subscriptionType") if isinstance(status, dict) else None
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
