"""No bearer token or account id reaches events, host results, logs or error text (docs/security.md)."""
from __future__ import annotations

import json
import logging

import httpx
import msgspec
import pytest

from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.live import protocol as lp
from nexus.live.manager import LiveManager
from nexus.live.transport import CodexLiveTransport
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from tests.test_live_bridge import _config
from tests.live_fakes import MANAGER_ID, live_config, settle

TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlc2lnbmF0dXJlc2lnbmF0dXJl"
ACCOUNT = "acct-secret-0123456789"
NEEDLES = (TOKEN, "Bearer", ACCOUNT, "eyJhbGci")


class Auth:
    async def headers(self):
        return {"authorization": f"Bearer {TOKEN}", "ChatGPT-Account-Id": ACCOUNT, "originator": "nexus"}


class LeakySocket:
    """Echoes credentials in every kind of failure text a server might produce."""

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)

    async def recv(self):
        await __import__("asyncio").sleep(0.01)
        raise ConnectionError(f"reset: Authorization: Bearer {TOKEN} account {ACCOUNT}")

    async def close(self, code=1000, reason=""):
        return None


def assert_clean(blob: str) -> None:
    for needle in NEEDLES:
        assert needle not in blob, f"{needle!r} leaked"


@pytest.mark.parametrize("mode", ["http_error", "transport_error", "sideband_drop"])
async def test_no_credential_reaches_events_results_or_logs(tmp_path, caplog, mode):
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "transport_error":
            raise httpx.ConnectError(f"cannot connect Authorization: Bearer {TOKEN} for {ACCOUNT}")
        if mode == "http_error":
            return httpx.Response(500, text=f"upstream said: Bearer {TOKEN} account={ACCOUNT}")
        return httpx.Response(200, text="v=0 answer", headers={"Location": "/calls/rtc_ok1"})

    async def connector(url, headers):
        return LeakySocket()

    fake_now = [0.0]

    async def sleep(delay):  # recovery is bounded by elapsed time: advance a fake clock instead of waiting
        fake_now[0] += delay

    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": ScriptedProvider(text_response("x"))})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime.live = LiveManager(
        live_config(log_raw=True),
        transport_factory=lambda raw_log=None: CodexLiveTransport(
            Auth(), client=client, connector=connector, sleep=sleep, clock=lambda: fake_now[0], raw_log=raw_log),
        home=tmp_path / "home", environ={}, flush_interval=0.0,
    )
    from nexus.host_support.live_consent import write_consent

    write_consent(True)  # the daemon refuses LiveStart until the notice is recorded
    write_consent(True, scope="manager")
    facade = HostFacade(runtime)
    facade.open_manager()
    results = []
    try:
        started = await facade.handle(p.LiveStart(session=MANAGER_ID, offer_sdp="v=0\r\noffer"))
        results.append(started)
        if mode == "sideband_drop":
            assert isinstance(started, p.LiveStartResult)
            await settle(lambda: any(e.type == "live.ended" for e in runtime.session(MANAGER_ID).events), timeout=5.0)
        else:
            assert isinstance(started, p.ErrorResult)
        results.append(await facade.handle(p.LiveStatus()))
        results.append(await facade.handle(p.LiveStop(session=MANAGER_ID)))

        events = list(runtime.session(MANAGER_ID).events)
        assert [e.type for e in events].count("live.started") == 1
        assert [e.type for e in events].count("live.ended") == 1
        assert_clean(json.dumps([e.data for e in events if e.type.startswith("live.")], default=str))
        assert_clean(json.dumps([msgspec.to_builtins(r) for r in results], default=str))
        assert_clean(caplog.text)
        view, _ = facade.state(MANAGER_ID)
        assert_clean(json.dumps(view.to_dict(), default=str))
        # the raw dev log (log_raw) is redacted too, and holds no audio or SDP offer secrets
        for path in (tmp_path / "home").rglob("*.jsonl"):
            assert_clean(path.read_text())
    finally:
        await client.aclose()
        await runtime.aclose()


async def test_status_and_start_results_have_no_credential_fields(tmp_path):
    fields = set(p.LiveStatusResult.__struct_fields__) | set(p.LiveStartResult.__struct_fields__)
    assert not {f for f in fields if any(w in f for w in ("token", "secret", "bearer", "auth", "account", "header"))}


def test_the_sdp_offer_is_never_printed():
    request = lp.CallRequest("v=0\r\na=ice-pwd:supersecretpassword\r\n", lp.LiveSession("hi"))
    assert "ice-pwd" not in repr(request)
    assert "ice-pwd" not in repr(p.LiveStart(session="s", offer_sdp="a=ice-pwd:supersecretpassword"))
