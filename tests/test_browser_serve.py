"""Native screenshot serving stays loopback-only and independent of UI toolkits."""
import pytest

from browser_serve import serve


def test_terminal_server_rejects_public_bind():
    with pytest.raises(ValueError, match="loopback"):
        serve("unused", host="0.0.0.0")
