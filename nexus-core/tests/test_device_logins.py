"""O4: `nexus login copilot|grok|opencode` against scripted sign-in servers, through to the first provider request.

No network: `httpx.AsyncClient` is routed to a handler that plays GitHub, auth.x.ai, the Copilot API
and the Grok proxy. The wire shapes (form fields, polling errors, headers) are the ones in docs/auth.md.
"""
import asyncio
import json
import stat
from urllib.parse import parse_qs

import httpx
import pytest

from nexus.cli import main
from nexus.llmhub.auth import SIGN_INS, AuthError, CopilotAuth, CredentialStore, GrokAuth
from nexus.llmhub.auth import copilot as copilot_module
from nexus.llmhub.auth import device as device_module
from nexus.llmhub.auth import grok as grok_module
from nexus.llmhub.chat import ChatProvider
from nexus.llmhub.hub import connected
from nexus.llmhub.providers import PROVIDERS
from nexus.llmhub.responses import ResponsesProvider
from nexus.llmhub.types import Message, ProviderError, Request, Text, WireRequest

GITHUB_CODE = "https://github.com/login/device/code"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
COPILOT_MODELS = "https://api.githubcopilot.com/models"
GROK_CODE = "https://auth.x.ai/oauth2/device/code"
GROK_TOKEN = "https://auth.x.ai/oauth2/token"


class Server:
    """Scripted endpoints. `script[url]` is a list of (status, json) answers, the last one repeating."""

    def __init__(self):
        self.script: dict[str, list[tuple[int, dict]]] = {}
        self.requests: list[httpx.Request] = []

    def forms(self, url):
        return [{k: v[0] for k, v in parse_qs(r.content.decode()).items()} for r in self.requests if str(r.url) == url]

    def called(self, url):
        return [r for r in self.requests if str(r.url) == url]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answers = self.script.get(str(request.url).split("?")[0])
        if answers is None:
            raise httpx.ConnectError(f"unscripted {request.url}")
        status, body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx.Response(status, json=body)


@pytest.fixture
def server(monkeypatch, tmp_path):
    srv = Server()
    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(srv)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(device_module.asyncio, "sleep", sleep)
    srv.sleeps = sleeps
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    for env in ("OPENCODE_API_KEY", grok_module.VERSION_ENV):
        monkeypatch.delenv(env, raising=False)
    return srv


def _start(user_code="ABCD-1234", **extra):
    return (200, {"device_code": "dev-1", "user_code": user_code, "verification_uri": "https://example.test/device",
                  "interval": 5, "expires_in": 900, **extra})


def _stored(tmp_path):
    return json.loads((tmp_path / "home" / "auth.json").read_text())


# -- copilot -----------------------------------------------------------------


def _copilot_script(server, *polls, models=(200, {"data": []})):
    server.script[GITHUB_CODE] = [_start()]
    server.script[GITHUB_TOKEN] = list(polls)
    server.script[COPILOT_MODELS] = [models]


