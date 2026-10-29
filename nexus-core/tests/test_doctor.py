import httpx

from nexus import doctor


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
