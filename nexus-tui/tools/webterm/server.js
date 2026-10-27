// A tiny static server + WebSocket PTY bridge. The page (public/index.html)
// shows an xterm.js terminal wired to a real process in a PTY, so the real
// `nexus-tui` binary can be driven and photographed in a browser.
//
// Profiles are registered by the caller (shots.mjs): the browser only names a
// profile, it never supplies a command.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";
import pty from "node-pty";

const here = path.dirname(fileURLToPath(import.meta.url));
const modules = path.join(here, "node_modules");
const files = {
  "/": [path.join(here, "public/index.html"), "text/html"],
  "/xterm.js": [path.join(modules, "@xterm/xterm/lib/xterm.js"), "text/javascript"],
  "/xterm.css": [path.join(modules, "@xterm/xterm/css/xterm.css"), "text/css"],
  "/addon-canvas.js": [path.join(modules, "@xterm/addon-canvas/lib/addon-canvas.js"), "text/javascript"],
};

export function createServer() {
  const profiles = new Map();
  const children = new Set();
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    const hit = files[url.pathname];
    if (!hit) {
      res.writeHead(404).end("not found");
      return;
    }
    res.writeHead(200, { "content-type": hit[1] });
    fs.createReadStream(hit[0]).pipe(res);
  });
  const wss = new WebSocketServer({ server, path: "/pty" });
  wss.on("connection", (ws, req) => {
    const q = new URL(req.url, "http://localhost").searchParams;
    const profile = profiles.get(q.get("profile"));
    const cols = Number(q.get("cols")) || 80;
    const rows = Number(q.get("rows")) || 24;
    if (!profile) {
      ws.close(1008, "unknown profile");
      return;
    }
    const child = pty.spawn(profile.file, profile.args ?? [], {
      name: "xterm-256color",
      cols,
      rows,
      cwd: profile.cwd ?? process.cwd(),
      env: { ...process.env, TERM: "xterm-256color", COLORTERM: "truecolor", LANG: "en_US.UTF-8", ...profile.env },
    });
    children.add(child);
    child.onData((d) => ws.readyState === 1 && ws.send(JSON.stringify({ t: "out", d })));
    child.onExit(({ exitCode }) => {
      children.delete(child);
      if (ws.readyState === 1) ws.send(JSON.stringify({ t: "exit", code: exitCode }));
    });
    ws.on("message", (m) => {
      const msg = JSON.parse(m.toString());
      if (msg.t === "in") child.write(msg.d);
      if (msg.t === "resize") child.resize(msg.cols, msg.rows);
    });
    ws.on("close", () => {
      try {
        child.kill();
      } catch {}
    });
  });
  return {
    profiles,
    listen: (port = 0) =>
      new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve(server.address().port))),
    close: () => {
      for (const c of children) {
        try {
          c.kill();
        } catch {}
      }
      wss.close();
      server.close();
    },
  };
}

// `npm run serve`: serve one profile ("default") running the command in
// WEBTERM_CMD (JSON array), for poking at the TUI by hand in a browser.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const cmd = JSON.parse(process.env.WEBTERM_CMD ?? '["../../target/debug/nexus-tui"]');
  const s = createServer();
  s.profiles.set("default", { file: path.resolve(here, cmd[0]), args: cmd.slice(1) });
  const port = await s.listen(Number(process.env.PORT) || 7681);
  console.log(`http://127.0.0.1:${port}/?profile=default&cols=120&rows=36`);
}
