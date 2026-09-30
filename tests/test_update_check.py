"""The "update available" notice (plans/release.md Phase 7): no network, fake fetch."""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.host_support import update_check as uc

NOW = 1_800_000_000.0


def _pypi(releases: dict) -> dict:
    return {"releases": {v: [{"yanked": y}] if y is not None else [] for v, y in releases.items()}}


@pytest.fixture(autouse=True)
def _installed(monkeypatch):
    monkeypatch.setattr(uc, "install_method", lambda: "uv-tool")


def _status(tmp_path, fetch, *, now=NOW, current="0.1.1", environ=None, **kw):
    return uc.update_status(
        home=tmp_path, environ={} if environ is None else environ, now=now,
        fetch=fetch, current=current, **kw,
    )


def _boom():
    raise AssertionError("must not fetch")


def test_newest_release_ignores_yanked_prerelease_and_empty():
    payload = _pypi({"0.1.0": False, "0.1.1": False, "0.2.0": True, "0.3.0rc1": False, "0.4.0": None})
    assert uc.newest_release(payload) == "0.1.1"
    assert uc.newest_release({"releases": []}) is None
    assert uc.newest_release("nope") is None


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("0.1.2", "0.1.1", True),
        ("0.1.1", "0.1.1", False),
        ("0.1.0", "0.1.1", False),
        ("0.10.0", "0.9.9", True),
        ("1.0", "0.9.9", True),
        ("0.1.1", "0.1.1.dev3", False),
        ("0.2.0rc1", "0.1.1", False),
        ("garbage", "0.1.1", False),
    ],
)
def test_is_newer(candidate, current, expected):
    assert uc.is_newer(candidate, current) is expected


def test_fetch_result_is_cached_for_a_day(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return "0.1.2"

    first = _status(tmp_path, fetch)
    assert first["available"] == "0.1.2" and first["enabled"] is True
    assert uc.notice(first) == "0.1.2 available: nexus update"
    _status(tmp_path, fetch, now=NOW + uc.CACHE_TTL_SECONDS - 1)
    assert len(calls) == 1
    _status(tmp_path, fetch, now=NOW + uc.CACHE_TTL_SECONDS + 1)
    assert len(calls) == 2


def test_failed_lookup_is_cached_and_keeps_last_good_answer(tmp_path):
    assert _status(tmp_path, lambda: None)["available"] is None
    _status(tmp_path, _boom, now=NOW + 60)  # the failure itself is remembered
    day = uc.CACHE_TTL_SECONDS
    assert _status(tmp_path, lambda: "0.1.2", now=NOW + 2 * day)["available"] == "0.1.2"
    assert _status(tmp_path, lambda: None, now=NOW + 4 * day)["available"] == "0.1.2"


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"checked_at": "x"}', '{"checked_at": 1, "latest": "evil"}', "x" * 9000],
)
def test_corrupt_cache_is_treated_as_stale(tmp_path, content):
    path = uc.cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert _status(tmp_path, lambda: "0.1.2")["available"] == "0.1.2"


def test_future_dated_cache_is_stale(tmp_path):
    path = uc.cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"checked_at": NOW + 10_000, "latest": "0.0.1"}))
    assert _status(tmp_path, lambda: "0.1.2")["available"] == "0.1.2"


@pytest.mark.parametrize("environ", [{"NEXUS_NO_UPDATE_CHECK": "1"}, {"CI": "true"}])
def test_env_opt_outs_never_fetch(tmp_path, environ):
    status = _status(tmp_path, _boom, environ=environ)
    assert status["enabled"] is False and status["available"] is None
    assert not uc.cache_path(tmp_path).exists()


def test_config_and_editable_opt_out(tmp_path, monkeypatch):
    assert _status(tmp_path, _boom, config_enabled=False)["enabled"] is False
    monkeypatch.setattr(uc, "install_method", lambda: "editable")
    assert _status(tmp_path, _boom)["enabled"] is False


def test_cached_only_never_fetches_and_trusts_old_answers(tmp_path):
    assert _status(tmp_path, _boom, cached_only=True)["available"] is None
    _status(tmp_path, lambda: "0.1.2")
    old = _status(tmp_path, _boom, now=NOW + 30 * uc.CACHE_TTL_SECONDS, cached_only=True)
    assert old["available"] == "0.1.2"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_latest_release_reads_pypi_json():
    body = _pypi({"0.1.0": False, "0.1.1": False})
    assert uc.latest_release(_client(lambda request: httpx.Response(200, json=body))) == "0.1.1"


def test_latest_release_failures_return_none():
    assert uc.latest_release(_client(lambda r: httpx.Response(404))) is None
    assert uc.latest_release(_client(lambda r: httpx.Response(200, content=b"{nope"))) is None
    big = b'{"releases": {}, "pad": "' + b"x" * uc.MAX_RESPONSE_BYTES + b'"}'
    assert uc.latest_release(_client(lambda r: httpx.Response(200, content=big))) is None

    def refuse(request):
        raise httpx.ConnectError("offline")

    assert uc.latest_release(_client(refuse)) is None


def test_version_flag_shows_cached_notice_without_network(tmp_path, monkeypatch, capsys):
    from nexus import cli

    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / ".."))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("NEXUS_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr("nexus.host_support.install.package_version", lambda: "0.1.1")
    monkeypatch.setattr(uc, "package_version", lambda: "0.1.1")
    monkeypatch.setattr(uc, "nexus_home", lambda home=None: tmp_path)
    _status(tmp_path, lambda: "0.1.2")
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == "nexus 0.1.1 (0.1.2 available: nexus update)\n"
    monkeypatch.setenv("NEXUS_NO_UPDATE_CHECK", "1")
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == "nexus 0.1.1\n"


def test_doctor_prints_the_notice():
    import io

    from nexus import cli

    out = io.StringIO()
    cli._print_doctor(
        {
            "install": {"version": "0.1.1", "method": "uv-tool", "python": "3.13"},
            "update": {"available": "0.1.2", "command": "nexus update"},
        },
        out,
    )
    assert "update: 0.1.2 available: nexus update" in out.getvalue()