def test_copilot_login_waits_for_approval_then_verifies_and_stores_only_the_github_token(server, tmp_path):
    _copilot_script(server, (200, {"error": "authorization_pending"}), (200, {"error": "slow_down"}),
                    (200, {"access_token": "gho_secret", "token_type": "bearer"}))
    told: list[str] = []
    auth = CopilotAuth()
    assert not auth.logged_in()

    asyncio.run(auth.login("device", told.append))

    assert told == ["Open https://example.test/device and enter code ABCD-1234"]
    start = server.forms(GITHUB_CODE)[0]
    assert start == {"client_id": copilot_module.CLIENT_ID, "scope": "read:user"}
    polls = server.forms(GITHUB_TOKEN)
    assert len(polls) == 3
    assert polls[0] == {"client_id": copilot_module.CLIENT_ID, "device_code": "dev-1",
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
    assert server.sleeps == [5, 5, 10]  # slow_down adds 5 s
    check = server.called(COPILOT_MODELS)[0]
    assert check.headers["authorization"] == "Bearer gho_secret"
    assert check.headers["copilot-integration-id"] == copilot_module.INTEGRATION_ID
    assert _stored(tmp_path) == {"copilot": {"github_token": "gho_secret"}}
    mode = stat.S_IMODE((tmp_path / "home" / "auth.json").stat().st_mode)
    assert mode == 0o600
    assert auth.logged_in() and "copilot" in connected()
    assert asyncio.run(auth.headers())["authorization"] == "Bearer gho_secret"


@pytest.mark.parametrize("models,message", [
    ((403, {}), "no active Copilot plan"),
    ((500, {}), "HTTP 500"),
])
def test_copilot_sign_in_is_not_saved_when_the_api_rejects_it(server, tmp_path, models, message):
    _copilot_script(server, (200, {"access_token": "gho_secret"}), models=models)
    auth = CopilotAuth()
    with pytest.raises(AuthError, match=message):
        asyncio.run(auth.login("device", lambda _: None))
    assert not auth.logged_in()
    assert not (tmp_path / "home" / "auth.json").exists()


def test_copilot_sign_in_is_not_saved_when_the_api_is_unreachable(server):
    server.script[GITHUB_CODE] = [_start()]
    server.script[GITHUB_TOKEN] = [(200, {"access_token": "gho_secret"})]  # COPILOT_MODELS unscripted: connect error
    auth = CopilotAuth()
    with pytest.raises(AuthError, match="could not reach"):
        asyncio.run(auth.login("device", lambda _: None))
    assert not auth.logged_in()


@pytest.mark.parametrize("error,message", [
    ("access_denied", "denied"),
    ("expired_token", "expired"),
    ("unsupported_grant_type", "sign-in failed"),
])
def test_device_flow_failures_say_why_and_store_nothing(server, tmp_path, error, message):
    _copilot_script(server, (200, {"error": "authorization_pending"}), (400, {"error": error}))
    with pytest.raises(AuthError, match=message):
        asyncio.run(CopilotAuth().login("device", lambda _: None))
    assert not server.called(COPILOT_MODELS)
    assert not (tmp_path / "home" / "auth.json").exists()


def test_device_flow_refusal_to_start_and_timeout(server, monkeypatch):
    server.script[GITHUB_CODE] = [(404, {})]
    with pytest.raises(AuthError, match="could not start device sign-in"):
        asyncio.run(CopilotAuth().login("device", lambda _: None))

    server.script[GITHUB_CODE] = [_start(expires_in=60)]
    server.script[GITHUB_TOKEN] = [(200, {"error": "authorization_pending"})]
    now = iter(range(0, 10_000, 40))  # every clock read moves 40 s, so the 60 s code runs out
    monkeypatch.setattr(device_module.time, "time", lambda: next(now))
    with pytest.raises(AuthError, match="timed out"):
        asyncio.run(CopilotAuth().login("device", lambda _: None))


def test_copilot_headers_need_a_login(server):
    with pytest.raises(AuthError, match="nexus login copilot"):
        asyncio.run(CopilotAuth().headers())


def test_copilot_logout_forgets_the_token(server, tmp_path):
    _copilot_script(server, (200, {"access_token": "gho_secret"}))
    auth = CopilotAuth()
    asyncio.run(auth.login("device", lambda _: None))
    auth.logout()
    assert not auth.logged_in() and _stored(tmp_path) == {}


# -- grok --------------------------------------------------------------------


def _grok_login(server, tmp_path, monkeypatch, refresh="rt-1"):
    monkeypatch.setenv(grok_module.VERSION_ENV, "0.1.152")
    server.script[GROK_CODE] = [_start(user_code="WXYZ-9999")]
    server.script[GROK_TOKEN] = [(400, {"error": "authorization_pending"}),
                                 (200, {"access_token": "at-1", "refresh_token": refresh, "expires_in": 3600})]
    auth = GrokAuth()
    told: list[str] = []
    asyncio.run(auth.login("device", told.append))
    return auth, told


def test_grok_login_stores_only_the_refresh_token(server, tmp_path, monkeypatch):
    auth, told = _grok_login(server, tmp_path, monkeypatch)

    assert told == ["Open https://example.test/device and enter code WXYZ-9999"]
    start = server.forms(GROK_CODE)[0]
    assert start == {"client_id": grok_module.CLIENT_ID, "scope": grok_module.SCOPE, "referrer": "nexus"}
    poll = server.forms(GROK_TOKEN)[0]
    assert poll == {"grant_type": grok_module.DEVICE_GRANT, "client_id": grok_module.CLIENT_ID, "device_code": "dev-1"}
    assert server.sleeps == [8.0, 8.0]  # interval 5 s plus OpenCode's 3 s
    assert _stored(tmp_path) == {"grok": {"refresh_token": "rt-1"}}
    assert "at-1" not in (tmp_path / "home" / "auth.json").read_text()
    assert auth.logged_in() and "grok" in connected()


def test_grok_headers_use_the_login_token_without_refreshing(server, tmp_path, monkeypatch):
    auth, _ = _grok_login(server, tmp_path, monkeypatch)
    before = len(server.requests)
    headers = asyncio.run(auth.headers())
    assert len(server.requests) == before
    assert headers == {
        "authorization": "Bearer at-1",
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-client-version": "0.1.152",
        "x-grok-client-identifier": "grok-shell",
        "user-agent": "xai-grok-cli",
    }


def test_grok_refresh_rotates_the_stored_token(server, tmp_path, monkeypatch):
    _grok_login(server, tmp_path, monkeypatch)
    fresh = GrokAuth()  # a new process: nothing in memory
    server.script[GROK_TOKEN] = [(200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600})]

    headers = asyncio.run(fresh.headers())

    assert headers["authorization"] == "Bearer at-2"
    refresh = server.forms(GROK_TOKEN)[-1]
    assert refresh == {"grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": grok_module.CLIENT_ID}
    assert _stored(tmp_path) == {"grok": {"refresh_token": "rt-2"}}
    asyncio.run(fresh.headers())  # cached: no second refresh
    assert len(server.forms(GROK_TOKEN)) == 3


