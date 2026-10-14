"""Settings → Providers: one page, one section per provider (connected first).

Each section shows the connection, the sign-in options and, while a sign-in is pending, its code,
page and status, all in place. API keys and sign-in codes are typed in masked fields and sent to the daemon; Antigravity CLI
owns its sign-in and credentials. None are shown in a page or toast.
"""
from __future__ import annotations

import asyncio
import webbrowser
from datetime import datetime

from ....ui_support import settings_page as sp

AREA = "providers"
METHOD_LABELS = {"browser": "Sign in with browser", "device": "Use a device code", "cli": "How to sign in with agy"}


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _login_of(workflows, provider):
    """The pending sign-in for ``provider``: the one this shell started, or one the host kept."""
    login = workflows.login
    if login is not None and _get(login, "provider") == provider["id"] and _get(login, "status") == "pending":
        return login
    kept = provider.get("login")
    if kept and _get(kept, "status") == "pending":
        from ..workflows import login_result, plain_login
        workflows.login = login_result(plain_login(kept))
        return workflows.login
    return None


def _probe_summary(probe: dict) -> str:
    """``12 models · refreshed 2026-10-14 09:30`` or the error, labelled and never silent."""
    count, stamp, error = int(probe.get("count") or 0), float(probe.get("refreshed_at") or 0.0), str(probe.get("error") or "")
    when = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") if stamp > 0 else "never"
    text = f"{count} models · refreshed {when}"
    return f"{text} · last refresh failed: {error}" if error else text


def _section(workflows, provider, expand: str) -> dict:
    pid = provider["id"]
    label = provider.get("label", pid)
    connected = bool(provider.get("connected"))
    login = _login_of(workflows, provider)
    methods = [_get(m, "id", m if isinstance(m, str) else "") for m in provider.get("methods", [])]
    blocks: list[dict] = []
    help_text = provider.get("instruction") or provider.get("help") or ""
    if help_text:
        blocks.append(sp.note(str(help_text)))
    if provider.get("detail"):
        blocks.append(sp.note(str(provider["detail"]), "warning" if not connected and provider.get("cli_present") is not True else "muted"))
    if login is not None:
        if _get(login, "message"):
            blocks.append(sp.note(str(_get(login, "message"))))
        if _get(login, "user_code"):
            blocks.append(sp.row(f"{pid}:user-code", "Code to enter", sp.readout(str(_get(login, "user_code")))))
        if _get(login, "url"):
            blocks.append(sp.row(f"{pid}:url", "Sign-in page", sp.readout(str(_get(login, "url")))))
        if _get(login, "code_entry"):
            blocks.append(sp.row(f"{pid}:code", "Sign-in code", sp.text("", sp.op(AREA, "code"), secret=True, placeholder="paste the code, Enter sends"),
                                 description="Shown on the sign-in page after you approve."))
        buttons = [("Open sign-in page", sp.op(AREA, "open"), "primary"), ("Refresh status", sp.op(AREA, "poll"), "secondary"),
                   ("Cancel sign-in", sp.op(AREA, "cancel"), "ghost")]
        blocks.append(sp.buttons(f"{pid}:login", buttons))
    else:
        if "api_key" in methods:
            blocks.append(sp.row(f"{pid}:key", "API key", sp.text("", sp.op(AREA, "api_key", provider=pid), secret=True,
                                                                    placeholder="replace the key" if connected else "paste your key, Enter saves"),
                                 description="Stored in the daemon's private credential file; never shown again."))
        sign_in = [(METHOD_LABELS.get(m, f"Sign in · {m}"), sp.op(AREA, "login", provider=pid, method=m), "primary" if not connected else "secondary")
                   for m in methods if m and m != "api_key"]
        if sign_in:
            blocks.append(sp.buttons(f"{pid}:methods", sign_in, label="Sign in"))
    if "cli" in methods:
        probe = provider.get("models_probe")
        if isinstance(probe, dict):
            blocks.append(sp.row(f"{pid}:models", "Models from agy", sp.readout(_probe_summary(probe)),
                                 description="Cached until you refresh; the model-list refresh in Settings > Models does not re-run agy."))
        blocks.append(sp.buttons(f"{pid}:refresh-models", [("Refresh models", sp.op(AREA, "refresh_models", provider=pid), "secondary")]))
    if connected and provider.get("can_logout", True) is not False and login is None:
        blocks.append(sp.buttons(f"{pid}:manage", [("Sign out…", sp.op(AREA, "logout_ask", provider=pid, label=label), "danger")], label="Manage"))
    managed = provider.get("cli_present") is True and not connected and login is None  # sign-in is unverifiable, not absent
    status = ("sign-in pending" if login is not None else "connected" if connected
              else "CLI available; sign-in managed by agy" if managed else "not connected")
    tone = "success" if connected else "warning" if login is not None else ""
    return sp.section(f"prov-{pid}", label, blocks, summary=status, tone=tone,
                      open_=connected or login is not None or expand == pid)


