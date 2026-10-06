"""Settings → Providers: one page, one section per provider (connected first).

Each section shows the connection, the sign-in options and, while a sign-in is pending, its code,
page and status, all in place. Credentials never come back: API keys and sign-in codes are typed in
masked fields, sent to the daemon, and never appear in a page or a toast.
"""
from __future__ import annotations

import asyncio
import webbrowser

from ....ui_support import settings_page as sp

AREA = "providers"
METHOD_LABELS = {"browser": "Sign in with browser", "device": "Use a device code"}


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
        blocks.append(sp.note(str(provider["detail"]), "warning" if not connected else "muted"))
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
    if connected and provider.get("can_logout", True) is not False and login is None:
        blocks.append(sp.buttons(f"{pid}:manage", [("Sign out…", sp.op(AREA, "logout_ask", provider=pid, label=label), "danger")], label="Manage"))
    status = "sign-in pending" if login is not None else "connected" if connected else "not connected"
    tone = "success" if connected else "warning" if login is not None else ""
    return sp.section(f"prov-{pid}", label, blocks, summary=status, tone=tone,
                      open_=connected or login is not None or expand == pid)


async def build(workflows) -> dict:
    result = await workflows.client.providers_status()
    expand = workflows.page_state.setdefault(AREA, {}).get("expand", "")
    providers = sorted(result.providers, key=lambda row: (not row.get("connected"), str(row.get("label", row["id"])).casefold()))
    blocks = [sp.note("Credentials stay in the daemon (~/.nexus/credentials.json). Connect more than one provider to fall back between them."),
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
    return sp.page(AREA, "Providers", blocks, footer=f"{connected} of {len(providers)} connected · credentials are stored by the daemon, not in config")


async def handle(workflows, operation) -> None:
    client = workflows.client
    key = operation["key"]
    flash = workflows.shell.flash
    if key == "login":
        workflows.page_state.setdefault(AREA, {})["expand"] = operation["provider"]
        workflows.login = await client.provider_login(operation["provider"], operation.get("method", ""))
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