def test_grok_refresh_is_renewed_before_the_access_token_expires(server, tmp_path, monkeypatch):
    auth, _ = _grok_login(server, tmp_path, monkeypatch)
    auth._expires_at = 0.0
    server.script[GROK_TOKEN] = [(200, {"access_token": "at-3", "expires_in": 3600})]  # no rotation
    assert asyncio.run(auth.headers())["authorization"] == "Bearer at-3"
    assert _stored(tmp_path) == {"grok": {"refresh_token": "rt-1"}}  # kept


def test_grok_refresh_failure_asks_for_a_new_login(server, tmp_path, monkeypatch):
    _grok_login(server, tmp_path, monkeypatch)
    server.script[GROK_TOKEN] = [(400, {"error": "invalid_grant"})]
    with pytest.raises(AuthError, match="nexus login grok"):
        asyncio.run(GrokAuth().headers())
    assert _stored(tmp_path) == {"grok": {"refresh_token": "rt-1"}}  # not wiped by one failure


def test_grok_login_without_a_refresh_token_stores_nothing(server, tmp_path, monkeypatch):
    monkeypatch.setenv(grok_module.VERSION_ENV, "0.1.152")
    server.script[GROK_CODE] = [_start()]
    server.script[GROK_TOKEN] = [(200, {"access_token": "at-1"})]
    with pytest.raises(AuthError, match="no refresh token"):
        asyncio.run(GrokAuth().login("device", lambda _: None))
    assert not (tmp_path / "home" / "auth.json").exists()


def test_grok_headers_name_the_missing_cli_version(server, tmp_path, monkeypatch):
    _grok_login(server, tmp_path, monkeypatch)
    monkeypatch.delenv(grok_module.VERSION_ENV)
    monkeypatch.setenv("HOME", str(tmp_path / "no-grok-cli"))
    with pytest.raises(AuthError, match="Grok CLI version"):
        asyncio.run(GrokAuth().headers())


def test_grok_version_comes_from_the_installed_cli(tmp_path, monkeypatch):
    monkeypatch.delenv(grok_module.VERSION_ENV, raising=False)
    cli = tmp_path / ".grok"
    cli.mkdir()
    (cli / "version.json").write_text('{"version": "0.2.0-beta.1"}')
    assert grok_module.grok_cli_version(cli) == "0.2.0-beta.1"
    (cli / "version.json").write_text('{"version": "not a version"}')
    assert grok_module.grok_cli_version(cli) is None
    monkeypatch.setenv(grok_module.VERSION_ENV, "9.9.9; rm -rf")
    assert grok_module.grok_cli_version(tmp_path / "missing") is None


def test_grok_logout_forgets_the_token(server, tmp_path, monkeypatch):
    auth, _ = _grok_login(server, tmp_path, monkeypatch)
    auth.logout()
    assert not auth.logged_in() and _stored(tmp_path) == {}


# -- opencode ----------------------------------------------------------------


def test_opencode_login_stores_the_pasted_key(server, tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "  oc-key-123  ")
    told: list[str] = []
    auth = SIGN_INS["opencode"]()
    asyncio.run(auth.login("api_key", told.append))
    assert "opencode.ai/auth" in told[0]
    assert _stored(tmp_path) == {"opencode": {"api_key": "oc-key-123"}}
    assert auth.key_source() == "stored" and "opencode" in connected()
    assert asyncio.run(auth.headers())["authorization"] == "Bearer oc-key-123"


def test_opencode_rejects_a_bad_paste_and_prefers_the_stored_key(server, tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "two words")
    with pytest.raises(AuthError, match="does not look like an API key"):
        asyncio.run(SIGN_INS["opencode"]().login("api_key", lambda _: None))
    monkeypatch.setenv("OPENCODE_API_KEY", "from-env")
    auth = SIGN_INS["opencode"]()
    assert auth.key_source() == "env"
    auth.set_key("stored")
    assert asyncio.run(auth.headers())["authorization"] == "Bearer stored"


# -- the command, and the first provider request -----------------------------


