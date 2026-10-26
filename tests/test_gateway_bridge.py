"""Focused checks for gateway-to-host URL and credential boundaries."""

import pytest

from nexus.gateway.upstream import UpstreamError, parse_launch_url


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_launch_url_requires_loopback_and_extracts_ticket(address):
    host = f"[{address}]" if ":" in address else address
    base, origin, ticket = parse_launch_url(f"http://{host}:12345/#ticket=abcdefghijklmnop")
    assert base == origin == f"http://{host}:12345"
    assert ticket == "abcdefghijklmnop"


def test_launch_url_accepts_daemon_project_prefix():
    base, origin, ticket = parse_launch_url("http://127.0.0.1:12345/w/b517bec9b7ab99ee/#ticket=abcdefghijklmnop")
    assert base == "http://127.0.0.1:12345/w/b517bec9b7ab99ee"
    assert origin == "http://127.0.0.1:12345"
    assert ticket == "abcdefghijklmnop"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:12345/w/../etc/#ticket=abcdefghijklmnop",
        "http://127.0.0.1:12345/other/#ticket=abcdefghijklmnop",
        "http://192.0.2.1:12345/#ticket=abcdefghijklmnop",
        "https://127.0.0.1:12345/#ticket=abcdefghijklmnop",
        "http://localhost:12345/#ticket=abcdefghijklmnop",
        "http://127.0.0.1:12345/#ticket=short",
        "http://127.0.0.1:12345/?redirect=1#ticket=abcdefghijklmnop",
        "http://user@127.0.0.1:12345/#ticket=abcdefghijklmnop",
    ],
)
def test_launch_url_rejects_nonlocal_or_malformed_address(url):
    with pytest.raises(UpstreamError) as error:
        parse_launch_url(url)
    assert error.value.code == "bad_launch"
