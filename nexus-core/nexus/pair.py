"""`nexus pair`: print how a phone reaches this Core (address, token, link, terminal QR code).

No relay service is involved: the phone connects straight to an address on the same network or a Tailscale
network. The link is `nexus://pair?url=<core url>[&token=<token>][&name=<host>]`; the QR code encodes the link and
the app's Connect screen also accepts it pasted.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
from typing import Any
from urllib.parse import quote

from . import daemon


def _tailscale_ip() -> str | None:
    """This machine's Tailscale address (100.64.0.0/10), when the `tailscale` CLI is installed and up."""
    try:
        done = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in done.stdout.split():
        try:
            if ipaddress.ip_address(line) in ipaddress.ip_network("100.64.0.0/10"):
                return line
        except ValueError:
            continue
    return None


def _lan_ip() -> str | None:
    """The address this machine uses to reach the local network (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        ip = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if ip.startswith("127.") else ip


def candidates(port: int) -> list[dict[str, str]]:
    """Addresses a phone could use, best first: Tailscale (works from anywhere), then the LAN."""
    out: list[dict[str, str]] = []
    if (ts := _tailscale_ip()):
        out.append({"transport": "Tailscale", "url": f"http://{ts}:{port}"})
    if (lan := _lan_ip()):
        out.append({"transport": "LAN", "url": f"http://{lan}:{port}"})
    return out


def link(url: str, token: str = "", name: str = "") -> str:
    parts = [f"url={quote(url, safe='')}"]
    if token:
        parts.append(f"token={quote(token, safe='')}")
    if name:
        parts.append(f"name={quote(name, safe='')}")
    return "nexus://pair?" + "&".join(parts)


def qr_lines(text: str) -> list[str]:
    """The QR code as terminal text (half-block characters)."""
    import segno

    out = segno.make(text, error="m")
    import io
    buffer = io.StringIO()
    out.terminal(out=buffer, compact=True, border=2)
    return buffer.getvalue().rstrip("\n").split("\n")


def pairing() -> dict[str, Any]:
    """What to show: the running Core's port and token, the addresses and the link of the best one."""
    found = daemon.discover()
    if not found:
        raise ValueError("Nexus Core is not running: start it with `nexus daemon start`")
    port = int(str(found["url"]).rsplit(":", 1)[-1])
    token = str(found.get("token") or "")
    host = daemon._host()
    if daemon.is_loopback(host):
        raise ValueError(f"Core is bound to {host}: a phone cannot reach it. Restart it with NEXUS_HOST=0.0.0.0 (trusted networks only)")
    addresses = candidates(port)
    if not addresses:
        raise ValueError("no network address found for this machine: connect to Wi-Fi or Tailscale")
    name = socket.gethostname().split(".")[0]
    for a in addresses:
        a["link"] = link(a["url"], token, name)
    return {"name": name, "token": token, "addresses": addresses}


def render(info: dict[str, Any], *, qr: bool = True) -> str:
    best = info["addresses"][0]
    lines = [f"Pair a phone with {info['name']}", ""]
    for a in info["addresses"]:
        lines.append(f"  {a['transport']:<10} {a['url']}")
    lines.append(f"  Token      {info['token'] or 'none: this Core serves the network without a token'}")
    lines += ["", f"Link ({best['transport']}):", f"  {best['link']}", ""]
    if qr:
        lines += qr_lines(best["link"]) + [""]
    lines.append("In Nexus on the phone: Connect, scan this code or paste the link. Use only on a trusted network.")
    return "\n".join(lines)