def test_login_command_for_each_provider_then_status(server, tmp_path, monkeypatch, capsys):
    _copilot_script(server, (200, {"access_token": "gho_secret"}))
    monkeypatch.setenv(grok_module.VERSION_ENV, "0.1.152")
    server.script[GROK_CODE] = [_start(user_code="WXYZ-9999")]
    server.script[GROK_TOKEN] = [(200, {"access_token": "at-1", "refresh_token": "rt-1"})]
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "oc-key-123")

    assert main(["login", "copilot"]) == 0
    assert main(["login", "grok", "--device"]) == 0
    assert main(["login", "opencode"]) == 0
    out = capsys.readouterr().out
    assert "enter code ABCD-1234" in out and "enter code WXYZ-9999" in out
    for pid in ("copilot", "grok", "opencode"):
        assert f"Logged in to {pid} ({PROVIDERS[pid].label})." in out
    assert set(_stored(tmp_path)) == {"copilot", "grok", "opencode"}

    assert main(["status"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line.split()}
    assert all("connected" in rows[p] and "not connected" not in rows[p] for p in ("copilot", "grok", "opencode"))

    assert main(["logout", "grok"]) == 0
    assert set(_stored(tmp_path)) == {"copilot", "opencode"}


def test_login_command_rejects_a_method_the_provider_lacks(server, capsys):
    assert main(["login", "copilot", "--method", "browser"]) == 1
    assert "sign-in methods are device" in capsys.readouterr().err


def test_login_command_reports_a_failed_sign_in(server, tmp_path, capsys):
    _copilot_script(server, (400, {"error": "access_denied"}))
    assert main(["login", "copilot"]) == 1
    assert "sign-in was denied" in capsys.readouterr().err
    assert not (tmp_path / "home" / "auth.json").exists()


REQUEST = Request(model="m", instructions="", messages=(Message("user", (Text("hi"),)),), conversation_id="conv-1")


def _first_request(provider, server):
    """Stream until the HTTP request has gone out (the server answers every unscripted URL with a connection error).

    Returns the redacted `WireRequest` the timeline shows and the request the server received.
    """
    async def drain():
        wire = None
        try:
            async for event in provider.stream(REQUEST):
                wire = wire or event
        except ProviderError:
            pass
        return wire

    wire = asyncio.run(drain())  # grok first refreshes its access token: the provider call is the last request
    assert isinstance(wire, WireRequest) and str(server.requests[-1].url) == wire.url
    return wire, server.requests[-1]


def test_logged_in_providers_send_their_credentials_on_the_first_request(server, tmp_path, monkeypatch):
    _copilot_script(server, (200, {"access_token": "gho_secret"}))
    monkeypatch.setenv(grok_module.VERSION_ENV, "0.1.152")
    server.script[GROK_CODE] = [_start()]
    server.script[GROK_TOKEN] = [(200, {"access_token": "at-1", "refresh_token": "rt-1"})]
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "oc-key-123")
    for pid in ("copilot", "grok", "opencode"):
        assert main(["login", pid]) == 0

    copilot = ChatProvider("copilot", PROVIDERS["copilot"].base_url, CopilotAuth(), PROVIDERS["copilot"].quirks)
    wire, sent = _first_request(copilot, server)
    assert str(sent.url) == "https://api.githubcopilot.com/chat/completions"
    assert sent.headers["authorization"] == "Bearer gho_secret"
    assert sent.headers["copilot-integration-id"] == copilot_module.INTEGRATION_ID
    assert wire.headers["authorization"] == "***"  # the visible wire is redacted

    grok = ResponsesProvider("grok", PROVIDERS["grok"].base_url, GrokAuth(), PROVIDERS["grok"].quirks)
    wire, sent = _first_request(grok, server)
    assert str(sent.url).startswith("https://cli-chat-proxy.grok.com/v1/")
    assert sent.headers["authorization"] == "Bearer at-1"
    assert sent.headers["x-grok-client-version"] == "0.1.152"
    assert sent.headers["x-grok-conv-id"] == "conv-1"
    assert wire.headers["authorization"] == "***"

    opencode = ChatProvider("opencode", PROVIDERS["opencode"].base_url, SIGN_INS["opencode"](), PROVIDERS["opencode"].quirks)
    wire, sent = _first_request(opencode, server)
    assert str(sent.url) == "https://opencode.ai/zen/go/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer oc-key-123"
    assert sent.headers["x-opencode-session"] == "conv-1"
    assert wire.headers["authorization"] == "***"


def test_the_credential_store_is_private_and_not_shared_between_providers(server, tmp_path):
    store = CredentialStore()
    store.write("copilot", {"github_token": "a"})
    store.write("grok", {"refresh_token": "b"})
    assert stat.S_IMODE((tmp_path / "home").stat().st_mode) == 0o700
    assert store.read("copilot") == {"github_token": "a"}
    store.write("copilot", None)
    assert store.read("grok") == {"refresh_token": "b"}
