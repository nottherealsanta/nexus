"""Standard OAuth 2.0 device authorization (RFC 8628), shared by Copilot and grok.

`start()` asks for a code; `poll()` waits for the user to approve it, honouring
`authorization_pending` and `slow_down`. Responses are bounded and errors carry
no token material.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from .errors import AuthError

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_RESPONSE_BYTES = 64 * 1024


async def post_form(url: str, form: Mapping[str, str], *, headers: Mapping[str, str] | None = None, label: str) -> tuple[int, dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(url, data=dict(form), headers={"accept": "application/json", **(headers or {})})
    except httpx.HTTPError as exc:
        raise AuthError(f"{label}: sign-in request failed; check connectivity") from exc
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise AuthError(f"{label}: sign-in service returned an oversized response")
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


async def device_flow(
    *,
    label: str,
    code_url: str,
    token_url: str,
    start_form: Mapping[str, str],
    poll_form: Callable[[str], Mapping[str, str]],
    notify: Callable[[str], None],
    headers: Mapping[str, str] | None = None,
    extra_wait: float = 0.0,
) -> dict[str, Any]:
    """Run the whole flow; returns the token response."""
    status, start = await post_form(code_url, start_form, headers=headers, label=label)
    device_code, user_code = start.get("device_code"), start.get("user_code")
    if status >= 400 or not isinstance(device_code, str) or not isinstance(user_code, str):
        raise AuthError(f"{label}: could not start device sign-in (HTTP {status})")
    url = start.get("verification_uri_complete") or start.get("verification_uri")
    notify(f"Open {url} and enter code {user_code}")
    interval = min(max(float(start.get("interval") or 5), 1.0), 60.0)
    deadline = time.time() + min(max(float(start.get("expires_in") or 900), 60.0), 1800.0)
    while time.time() < deadline:
        await asyncio.sleep(interval + extra_wait)
        status, result = await post_form(token_url, poll_form(device_code), headers=headers, label=label)
        error = result.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = min(interval + 5.0, 60.0)
            continue
        if error in ("access_denied", "authorization_declined", "authorization_denied"):
            raise AuthError(f"{label}: sign-in was denied")
        if error == "expired_token":
            raise AuthError(f"{label}: the sign-in code expired; start again")
        if error or status >= 400 or not result.get("access_token"):
            raise AuthError(f"{label}: sign-in failed (HTTP {status}{', ' + str(error) if error else ''})")
        return result
    raise AuthError(f"{label}: sign-in timed out")
