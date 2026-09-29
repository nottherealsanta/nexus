"""Provider sign-in behind the host boundary: Settings → Providers (plan section 7).

Three providers connect from any surface:

* ``codex`` — ChatGPT sign-in in the browser (PKCE, local callback on port
  1455) or with a device code.
* ``github-copilot`` — no sign-in. Nexus has no GitHub OAuth app and will not
  use OpenCode's. A token already in the keychain can still be disconnected.
* ``opencode-go`` — an OpenCode Go API key pasted once.

Credentials live in the secure native keychain of the daemon's machine. OAuth
tokens never cross the wire; a pasted API key crosses **inward only**
(``ProviderKeySet``) and is never echoed, logged, or written to config. A
sign-in URL and a short user code are returned so a client can show or open
them. Each flow runs as a bounded daemon task that clients poll by an opaque
``login_id``. A successful connection also writes the provider's
``[providers.<id>]`` route to ``~/.nexus/config.toml``; routes are built at
startup, so the daemon needs a restart before the provider serves turns.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..auth.api_key import StoredKeyAuth, validate_api_key
from ..auth.codex import CodexOAuthManager
from ..auth.copilot import CopilotAuthManager, api_base_url
from ..errors import ConfigError
from ..util import redact_secrets
from . import settings_inventory
from .settings_scope import settings_target

OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"

#: ``id -> (label, sign-in methods, one-line help)``; the first method is the default.
PROVIDERS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "codex": (
        "ChatGPT (Codex)", ("browser", "device"),
        "Sign in with your ChatGPT Plus or Pro account. This is not an OpenAI API key.",
    ),
    "github-copilot": (
        "GitHub Copilot", (),
        "Nexus does not sign in through OpenCode's GitHub app. Copilot needs its own OAuth app.",
    ),
    "opencode-go": (
        "OpenCode Go", ("api_key",),
        "Paste the API key from your OpenCode Go subscription (opencode.ai/auth).",
    ),
}
_LOGIN_TTL = 900.0
_MAX_LOGINS = 8
_READY_TIMEOUT = 15.0


@dataclass
class _Login:
    id: str
    provider: str
    method: str
    started: float
    status: str = "starting"
    url: str = ""
    user_code: str = ""
    message: str = ""
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def view(self) -> dict[str, Any]:
        return {
            "login_id": self.id, "provider": self.provider, "method": self.method,
            "status": self.status, "url": self.url, "user_code": self.user_code,
            "message": self.message,
        }


def _factory(runtime: object, name: str, default: Callable[..., Any]) -> Callable[..., Any]:
    return getattr(runtime, name, None) or default


def _codex(runtime: object) -> Any:
    return _factory(runtime, "_codex_auth_factory", CodexOAuthManager)(profile="default")


def _copilot(runtime: object) -> Any:
    return _factory(runtime, "_copilot_auth_factory", CopilotAuthManager)(profile="default")


def _api_key(runtime: object, provider: str) -> Any:
    return _factory(runtime, "_api_key_auth_factory", StoredKeyAuth)(provider, profile="default")


def _manager(runtime: object, provider: str) -> Any:
    if provider == "codex":
        return _codex(runtime)
    if provider == "github-copilot":
        return _copilot(runtime)
    if provider == "opencode-go":
        return _api_key(runtime, provider)
    raise ConfigError("unknown provider")


def _safe(exc: BaseException) -> str:
    """A bounded, redacted message; a keychain or HTTP error never leaks a value."""
    text = str(exc) if isinstance(exc, (ConfigError, ValueError)) or type(exc).__name__.endswith("Error") else ""
    return redact_secrets(text or "sign-in failed")[:240]


async def copilot_domain(runtime: object) -> str | None:
    """The GitHub domain the stored Copilot token belongs to, if any."""
    try:
        return await _copilot(runtime).domain()
    except Exception:  # noqa: BLE001 - status is best-effort
        return None


async def connected(runtime: object, provider: str) -> bool:
    """Whether the keychain holds a credential for ``provider`` (never the value)."""
    try:
        return bool(await _manager(runtime, provider).status())
    except Exception:  # noqa: BLE001 - an unavailable keychain reads as not connected
        return False


def _logins(runtime: object) -> dict[str, _Login]:
    table = getattr(runtime, "_provider_logins", None)
    if table is None:
        table = {}
        runtime._provider_logins = table  # type: ignore[attr-defined]
    now = time.monotonic()
    for key, login in list(table.items()):
        if now - login.started > _LOGIN_TTL + 60 and (login.task is None or login.task.done()):
            del table[key]
    return table


def _route(provider: str, domain: str | None = None) -> tuple[tuple[str, str], ...]:
    if provider == "codex":
        return (("auth", "chatgpt_oauth"), ("profile", "default"), ("api", "responses"))
    if provider == "github-copilot":
        return (("auth", "github_copilot"), ("base_url", api_base_url(domain)), ("api", "chat"))
    return (("auth", "keychain"), ("base_url", OPENCODE_GO_BASE_URL), ("api", "chat"))


def write_global_keys(runtime: object, updates: tuple[tuple[str, str, str], ...]) -> None:
    """Set ``(table, key, value)`` rows in ``~/.nexus/config.toml`` (v2 only)."""
    target = settings_target(runtime, "global", "config", "config")
    try:
        with target.path.open("rb") as stream:
            old = stream.read(settings_inventory.MAX_BODY + 1)
    except FileNotFoundError:
        old = b""
    except OSError as exc:
        raise ConfigError("cannot read global config") from exc
    if len(old) > settings_inventory.MAX_BODY:
        raise ConfigError("global config exceeds 256 KiB")
    try:
        text = old.decode("utf-8")
        document = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("cannot update invalid global config") from exc
    if document and document.get("config_version") != 2:
        raise ConfigError("global config is v1; convert it to config_version = 2 first")
    body = text if document else "config_version = 2\n\n"
    for table, key, value in updates:
        body = settings_inventory.set_toml_key(body, table, key, value)
    expected = hashlib.sha256(old).hexdigest() if old else ""
    try:
        result = settings_inventory.write(runtime, "global", "config", "config", body, expected)
    except (ConfigError, OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("could not save global config") from exc
    if result.get("status") != "written":
        raise ConfigError("global config changed while saving; try again")


def provider_route(provider: str, domain: str | None = None) -> tuple[tuple[str, str], ...]:
    """The ``[providers.<id>]`` keys a connected provider needs."""
    return _route(provider, domain)


def _save_route(runtime: object, provider: str, domain: str | None = None) -> str:
    try:
        write_global_keys(runtime, tuple((f"providers.{provider}", key, value) for key, value in _route(provider, domain)))
    except ConfigError as exc:
        return f"Connected, but the route was not saved: {_safe(exc)}"
    return "Connected. Restart the daemon to use it (nexus daemon stop)."


async def providers_status(runtime: object) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    logins = _logins(runtime)
    for provider, (label, methods, help_text) in PROVIDERS.items():
        detail = (await copilot_domain(runtime) or "") if provider == "github-copilot" else ""
        active = next((login for login in logins.values()
                       if login.provider == provider and login.status == "pending"), None)
        rows.append({
            "id": provider, "label": label, "methods": list(methods), "help": help_text,
            "connected": await connected(runtime, provider), "detail": detail,
            "login": active.view() if active else None,
        })
    return {"providers": rows}


async def provider_login(runtime: object, provider: str, method: str = "", domain: str = "") -> dict[str, Any]:
    """Start one sign-in; returns the URL and code once they are known."""
    if provider not in PROVIDERS:
        raise ConfigError("unknown provider")
    methods = PROVIDERS[provider][1]
    method = method or (methods[0] if methods else "")
    if method not in methods or method == "api_key":
        if provider == "github-copilot":
            raise ConfigError("Nexus does not sign in through OpenCode's GitHub app")
        raise ConfigError(f"{PROVIDERS[provider][0]} does not support {method or 'that'} sign-in")
    host = None
    logins = _logins(runtime)
    # One flow per provider: a new attempt replaces an abandoned one.
    for login in list(logins.values()):
        if login.provider == provider and login.task is not None and not login.task.done():
            login.cancel.set()
            login.task.cancel()
    if sum(1 for login in logins.values() if login.task is not None and not login.task.done()) >= _MAX_LOGINS:
        raise ConfigError("too many sign-ins in progress")
    login = _Login(id=secrets.token_urlsafe(16), provider=provider, method=method, started=time.monotonic())
    logins[login.id] = login

    def show(url: str, code: str = "") -> bool:
        login.url, login.user_code, login.status = url, code, "pending"
        login.ready.set()
        return True

    async def run() -> None:
        manager = _manager(runtime, provider)
        try:
            async with asyncio.timeout(_LOGIN_TTL):
                if provider == "codex" and method == "browser":
                    # The client opens the URL; the daemon never launches a browser.
                    await manager.browser_login(notify=lambda _text: None, browser_open=lambda url: show(url))
                elif provider == "codex":
                    await manager.device_login(on_code=show, cancel=login.cancel)
                else:
                    await manager.device_login(domain=host, on_code=show, cancel=login.cancel)
        except asyncio.CancelledError:
            login.status, login.message = "cancelled", "Sign-in cancelled."
        except TimeoutError:
            login.status, login.message = "failed", "Sign-in timed out."
        except OSError as exc:
            login.status = "failed"
            login.message = ("Port 1455 is in use (is another Codex sign-in open?). Try the device code."
                             if provider == "codex" and method == "browser" else _safe(exc))
        except Exception as exc:  # noqa: BLE001 - reported to the client, redacted
            login.status, login.message = "failed", _safe(exc)
        else:
            login.status = "connected"
            login.message = await asyncio.to_thread(_save_route, runtime, provider, host)
        finally:
            login.url = login.url if login.status == "pending" else ""
            login.ready.set()

    login.task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(login.ready.wait(), _READY_TIMEOUT)
    except TimeoutError:
        login.cancel.set()
        login.task.cancel()
        raise ConfigError("the sign-in service did not respond; check connectivity") from None
    if login.status in ("failed", "cancelled"):
        raise ConfigError(login.message or "sign-in failed")
    return login.view()


async def provider_login_poll(runtime: object, login_id: str) -> dict[str, Any]:
    login = _logins(runtime).get(login_id)
    if login is None:
        raise ConfigError("unknown or expired sign-in")
    return login.view()


async def provider_login_cancel(runtime: object, login_id: str) -> dict[str, Any]:
    login = _logins(runtime).get(login_id)
    if login is None:
        raise ConfigError("unknown or expired sign-in")
    if login.task is not None and not login.task.done():
        login.cancel.set()
        login.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(login.task), 5)
        except (asyncio.CancelledError, TimeoutError):
            pass
        login.status, login.message, login.url = "cancelled", "Sign-in cancelled.", ""
    return login.view()


async def provider_key_set(runtime: object, provider: str, key: str) -> dict[str, Any]:
    if provider not in PROVIDERS or "api_key" not in PROVIDERS[provider][1]:
        raise ConfigError("this provider does not use an API key")
    try:
        await _api_key(runtime, provider).save(validate_api_key(key))
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - keychain failures, never the key
        raise ConfigError(f"could not store the key: {_safe(exc)}") from None
    message = await asyncio.to_thread(_save_route, runtime, provider)
    return {"provider": provider, "connected": True, "message": message}


async def provider_logout(runtime: object, provider: str) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise ConfigError("unknown provider")
    try:
        await _manager(runtime, provider).logout()
    except Exception as exc:  # noqa: BLE001 - keychain failures
        raise ConfigError(f"could not remove the credential: {_safe(exc)}") from None
    return {"provider": provider, "connected": False, "message": "Disconnected. The credential was removed from the keychain."}


async def dispatch_providers(command: Any, runtime: object) -> Any | None:
    """Handle the ``Provider*`` host commands, or return ``None``."""
    from ..host import protocol as p

    if isinstance(command, p.ProvidersStatus):
        return p.ProvidersStatusResult(**await providers_status(runtime))
    if isinstance(command, p.ProviderLogin):
        return p.ProviderLoginResult(**await provider_login(runtime, command.provider, command.method, command.domain))
    if isinstance(command, p.ProviderLoginPoll):
        return p.ProviderLoginResult(**await provider_login_poll(runtime, command.login_id))
    if isinstance(command, p.ProviderLoginCancel):
        return p.ProviderLoginResult(**await provider_login_cancel(runtime, command.login_id))
    if isinstance(command, p.ProviderKeySet):
        return p.ProviderAuthResult(**await provider_key_set(runtime, command.provider, command.key))
    if isinstance(command, p.ProviderLogout):
        return p.ProviderAuthResult(**await provider_logout(runtime, command.provider))
    return None


__all__ = [
    "OPENCODE_GO_BASE_URL",
    "PROVIDERS",
    "connected",
    "copilot_domain",
    "dispatch_providers",
    "provider_key_set",
    "provider_login",
    "provider_login_cancel",
    "provider_login_poll",
    "provider_logout",
    "provider_route",
    "providers_status",
    "write_global_keys",
]
