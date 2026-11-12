from urllib.parse import parse_qs, urlparse

import pytest

from nexus import pair


def test_link_roundtrip():
    out = pair.link("http://192.168.1.20:4747", "t k", "mac")
    q = parse_qs(urlparse(out).query)
    assert out.startswith("nexus://pair?")
    assert q == {"url": ["http://192.168.1.20:4747"], "token": ["t k"], "name": ["mac"]}


def test_link_omits_empty_token():
    assert "token" not in pair.link("http://10.0.0.2:4747")


def test_render_has_qr_and_addresses():
    info = {"name": "mac", "token": "", "addresses": [{"transport": "LAN", "url": "http://10.0.0.2:4747", "link": pair.link("http://10.0.0.2:4747", "", "mac")}]}
    text = pair.render(info)
    assert "LAN" in text and "http://10.0.0.2:4747" in text and "without a token" in text
    assert any(c in text for c in "█▀▄")
    assert "█" not in pair.render(info, qr=False) and "▀" not in pair.render(info, qr=False)


def test_pairing_needs_a_daemon(monkeypatch):
    monkeypatch.setattr(pair.daemon, "discover", lambda: None)
    with pytest.raises(ValueError, match="not running"):
        pair.pairing()


def test_pairing_refuses_loopback_bind(monkeypatch):
    monkeypatch.setattr(pair.daemon, "discover", lambda: {"url": "http://127.0.0.1:4747", "token": "x"})
    monkeypatch.setenv("NEXUS_HOST", "127.0.0.1")
    with pytest.raises(ValueError, match="cannot reach"):
        pair.pairing()


def test_pairing_lists_addresses(monkeypatch):
    monkeypatch.setattr(pair.daemon, "discover", lambda: {"url": "http://127.0.0.1:4747", "token": ""})
    monkeypatch.delenv("NEXUS_HOST", raising=False)
    monkeypatch.setattr(pair, "candidates", lambda port: [{"transport": "LAN", "url": f"http://10.0.0.2:{port}"}])
    info = pair.pairing()
    assert info["addresses"][0]["link"].startswith("nexus://pair?url=http%3A%2F%2F10.0.0.2%3A4747")
