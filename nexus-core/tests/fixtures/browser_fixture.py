"""A stand-in for browser-use 1.0.2's stdio server: browser_exec and browser_screenshot, no browser.

`FLAG` in the browser_exec namespace is the flag Nexus started it with (`--mcp` or `--cli-mcp`).
"""
import base64
import sys

from mcp.server import MCPServer
from mcp_types import ImageContent, TextContent

server = MCPServer("browser-fixture")
namespace: dict = {"FLAG": sys.argv[-1], "new_tab": lambda url: None,
                   "page_info": lambda: {"url": "https://example.com/", "title": "Example Domain"}}


@server.tool()
def browser_exec(code: str) -> str:
    """Run Python; the namespace persists."""
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(code, namespace)  # noqa: S102 - test fixture
    return out.getvalue()


@server.tool()
def browser_screenshot(full: bool = False) -> list[TextContent | ImageContent]:
    """Return a tiny PNG."""
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 40).decode()
    return [ImageContent(type="image", data=png, mime_type="image/png")]


@server.tool()
def browser_login(force: bool = False) -> str:
    """Upstream still offers this; Nexus must not."""
    return "signed in"


if __name__ == "__main__":
    server.run("stdio")
