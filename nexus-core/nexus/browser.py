"""Browser tools: `browser_exec` and `browser_screenshot`, run by upstream browser-use.

They are ordinary grantable tools, like `bash` or `web_fetch`: an agent without a
`tools` list has them, and an agent with one has them only if it names them. They
are not MCP tools (browser-use is not an MCP server in Settings), and the other
tools browser-use 1.0.2 offers (`browser_login`, `browser_feedback`) are not
offered to the model.

The process is upstream browser-use, pinned (`PACKAGE`), started with `uvx` and
spoken to over stdio. Nexus owns its lifetime: one process per session, started
by that session's first browser call (never at session start) and closed when
the daemon stops. A process that died, or one started in the other browser mode,
is replaced by the next call, whose result then says the browser state is gone.
stderr goes to `~/.nexus/logs/browser/<session>.log`, never into context. What
the tools return is untrusted page data and is fenced like MCP output.

Which browser: by default browser-use's own Chromium on a temporary profile
(`browser-use --mcp`); it never attaches the user's Chrome. Only when the user
turns on `[browser] use_my_chrome` (Settings > Browser) does Nexus start
`browser-use --cli-mcp`, which drives the user's own Chrome, with its tabs and
sign-ins, when Chrome's remote debugging is on. The setting is read at every
call, so turning it off takes effect on the next call.

The definitions are Nexus's own, frozen here, so they never change within a
session. The tools follow the session's permission setting and the permission
rules.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .llmhub.types import ToolSpec
from .mcp.config import ServerConfig
from .mcp.manager import Connection, McpUnavailable
from .paths import home
from .tools.base import Tool, ToolContext, ToolError

PACKAGE = "browser-use[cli]==1.0.2"
COMMAND = ("uvx", "--from", PACKAGE, "browser-use")  # + OWN_FLAG or CHROME_FLAG
OWN_FLAG = "--mcp"  # its own Chromium on a temporary profile
CHROME_FLAG = "--cli-mcp"  # the user's Chrome when remote debugging is on
SERVER = "browser-use"  # the name in fences and artifact sources
FENCE_SOURCE = "the browser tools (browser-use, driving a web page)"
CONNECT_TIMEOUT = 180.0  # the first start downloads the package
CALL_TIMEOUT = 120.0

EXEC, SCREENSHOT = "browser_exec", "browser_screenshot"
BROWSER_TOOLS = (EXEC, SCREENSHOT)

_DEFINITIONS: dict[str, dict[str, Any]] = {
    EXEC: {
        "name": EXEC,
        "description": (
            "Run Python that drives a web browser. Helpers like new_tab(url), goto_url(url), page_info(), "
            "click_at_xy(x, y), type_text(text), js(code), cdp(method, ...), wait_for_load(), list_tabs() are "
            "pre-imported. The namespace persists across calls in this session. Returns whatever the code prints "
            "(prints from threads it starts are not returned: collect their results and print them in the code "
            "itself). First navigation should be new_tab(url). The browser is a Chromium with its own temporary "
            "profile unless the user allowed their own Chrome in Settings; a result says which. Use it for pages "
            "that need JavaScript, forms, clicks or a visual check; use web_fetch or web_search for plain reading."
        ),
        "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "Python code to execute"}},
                       "required": ["code"]},
    },
    SCREENSHOT: {
        "name": SCREENSHOT,
        "description": "Capture the current browser page and return it as an image. Prefer this over capture_screenshot() in browser_exec.",
        "parameters": {"type": "object", "properties": {
            "full": {"type": "boolean", "description": "Capture beyond the viewport (full page)", "default": False},
            "max_dim": {"type": "integer", "minimum": 1,
                        "description": "Downscale so no side exceeds this many pixels (e.g. 1800 for 2x displays)"}}},
    },
}


async def _unbound(args: dict[str, Any], ctx: ToolContext) -> str:
    raise ToolError("the browser tools run only in a Nexus session")


# Registry entries: the runtime binds each session's runner (`Runtime._browser_call`).
TOOLS = tuple(Tool(ToolSpec(d["name"], d["description"], d["parameters"]), _unbound) for d in _DEFINITIONS.values())


def definition(name: str) -> dict[str, Any]:
    """A snapshot-shaped tool definition (name, description, parameters)."""
    return json.loads(json.dumps(_DEFINITIONS[name]))


RESTART_NOTICE = ("[Nexus notice: browser restarted] browser-use was not running (it stopped, Nexus restarted, or "
                  "the browser setting changed), so this call started it again. Tabs and variables from earlier "
                  "browser_exec calls are gone; open the page again with new_tab(url).")


INSTALL_CHROMIUM = f"uvx --from '{PACKAGE}' browser-use install"


def find_uvx() -> str | None:
    """`uvx` on PATH, else the one beside `uv` (Nexus installs uv; a GUI-started daemon may lack it on PATH)."""
    found = shutil.which("uvx")
    if found:
        return found
    uv = shutil.which("uv")
    sibling = Path(uv).with_name("uvx.exe" if os.name == "nt" else "uvx") if uv else None
    for path in (sibling, Path.home() / ".local/bin/uvx", Path.home() / ".cargo/bin/uvx"):
        if path is not None and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _snap(path: str) -> bool:
    """Ubuntu's Snap Chromium: its confinement hides browser-use's temporary profile, so browser-use skips it."""
    return sys.platform.startswith("linux") and "/snap/" in os.path.realpath(path)


def _playwright() -> list[tuple[str, str]]:
    """Chromium from `browser-use install` (Playwright's cache), newest first, as browser-use 1.0.2 finds it."""
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if root in ("", "0"):
        root = str(Path.home() / "Library/Caches/ms-playwright" if sys.platform == "darwin"
                   else Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "ms-playwright"
                   if sys.platform == "win32" else Path.home() / ".cache/ms-playwright")
    builds = sorted(Path(root).expanduser().glob("chromium-*"),
                    key=lambda p: int(p.name.split("-")[-1]) if p.name.split("-")[-1].isdigit() else -1, reverse=True)
    rels = (["chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
             "chrome-mac*/Chromium.app/Contents/MacOS/Chromium"] if sys.platform == "darwin"
            else ["chrome-win*/chrome.exe"] if sys.platform == "win32" else ["chrome-linux*/chrome"])
    return [("Chromium (browser-use install)", str(path)) for build in builds for rel in rels for path in build.glob(rel)]


def find_browser(*, chrome_only: bool = False) -> dict[str, str] | None:
    """The browser browser-use 1.0.2 would launch: an installed Chrome, Chromium or Edge, then Playwright's
    Chromium. `{name, path}`, or None. `chrome_only`: Google Chrome alone (what "Use my Chrome profile" drives)."""
    names = ["Google Chrome"] if chrome_only else ["Google Chrome", "Chromium", "Microsoft Edge", "Google Chrome Canary"]
    found: list[tuple[str, str]] = []
    if sys.platform == "darwin":
        found = [(name, str(root / f"{name}.app/Contents/MacOS/{name}"))
                 for name in names for root in (Path("/Applications"), Path.home() / "Applications")]
    elif sys.platform == "win32":
        rels = {"Google Chrome": r"Google\Chrome\Application\chrome.exe", "Chromium": r"Chromium\Application\chrome.exe",
                "Microsoft Edge": r"Microsoft\Edge\Application\msedge.exe"}
        roots = [r for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if (r := os.environ.get(k))]
        found = [(name, str(Path(root) / rels[name])) for name in names if name in rels for root in roots]
    else:
        commands = {"Google Chrome": ["google-chrome-stable", "google-chrome"], "Chromium": ["chromium", "chromium-browser"],
                    "Microsoft Edge": ["microsoft-edge-stable", "microsoft-edge"]}
        found = [(name, path) for name in names for command in commands.get(name, []) if (path := shutil.which(command))]
    if not chrome_only:
        found += _playwright()
    for name, path in found:
        if Path(path).is_file() and not _snap(path):
            return {"name": name, "path": path}
    return None


def requirements(use_my_chrome: bool) -> dict[str, Any]:
    """What the browser tools need on this machine: `uvx` and a browser; with `use_my_chrome`, Google Chrome.

    `ok` is false when a browser call would fail to start. `problems` say how to fix each gap. Chrome's remote
    debugging switch cannot be read from outside Chrome, so `note` only reminds of it.
    """
    uvx, browser = find_uvx(), find_browser()
    chrome = find_browser(chrome_only=True) if use_my_chrome else None
    problems = []
    if uvx is None:
        problems.append("uvx was not found (it comes with uv, which the Nexus install sets up): install uv from "
                        "https://docs.astral.sh/uv/ and restart the daemon")
    if browser is None:
        problems.append(f"no Chrome, Chromium or Edge was found: install Google Chrome, or run `{INSTALL_CHROMIUM}` "
                        "for a Chromium only the browser tools use")
    if use_my_chrome and chrome is None:
        problems.append("Use my Chrome profile is on, but Google Chrome is not installed; the browser tools fall back "
                        "to their own Chromium")
    return {
        "ok": uvx is not None and browser is not None,
        "mode": "chrome" if use_my_chrome else "own",
        "uvx": uvx, "browser": browser, "chrome": chrome, "package": PACKAGE, "problems": problems,
        "note": ("Your Chrome is used only while its remote debugging is on (chrome://inspect/#remote-debugging); "
                 "Nexus cannot check that switch." if use_my_chrome else
                 "The browser tools use their own profile; your Chrome profile is never touched."),
    }


def log_path(session_id: str) -> Any:
    return home() / "logs" / "browser" / f"{session_id}.log"


class BrowserService:
    """One browser-use process per session, started by its first call."""

    def __init__(self, publish: Callable[[str, dict[str, Any], str], Any], use_my_chrome: Callable[[], bool],
                 command: tuple[str, ...] = COMMAND):
        self.publish = publish
        self.use_my_chrome = use_my_chrome
        self.command = command
        self.connections: dict[str, Connection] = {}
        self.flags: dict[str, str] = {}  # session -> the flag its process was started with
        self.check: bool | None = None  # None: check uvx and a browser only for the real command (not a test stand-in)
        self.started: set[str] = set()  # sessions whose process has run once in this daemon

    def _checking(self) -> bool:
        return self.command == COMMAND if self.check is None else self.check

    def _connection(self, session_id: str, flag: str) -> Connection:
        conn = self.connections.get(session_id)
        if conn is None:
            command = (find_uvx() or self.command[0]) if self._checking() else self.command[0]
            config = ServerConfig(name=SERVER, transport="stdio", command=command,
                                  args=(*self.command[1:], flag),
                                  connect_timeout=CONNECT_TIMEOUT, call_timeout=CALL_TIMEOUT)

            def publish(kind: str, data: dict[str, Any]) -> None:
                self.publish(kind.replace("mcp.", "browser.", 1), data, session_id)
            log = log_path(session_id)
            log.parent.mkdir(parents=True, exist_ok=True)
            conn = self.connections[session_id] = Connection(config, publish, log_file=log)
            self.flags[session_id] = flag
        return conn

    async def call(self, session_id: str, tool: str, arguments: dict[str, Any],
                   *, used_before: Callable[[], bool] = lambda: False) -> tuple[Any, str | None]:
        """`(result, notice)`. A call that has to start the process again (it died, the setting changed, or the
        daemon restarted after this session used the browser) does so at once, without the MCP backoff, and
        returns `RESTART_NOTICE`. `used_before()` says whether the session called a browser tool in an earlier
        daemon; it is asked only when a process starts."""
        flag = CHROME_FLAG if self.use_my_chrome() else OWN_FLAG
        conn = self.connections.get(session_id)
        if conn is not None and (conn.state == "failed" or self.flags.get(session_id) != flag):
            await self.stop(session_id)
            conn = None
        fresh = conn is None or conn.state != "connected"
        if fresh and self._checking():
            needed = requirements(flag == CHROME_FLAG)
            if not needed["ok"]:  # say why plainly, rather than a failed process start
                raise McpUnavailable("the browser tools cannot start: " + "; ".join(needed["problems"]))
        again = fresh and (session_id in self.started or used_before())
        result = await self._connection(session_id, flag).call(tool, arguments)
        self.started.add(session_id)
        return result, RESTART_NOTICE if again else None

    async def stop(self, session_id: str) -> None:
        conn = self.connections.pop(session_id, None)
        self.flags.pop(session_id, None)
        if conn is not None:
            conn.retire()
            await conn.close()

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(s) for s in list(self.connections)), return_exceptions=True)
