"""Loopback-only native terminal screenshot server over a framed PTY bridge."""
from __future__ import annotations

import argparse
import asyncio
import json
import shlex
from pathlib import Path

HTML = '''<!doctype html><html><head><link rel="stylesheet" href="/assets/xterm.css">
<style>html,body,#terminal{margin:0;width:100%;height:100%;background:#141414}</style>
</head><body><div id="terminal"></div><script src="/assets/xterm.js"></script><script>
const term=new Terminal({fontSize:16,cols:122,rows:40,theme:{background:'#141414'}});
term.open(document.getElementById('terminal'));
term.textarea.setAttribute('aria-label','Terminal input');
term.textarea.removeAttribute('aria-hidden');
term.textarea.setAttribute('role','textbox');
const socket=new WebSocket('ws://'+location.host+'/terminal');
window.__nexusSockets=[socket];socket.binaryType='arraybuffer';
const encoder=new TextEncoder();
socket.onmessage=e=>term.write(new Uint8Array(e.data));
term.onData(data=>{if(socket.readyState===1)socket.send(encoder.encode(data));});
term.attachCustomKeyEventHandler(e=>{
 if(e.type==='keydown'&&e.key==='Enter'&&(e.shiftKey||e.ctrlKey)){
 socket.send(encoder.encode(e.ctrlKey?'\x1b[13;5u':'\x1b[13;2u'));return false;
 }return true;
});
function resize(){const size={width:Math.max(20,Math.floor(innerWidth/9.6)),height:Math.max(8,Math.floor(innerHeight/19))};
 term.resize(size.width,size.height);if(socket.readyState===1)socket.send(JSON.stringify(['resize',size]));}
socket.onopen=resize;window.onresize=resize;
</script></body></html>'''


def serve(command: str, *, host: str = "127.0.0.1", port: int = 8000, title: str | None = None) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route, Mount, WebSocketRoute
    from starlette.staticfiles import StaticFiles
    from starlette.websockets import WebSocketDisconnect

    if host != "127.0.0.1":
        raise ValueError("Terminal test server must listen on loopback")

    async def index(request):
        return HTMLResponse(HTML)

    async def terminal(socket):
        await socket.accept()
        process = await asyncio.create_subprocess_exec(*shlex.split(command), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024)

        async def output():
            if await process.stdout.readline() != b"__GANGLION__\n":
                raise RuntimeError("Expected a native PTY bridge command")
            while True:
                header = await process.stdout.readexactly(5)
                size = int.from_bytes(header[1:], "big")
                if size > 16 * 1024 * 1024:
                    raise ValueError("Terminal output packet exceeds limit")
                payload = await process.stdout.readexactly(size)
                if header[:1] == b"D":
                    await socket.send_bytes(payload)

        async def input_():
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    kind, payload = b"D", message["bytes"]
                else:
                    action, size = json.loads(message["text"])
                    if action != "resize":
                        continue
                    payload = json.dumps({"type": "resize", "width": max(20, min(500, int(size["width"]))),
                        "height": max(8, min(200, int(size["height"])))}).encode()
                    kind = b"M"
                if len(payload) > 1024 * 1024:
                    raise ValueError("Terminal input packet exceeds limit")
                process.stdin.write(kind + len(payload).to_bytes(4, "big") + payload)
                await process.stdin.drain()

        tasks = [asyncio.create_task(output()), asyncio.create_task(input_())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (asyncio.IncompleteReadError, WebSocketDisconnect):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process.returncode is None:
                process.terminate()
            await process.wait()

    app = Starlette(routes=[Route("/", index), WebSocketRoute("/terminal", terminal),
        Mount("/assets", StaticFiles(directory=Path(__file__).parent / "terminal_assets"))])
    uvicorn.run(app, host=host, port=port, log_level="warning", ws="wsproto", ws_max_size=1024 * 1024)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True, help="Native PTY bridge command")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--title")
    args = parser.parse_args()
    serve(args.command, host=args.host, port=args.port, title=args.title)


if __name__ == "__main__":
    main()
