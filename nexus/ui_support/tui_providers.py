"""Settings → Providers pane: sign in to Codex, GitHub Copilot and OpenCode Go (plan section 7).

Every action is a host command (``ProvidersStatus``, ``ProviderLogin`` +
``ProviderLoginPoll``, ``ProviderKeySet``, ``ProviderLogout``); the daemon keeps
credentials in the system keychain. A browser or device sign-in shows its URL
and code here and opens the URL; the pane polls until the daemon reports the
result. Closing Settings leaves a device sign-in running; reopening resumes it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from .text import sanitize

#: ``(id, label, actions)`` mirrored from the host; the host remains authoritative.
PROVIDERS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("codex", "ChatGPT (Codex)", (("browser", "Sign in with browser"), ("device", "Use a device code"))),
    ("github-copilot", "GitHub Copilot", (("device", "Use a device code"),)),
    ("opencode-go", "OpenCode Go", (("api_key", "Save key"),)),
)
_POLL_SECONDS = 1.5
_POLL_LIMIT = 600


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


class ProvidersPane(VerticalScroll):
    """One card per provider: state, help, sign-in actions, and flow status."""

    #: Tests turn this off so a sign-in never launches a real browser.
    open_links = True

    def __init__(self, client: Any | None, *, heading: bool = True) -> None:
        super().__init__(id="providers")
        self._client = client
        #: First-run setup embeds the cards without the Settings heading.
        self._heading = heading
        self._polling: set[str] = set()
        self._login_ids: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        if self._heading:
            yield Static("Providers", classes="settings-heading", markup=False)
            yield Static(
                "Sign in to model providers. Credentials stay in this machine's keychain.",
                classes="settings-help", markup=False,
            )
        for provider, label, actions in PROVIDERS:
            with Vertical(classes="provider-card", id=f"provider-{provider}"):
                with Horizontal(classes="provider-head"):
                    yield Static(label, classes="provider-name", markup=False)
                    yield Static("", classes="provider-state", markup=False)
                yield Static("", classes="provider-help", markup=False)
                with Horizontal(classes="provider-actions"):
                    if provider == "opencode-go":
                        yield Input(placeholder="OpenCode Go API key", password=True, id="provider-go-key",
                                    classes="provider-input")
                    for method, text in actions:
                        yield Button(text, name=f"{provider}|{method}", classes="provider-action")
                    yield Button("Cancel", name=f"{provider}|cancel", classes="provider-cancel")
                    yield Button("Disconnect", name=f"{provider}|logout", classes="provider-logout")
                yield Static("", classes="provider-flow", markup=False)

    def on_mount(self) -> None:
        for button in self.query(".provider-cancel"):
            button.display = False
        self.reload()

    def reload(self) -> None:
        self.run_worker(self._load(), group="providers-status", exclusive=True)

    def _card(self, provider: str) -> Vertical:
        return self.query_one(f"#provider-{provider}", Vertical)

    def _flow(self, provider: str, text: str) -> None:
        if self.is_mounted:
            self._card(provider).query_one(".provider-flow", Static).update(text)

    def _pending(self, provider: str, pending: bool) -> None:
        card = self._card(provider)
        card.set_class(pending, "-pending")
        for button in card.query(".provider-action"):
            button.display = not pending
        card.query_one(".provider-cancel", Button).display = pending

    async def _load(self) -> None:
        if self._client is None:
            return
        try:
            result = await self._client.providers_status()
        except Exception as exc:  # noqa: BLE001 - show the host/transport error
            for provider, _, _ in PROVIDERS:
                self._flow(provider, f"Status unavailable: {sanitize(str(exc), 120)}")
            return
        if not self.is_mounted:
            return
        for row in list(_field(result, "providers", ()))[:8]:
            provider = str(_field(row, "id", ""))
            if provider not in {key for key, _, _ in PROVIDERS}:
                continue
            card = self._card(provider)
            connected = _field(row, "connected") is True
            detail = sanitize(str(_field(row, "detail", "") or ""), 60)
            state = ("● connected" + (f" · {detail}" if detail else "")) if connected else "○ not connected"
            card.query_one(".provider-state", Static).update(state)
            card.set_class(connected, "-connected")
            card.query_one(".provider-help", Static).update(sanitize(str(_field(row, "help", "")), 200))
            card.query_one(".provider-logout", Button).display = connected
            login = _field(row, "login")
            if login and _field(login, "status") == "pending":
                self._show_login(provider, login)
                self._watch(provider, str(_field(login, "login_id", "")))

    def _show_login(self, provider: str, login: Any) -> None:
        url = sanitize(str(_field(login, "url", "")), 400)
        code = sanitize(str(_field(login, "user_code", "")), 32)
        text = f"Enter code {code} at {url}" if code else f"Finish signing in at {url}"
        self._flow(provider, f"{text}\nWaiting for approval…")
        self._pending(provider, True)
        self._login_ids[provider] = str(_field(login, "login_id", ""))

    async def _sign_in(self, provider: str, method: str) -> None:
        domain = ""
        self._flow(provider, "Starting sign-in…")
        try:
            login = await self._client.provider_login(provider, method, domain)
        except Exception as exc:  # noqa: BLE001 - host validation error
            self._flow(provider, sanitize(str(exc), 200))
            return
        self._show_login(provider, login)
        url = str(_field(login, "url", ""))
        if self.open_links and url.startswith("https://"):
            self.app.open_url(url)
        self._watch(provider, str(_field(login, "login_id", "")))

    def _watch(self, provider: str, login_id: str) -> None:
        if login_id and login_id not in self._polling:
            self._polling.add(login_id)
            self.run_worker(self._poll(provider, login_id), group=f"provider-poll-{provider}")

    async def _poll(self, provider: str, login_id: str) -> None:
        try:
            for _ in range(_POLL_LIMIT):
                await asyncio.sleep(_POLL_SECONDS)
                try:
                    login = await self._client.provider_login_poll(login_id)
                except Exception as exc:  # noqa: BLE001 - expired or unknown login
                    self._flow(provider, sanitize(str(exc), 200))
                    break
                if _field(login, "status") != "pending":
                    self._flow(provider, sanitize(str(_field(login, "message", "")), 240))
                    break
        finally:
            self._polling.discard(login_id)
            if self.is_mounted:
                self._pending(provider, False)
                await self._load()

    async def _cancel(self, provider: str) -> None:
        login_id = self._login_ids.get(provider, "")
        if login_id:
            try:
                await self._client.provider_login_cancel(login_id)
            except Exception as exc:  # noqa: BLE001 - already finished
                self._flow(provider, sanitize(str(exc), 200))

    async def _save_key(self, provider: str) -> None:
        field = self.query_one("#provider-go-key", Input)
        key, field.value = field.value, ""
        if not key.strip():
            self._flow(provider, "Paste your OpenCode Go API key first.")
            return
        try:
            result = await self._client.provider_key_set(provider, key)
        except Exception as exc:  # noqa: BLE001 - validation error (never echoes the key)
            self._flow(provider, sanitize(str(exc), 200))
            return
        self._flow(provider, sanitize(str(_field(result, "message", "")), 240))
        await self._load()

    async def _logout(self, provider: str) -> None:
        try:
            result = await self._client.provider_logout(provider)
        except Exception as exc:  # noqa: BLE001 - keychain error
            self._flow(provider, sanitize(str(exc), 200))
            return
        self._flow(provider, sanitize(str(_field(result, "message", "")), 240))
        await self._load()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "provider-go-key":
            event.stop()
            self.run_worker(self._save_key("opencode-go"), group="provider-key", exclusive=True)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        provider, _, action = (event.button.name or "").partition("|")
        if not provider or self._client is None:
            return
        event.stop()
        if action == "logout":
            work = self._logout(provider)
        elif action == "cancel":
            work = self._cancel(provider)
        elif action == "api_key":
            work = self._save_key(provider)
        else:
            work = self._sign_in(provider, action)
        self.run_worker(work, group=f"provider-{provider}", exclusive=True)


__all__ = ["PROVIDERS", "ProvidersPane"]
