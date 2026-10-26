"""Static serving of the NSH client bundle at `<prefix>/app/` (docs/nsh-client.md).

This is the only Python module that knows where the NSH bundle lives; the rest
of Nexus (core, host API, `web.py`) is client-neutral and only calls
`serve_app`. The bundle is build output of `scripts/nsh-app.sh package web`
(git-ignored). Ticket redemption stays in `web.py` (generic auth) and is passed
in as callbacks.
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs

import msgspec

NSH_ROOT = Path(__file__).resolve().parents[1] / "ui" / "nsh"
_NSH_MIME = {".wasm": "application/wasm", ".js": "text/javascript", ".webmanifest": "application/manifest+json"}
_INLINE_SCRIPT = re.compile(r"<script(\s[^>]*)?>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_SRC_ATTR = re.compile(r"(?:^|\s)src\s*=", re.IGNORECASE)


def _json(value) -> bytes:
    return msgspec.json.encode(value)


def app_csp(index: bytes) -> str:
    """CSP for the app route only; inline scripts are allowed by hash."""
    hashes = []
    for match in _INLINE_SCRIPT.finditer(index.decode("utf-8", "replace")):
        if _SRC_ATTR.search(match.group(1) or ""):
            continue
        digest = base64.b64encode(hashlib.sha256(match.group(2).encode("utf-8")).digest()).decode("ascii")
        hashes.append(f"'sha256-{digest}'")
    scripts = " ".join(["'self'", "'wasm-unsafe-eval'", *hashes])
    return (
        f"default-src 'self'; script-src {scripts}; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
        "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )


async def serve_app(path, request, writer, server, common, *, prefix, redeem, session_cookie) -> None:
    """Serve `/app` and `/app/*`. `redeem(ticket) -> key|None`, `session_cookie(key) -> str`."""
    if path == "/app":
        await server._write_response(writer, 301, b"", headers={**common, "Location": f"{prefix}/app/"})
        return
    tickets = parse_qs(request.query).get("ticket", []) if path == "/app/" else []
    if tickets:
        # `nexus web` launch: NSH 0.2.0 apps cannot read the URL fragment, so
        # the one-use ticket arrives in the query. Redeem it here and redirect
        # at once so the URL no longer carries it.
        headers = {**common, "Location": f"{prefix}/app/"}
        key = await redeem(tickets[0])
        if key is not None:
            headers["Set-Cookie"] = session_cookie(key)
        await server._write_response(writer, 303, b"", headers=headers)
        return
    relative = path[len("/app/"):] or "index.html"
    root = NSH_ROOT.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
        data = target.read_bytes()
    except (ValueError, OSError):
        message = "not found" if (root / "index.html").is_file() else "nsh app not built"
        await server._write_response(writer, 404, _json({"error": message}), headers=common)
        return
    headers = dict(common)
    if target.name == "index.html":
        headers["Content-Security-Policy"] = app_csp(data)
    mutable = target.name in {"index.html", "manifest.webmanifest"}
    headers["Content-Type"] = (
        _NSH_MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    )
    headers["Cache-Control"] = "no-cache" if mutable else "public, max-age=31536000, immutable"
    await server._write_response(writer, 200, b"" if request.method == "HEAD" else data, headers=headers)
