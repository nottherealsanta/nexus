"""Serve the Nexus Textual shell to a browser with a working multiline editor.

This is a **development/test helper**, not a shipped surface: it lives under
``tests/``, is not installed with the ``nexus`` package, and no part of the CLI
imports it. It is used by the browser checks in
``tests/playwright_tui_check.py`` to give the real shell a frontend that can
report a modified Enter.

Why this exists
---------------
``textual serve`` serves a bundled xterm.js frontend whose default key handling
emits a bare carriage return for *every* Enter variant: Enter, Shift+Enter,
Ctrl+Enter and Cmd+Enter all reach the app as ``CR``, so the app cannot tell
"send" from "newline" (only Alt+Enter survives, as ``ESC CR`` — and Textual
8.2.8 discards that sequence). Verified against the served websocket in
``tests/playwright_tui_check.py``.

Textual itself already understands the Kitty keyboard protocol (CSI-u): its
parser resolves ``ESC [ 13 ; 2 u`` to ``shift+enter`` and ``ESC [ 13 ; 5 u`` to
``ctrl+enter`` (see ``textual/_xterm_parser.py::_parse_extended_key``). That is
exactly how a Kitty-capable terminal reports a modified Enter. textual-serve's
xterm.js does not emit it and exposes no ``modifyOtherKeys`` option, and the
WebDriver never negotiates the protocol.

The fix therefore belongs in the serving layer: this module serves textual-serve
unchanged except for one additive browser script that
  1. captures the live terminal websocket (textual-serve keeps the terminal
     instance private), and
   2. on capture-phase ``keydown`` rewrites Shift+Enter / Ctrl+Enter into the
      CSI-u sequence Textual parses, before xterm can collapse it to ``CR``.

Plain Enter is left alone, so it still sends ``CR`` and submits. Alt/Meta+Enter
is deliberately not rewritten (xterm.js does not preserve those modifiers
reliably here); this bridge only restores the Shift/Ctrl contract. The script is
injected into the *runtime copy* of textual-serve's own HTML template, so the
serving page tracks the installed dependency instead of forking it.

The Nexus core (``nexus/``) never imports this module or textual-serve; it only
knows the ``shift+enter`` / ``ctrl+enter`` key names, which are also what a
Kitty-capable terminal and the Textual pilot tests deliver.

Run directly::

    python tests/browser_serve.py --host 127.0.0.1 --port 8129 \
        --command "uv run python tests/visual_tui_demo.py --state functional"
"""

from __future__ import annotations

import argparse
import tempfile
from importlib import resources
from pathlib import Path

#: The exact tag textual-serve 1.1.3 emits in ``templates/app_index.html``. The
#: bridge must run before ``textual.js`` so it can capture ``window.WebSocket``
#: before the terminal opens its socket.
_TEMPLATE_MARKER = '<script src="{{ config.static.url }}js/textual.js"></script>'

#: Browser-side keyboard bridge. Kept dependency-free and additive: it never
#: touches the terminal API, only the socket and one capture-phase listener.
_KEYBOARD_BRIDGE = """<script>
      /* Nexus keyboard bridge: report Shift/Ctrl+Enter as Kitty CSI-u so the
         app can distinguish "newline" from the bare CR that Enter submits. */
      (function () {
        "use strict";
        var sockets = [];
        window.__nexusSockets = sockets;
        var NativeWebSocket = window.WebSocket;
        function NexusWebSocket(url, protocols) {
          var socket = arguments.length > 1
            ? new NativeWebSocket(url, protocols)
            : new NativeWebSocket(url);
          sockets.push(socket);
          return socket;
        }
        NexusWebSocket.prototype = NativeWebSocket.prototype;
        NexusWebSocket.CONNECTING = NativeWebSocket.CONNECTING;
        NexusWebSocket.OPEN = NativeWebSocket.OPEN;
        NexusWebSocket.CLOSING = NativeWebSocket.CLOSING;
        NexusWebSocket.CLOSED = NativeWebSocket.CLOSED;
        window.WebSocket = NexusWebSocket;

        function sendStdin(data) {
          var socket = sockets[sockets.length - 1];
          if (!socket || socket.readyState !== NexusWebSocket.OPEN) return false;
          socket.send(JSON.stringify(["stdin", data]));
          return true;
        }

        document.addEventListener("keydown", function (event) {
          if (event.key !== "Enter" || event.isComposing) return;
          if (!event.shiftKey && !event.ctrlKey) return;
          if (event.altKey || event.metaKey) return;
          /* Kitty modifier field is 1 + (shift | 2*alt | 4*ctrl | ...). */
          var modifiers = (event.shiftKey ? 1 : 0) | (event.ctrlKey ? 4 : 0);
          if (sendStdin("\\u001b[13;" + (modifiers + 1) + "u")) {
            event.preventDefault();
            event.stopImmediatePropagation();
          }
        }, true);
      })();
    </script>
    """


def _augmented_template() -> str:
    """Return textual-serve's index template with the keyboard bridge injected.

    Reading the installed template at runtime keeps this page in lock-step with
    the dependency; the only coupling is the marker below, which is asserted so
    a future template change fails loudly rather than silently dropping the
    bridge.
    """
    template = (
        resources.files("textual_serve")
        .joinpath("templates", "app_index.html")
        .read_text(encoding="utf-8")
    )
    if _TEMPLATE_MARKER not in template:
        raise RuntimeError(
            "textual-serve's app_index.html no longer contains the textual.js "
            "script tag; update tests/browser_serve.py for the new layout"
        )
    return template.replace(_TEMPLATE_MARKER, _KEYBOARD_BRIDGE + _TEMPLATE_MARKER, 1)


def serve(command: str, *, host: str = "127.0.0.1", port: int = 8000, title: str | None = None) -> None:
    """Serve ``command`` with the browser keyboard bridge installed."""
    from textual_serve.server import Server

    statics = resources.files("textual_serve").joinpath("static")
    templates = Path(tempfile.mkdtemp(prefix="nexus-browser-serve-"))
    (templates / "app_index.html").write_text(_augmented_template(), encoding="utf-8")

    Server(
        command=command,
        host=host,
        port=port,
        title=title,
        statics_path=str(statics),
        templates_path=str(templates),
    ).serve()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True, help="Textual app command to serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--title", default=None)
    args = parser.parse_args(argv)
    serve(args.command, host=args.host, port=args.port, title=args.title)


if __name__ == "__main__":
    main()
