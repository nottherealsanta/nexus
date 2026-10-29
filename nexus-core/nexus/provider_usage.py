"""Plan limits for the providers that report them: codex and copilot (PLAN K11).

Re-implemented from old Nexus `host_support/provider_usage.py` over the credentials `auth.json` already holds.

* `codex`: `GET chatgpt.com/backend-api/wham/usage` with the ChatGPT headers. `rate_limit.primary_window` and
  `secondary_window` (and every `additional_rate_limits[]` entry) carry `used_percent`, `reset_at` and
  `limit_window_seconds`.
* `copilot`: `GET api.github.com/copilot_internal/user` with the stored GitHub token. `quota_snapshots` give a
  monthly `percent_remaining` per quota, reset at `quota_reset_date_utc`.

Only signed-in providers are asked, concurrently, each bounded in time and size. A failure is that row's
`error` (a short reason, never a credential). A row is `{id, label, plan, windows, notes, error}`; a window is
`{label, used_percent, resets_at (epoch seconds or None), period_seconds, kind, detail}` where `kind` is
`five_hour`, `weekly`, `monthly` or `other` (per-model side quotas are `other`).
"""
from __future__ import annotations

import asyncio
import calendar
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from .llmhub.auth import SIGN_INS, AuthError

CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
COPILOT_URL = "https://api.github.com/copilot_internal/user"
LABELS = {"codex": "ChatGPT (Codex)", "copilot": "GitHub Copilot"}
SOURCES = {"codex": "chatgpt.com · wham/usage", "copilot": "api.github.com · copilot_internal/user"}
MAX_BODY = 512 * 1024
MAX_WINDOWS = 16
FIVE_HOURS, WEEK = 18_000, 604_800


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
    return {"plan": _text(data.get("copilot_plan"), 40).title(), "windows": windows, "notes": notes}


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


async def _row(provider: str, client: httpx.AsyncClient) -> dict[str, Any]:
    row: dict[str, Any] = {"id": provider, "label": LABELS[provider], "source": SOURCES[provider], "plan": "",
                           "windows": [], "notes": [], "error": None}
    try:
        auth = SIGN_INS[provider]()
        headers = await auth.headers()
        if provider == "codex":
            row |= parse_codex(await _get(client, CODEX_URL, {**headers, "accept": "application/json"}))
        else:
            row |= parse_copilot(await _get(client, COPILOT_URL, {
                "authorization": headers["authorization"], "accept": "application/json",
                "user-agent": headers.get("user-agent", "nexus/0.10")}))
    except UsageError as exc:
        row["error"] = str(exc)
    except AuthError as exc:
        row["error"] = _text(exc, 120)
    except (TimeoutError, httpx.HTTPError, OSError):
        row["error"] = "could not reach the provider"
    return row


async def providers_usage(client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """One row per signed-in provider that reports limits."""
    wanted = [p for p in LABELS if p in SIGN_INS and SIGN_INS[p]().logged_in()]
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        rows = await asyncio.wait_for(asyncio.gather(*(_row(p, client) for p in wanted)), timeout=40)
    except TimeoutError:
        rows = [{"id": p, "label": LABELS[p], "source": SOURCES[p], "plan": "", "windows": [], "notes": [],
                 "error": "timed out"} for p in wanted]
    finally:
        if own:
            await client.aclose()
    return {"fetched": datetime.now(UTC).timestamp(), "providers": list(rows)}