async def build(workflows) -> dict:
    result = await workflows.client.providers_status()
    expand = workflows.page_state.setdefault(AREA, {}).get("expand", "")
    providers = sorted(result.providers, key=lambda row: (not row.get("connected"), str(row.get("label", row["id"])).casefold()))
    blocks = [sp.note("Nexus-managed credentials stay in the daemon (~/.nexus/credentials.json); Antigravity CLI owns its own sign-in. Connect more than one provider to fall back between them."),
              sp.gap()]
    try:  # first run: say what to do next, with the one action that finishes setup
        setup = await workflows.client.setup_status()
        if getattr(setup, "required", False):
            blocks.insert(0, sp.callout("info", "Connect a provider, then choose your default model.",
                                        {"label": "Choose default model", "operation": {"kind": "setup"}}))
            blocks.insert(1, sp.gap())
    except Exception:  # noqa: BLE001 - the page still works without the setup hint
        pass
    for provider in providers:
        blocks.append(_section(workflows, provider, expand))
    if not providers:
        blocks.append(sp.note("The host reports no providers.", "warning"))
    connected = sum(bool(row.get("connected")) for row in providers)
    from . import remember_provider_count
    remember_provider_count(workflows, connected, len(providers))
    return sp.page(AREA, "Providers", blocks, footer=f"{connected} of {len(providers)} connected · credentials are stored by the daemon, not in config")


async def handle(workflows, operation) -> None:
    client = workflows.client
    key = operation["key"]
    flash = workflows.shell.flash
    if key == "refresh_models":
        workflows.page_state.setdefault(AREA, {})["expand"] = operation["provider"]
        result = await client.provider_models_refresh(operation["provider"])
        flash(str(_get(result, "message", "") or "Models refreshed") + "; sign-in remains managed by agy",
              "warning" if _get(result, "error") else "info")
    elif key == "login":
        workflows.page_state.setdefault(AREA, {})["expand"] = operation["provider"]
        started = await client.provider_login(operation["provider"], operation.get("method", ""))
        if _get(started, "status") == "manual":  # the provider's CLI owns sign-in: show the steps, track nothing
            workflows.login = None
            flash(str(_get(started, "message", ""))[:300] or "Sign in with the provider's own CLI", "info")
        else:
            workflows.login = started
    elif key == "open":
        login = workflows.login
        if login is not None and str(_get(login, "url", "")).startswith("https://"):
            await asyncio.to_thread(webbrowser.open, login.url)
    elif key == "poll":
        if workflows.login is None:
            raise ValueError("No sign-in is pending")
        workflows.login = await client.provider_login_poll(workflows.login.login_id)
        if workflows.login.status != "pending":  # finished: say what the host said, then fresh provider state
            done = workflows.login
            workflows.login = None
            flash(str(_get(done, "message", "") or done.status), "success" if done.status == "connected" else "warning")
    elif key == "code":
        code = str(operation.get("value", "")).strip()
        if not code:
            raise ValueError("Paste the code shown after signing in first")
        if workflows.login is None:
            raise ValueError("No sign-in is pending")
        result = await client.provider_login_code(workflows.login.login_id, code)
        flash(str(_get(result, "message", "") or "Code sent"), "info")
    elif key == "cancel":
        login, workflows.login = workflows.login, None
        if login is not None:
            try:
                await client.provider_login_cancel(login.login_id)
            except Exception:  # noqa: BLE001 - an already-finished sign-in is not worth stranding the page for
                pass
        flash("Sign-in cancelled", "info")
    elif key == "api_key":
        secret = str(operation.get("value", "")).strip()
        if not secret:
            raise ValueError("Paste your API key first")
        workflows.page_state.setdefault(AREA, {})["expand"] = operation["provider"]
        result = await client.provider_key_set(operation["provider"], secret)
        message = _get(result, "message", "")
        flash(message if isinstance(message, str) and message else "Key saved", "success")
    elif key == "logout_ask":
        label = operation.get("label") or operation["provider"]
        workflows.menu(f"Sign out of {label}?", [("Cancel", {"kind": "back"}), ("Sign out", sp.op(AREA, "logout", provider=operation["provider"]))],
                       ["The credential is removed from the daemon. You can connect again at any time."])
    elif key == "logout":
        result = await client.provider_logout(operation["provider"])
        flash(str(_get(result, "message", "") or "Signed out"), "success")
        workflows.return_to_page()
    else:
        raise ValueError(f"Unknown Providers operation {key!r}")
