import os
import sys

import httpx
import pytest

from nexus import doctor
from nexus.config import ConfigFile


def test_fingerprint_is_stable_and_follows_source(tmp_path, monkeypatch):
    assert doctor.source_fingerprint() == doctor.source_fingerprint()


def test_dev_version_sorts_before_its_release():
    key = doctor._version_key
    assert key("0.10.0.dev0") < key("0.10.0") < key("v0.10.1") < key("0.11.0")


def test_update_check_reports_each_outcome(monkeypatch):
    def answer(status, body=None):
        monkeypatch.setattr(doctor.httpx, "get", lambda *a, **k: httpx.Response(status, json=body or {}))
        return doctor.update_check("0.10.0.dev0")

    assert answer(404)["state"] == "none"
    assert answer(500)["state"] == "unknown"
    assert answer(200, {"tag_name": "v0.10.0", "html_url": "u"})["state"] == "available"
    assert answer(200, {"tag_name": "v0.9.0"})["state"] == "current"

    def refuse(*a, **k):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(doctor.httpx, "get", refuse)
    assert doctor.update_check("0.10.0")["state"] == "unknown"


def test_daemon_row_flags_other_code(monkeypatch):
    found = {"url": "http://x", "token": "t"}
    monkeypatch.setattr(doctor, "_get", lambda f, path: {"pid": 1, "version": "v", "code": "stale"})
    assert doctor.check_daemon(found)["status"] == "fail"
    monkeypatch.setattr(doctor, "_get", lambda f, path: {"pid": 1, "version": "v", "code": doctor.source_fingerprint()})
    assert doctor.check_daemon(found)["status"] == "ok"
    assert doctor.check_daemon(None)["status"] == "warn"


def test_config_errors_fail_the_row(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[ui]\nbogus = 1\n")
    row = doctor.check_config()
    assert row["status"] == "fail" and "bogus" in row["detail"]


# --- external worker rows ---------------------------------------------------------------------------------------

def _zmx_config(tmp_path, monkeypatch, **values):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    config = ConfigFile()
    config.patch(config.read()["revision"], values, [])


def _fake_agy(tmp_path, version: str) -> str:
    """A CLI that answers `--version` and has no headless flags (the ACP preflight must not ask for them)."""
    path = tmp_path / "agy"
    path.write_text(f"#!{sys.executable}\nimport sys\n"
                    f"if '--version' in sys.argv:\n    print({version!r})\n"
                    "else:\n    sys.exit(3)\n")
    path.chmod(0o700)
    return str(path)


@pytest.mark.skipif(os.name != "posix", reason="external workers need POSIX PTYs and process groups")
def test_zmx_doctor_checks_the_acp_preflight_not_legacy_stream_json(tmp_path, monkeypatch):
    _zmx_config(tmp_path, monkeypatch, **{"zmx.antigravity": True})
    monkeypatch.setenv("NEXUS10_AGY_CLI", _fake_agy(tmp_path, "1.5.14"))
    row = {r["name"]: r for r in doctor.check_zmx()}["workers agy"]
    assert row["status"] == "ok"
    assert "version 1.5.14" in row["detail"] and "ACP v1 stdio" in row["detail"]
    assert "`agy acp`" in row["detail"] and "sign-in not verified" in row["detail"]
    assert "stream-json" not in row["detail"] and "required flags" not in row["detail"]


@pytest.mark.skipif(os.name != "posix", reason="external workers need POSIX PTYs and process groups")
def test_zmx_doctor_rejects_an_agy_that_predates_acp(tmp_path, monkeypatch):
    _zmx_config(tmp_path, monkeypatch, **{"zmx.antigravity": True})
    monkeypatch.setenv("NEXUS10_AGY_CLI", _fake_agy(tmp_path, "1.4.12"))
    row = {r["name"]: r for r in doctor.check_zmx()}["workers agy"]
    assert row["status"] == "fail"
    assert "1.4.12" in row["detail"] and "predates ACP" in row["detail"] and "1.5.0" in row["detail"]
    assert "agy update" in row["detail"]


@pytest.mark.skipif(os.name != "posix", reason="external workers need POSIX PTYs and process groups")
def test_zmx_doctor_reports_an_unverifiable_agy_version(tmp_path, monkeypatch):
    _zmx_config(tmp_path, monkeypatch, **{"zmx.antigravity": True})
    monkeypatch.setenv("NEXUS10_AGY_CLI", _fake_agy(tmp_path, "not a version"))
    row = {r["name"]: r for r in doctor.check_zmx()}["workers agy"]
    assert row["status"] == "fail" and "version could not be verified for ACP" in row["detail"]


@pytest.mark.skipif(os.name != "posix", reason="external workers need POSIX PTYs and process groups")
def test_zmx_doctor_never_probes_antigravity_while_it_is_off(tmp_path, monkeypatch):
    _zmx_config(tmp_path, monkeypatch)
    marker = tmp_path / "probed"
    path = tmp_path / "agy"
    path.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    path.chmod(0o700)
    monkeypatch.setenv("NEXUS10_AGY_CLI", str(path))
    rows = {r["name"]: r for r in doctor.check_zmx()}
    assert rows["workers agy"]["status"] == "ok" and "off (no CLI probed)" in rows["workers agy"]["detail"]
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="external workers need POSIX PTYs and process groups")
def test_zmx_doctor_keeps_the_claude_flag_preflight(tmp_path, monkeypatch):
    from nexus.workers import registry

    _zmx_config(tmp_path, monkeypatch, **{"zmx.claude_backend": "zmx"})
    claude = tmp_path / "claude"
    claude.write_text(f"#!{sys.executable}\nimport sys\n"
                      "if '--version' in sys.argv:\n    print('2.1.318')\n"
                      "else:\n    print('--model --session-id --resume --permission-mode --settings --disallowedTools')\n")
    claude.chmod(0o700)
    monkeypatch.setattr(registry, "cli_path", lambda pid: str(claude) if pid == "claude-code" else None)
    row = {r["name"]: r for r in doctor.check_zmx()}["workers claude-code"]
    assert row["status"] == "ok"
    assert "version 2.1.318" in row["detail"] and "required flags present" in row["detail"]
    assert "ACP" not in row["detail"]
