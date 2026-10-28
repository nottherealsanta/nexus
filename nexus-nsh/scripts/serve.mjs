#!/usr/bin/env node
// Same-origin host for the NSH web build: serves the packaged app and proxies
// /api/* to the Nexus Core daemon, adding the bearer token the browser cannot read.
//
//   node scripts/serve.mjs [--port 4820] [--static dist] [--upstream http://127.0.0.1:8790]
//
// Discovery follows the TUI: <home>/daemon.json ({url, pid, token}), re-read on every
// request so a daemon restart (new pid/token) is picked up without restarting this host.
// The token never reaches the browser. Binds 127.0.0.1 only. Zero npm dependencies.
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1] && !all[i + 1].startsWith("--") ? all[i + 1] : "1"]] : acc), []),
);
const here = path.dirname(fileURLToPath(import.meta.url));
const port = Number(args.port ?? process.env.NEXUS_NSH_PORT ?? 4820);
const staticDir = path.resolve(here, "..", args.static ?? "dist/Nexus");
const upstream = args.upstream ? new URL(args.upstream) : null;
const nexusHome = path.resolve(process.env.NEXUS10_HOME ?? path.join(os.homedir(), ".nexus10"));
if (nexusHome === path.join(os.homedir(), ".nexus") || nexusHome.startsWith(path.join(os.homedir(), ".nexus") + path.sep)) {
  console.error("refusing to use ~/.nexus (old Nexus); set NEXUS10_HOME");
  process.exit(2);
}
const coreCmd = (process.env.NEXUS_NSH_CORE_CMD ?? "nexus").split(" ");
const noStart = !!process.env.NEXUS_NSH_NO_START;
const allowedHosts = new Set([`127.0.0.1:${port}`, `localhost:${port}`]);

const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".wasm": "application/wasm", ".json": "application/json", ".webmanifest": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml", ".bin": "application/octet-stream", ".nshc": "application/octet-stream", ".otf": "font/otf", ".ttf": "font/ttf", ".css": "text/css" };

function daemonInfo() {
  try {
    const d = JSON.parse(fs.readFileSync(path.join(nexusHome, "daemon.json"), "utf8"));
    if (typeof d.url === "string" && typeof d.token === "string") return d;
  } catch {}
  return null;
}
const json = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
};

/** The host of an Origin header; "" for `null` or anything unparsable (refused). */
function originHost(origin) {
  try { return new URL(origin).host; } catch { return ""; }
}

// DNS-rebinding and cross-site request guard: a page on another origin can send requests
// to 127.0.0.1 but cannot read answers; refuse anything that is not this host's own page.
function guard(req, res) {
  if (!allowedHosts.has(req.headers.host ?? "")) return json(res, 403, { error: "bad host" }), false;
  // Only origin-form targets ("/path"); "//host/path" would parse as another host.
  if (!req.url?.startsWith("/") || req.url.startsWith("//")) return json(res, 400, { error: "bad request target" }), false;
  const origin = req.headers.origin;
  if (origin && !allowedHosts.has(originHost(origin))) return json(res, 403, { error: "cross-origin request refused" }), false;
  if (req.method !== "GET" && req.method !== "HEAD" && !origin && req.headers["sec-fetch-site"] && req.headers["sec-fetch-site"] !== "same-origin") return json(res, 403, { error: "cross-site request refused" }), false;
  return true;
}

let starting = null;
function startCore() {
  if (starting) return starting;
  starting = new Promise((resolve) => {
    const logDir = path.join(nexusHome, "logs");
    fs.mkdirSync(logDir, { recursive: true, mode: 0o700 });
    const log = fs.openSync(path.join(logDir, "nsh-host.log"), "a");
    const child = spawn(coreCmd[0], [...coreCmd.slice(1), "daemon", "start"], { stdio: ["ignore", log, log], env: process.env });
    child.on("exit", (code) => { starting = null; resolve(code); });
    child.on("error", () => { starting = null; resolve(-1); });
  });
  return starting;
}

function proxyApi(req, res) {
  const d = daemonInfo();
  if (!d) return json(res, 502, { error: "Nexus Core is not running (no daemon.json)", code: "no_daemon" });
  // Path and query only, on the daemon's own origin: the request never picks the host.
  const { pathname, search } = new URL(req.url, "http://x");
  const target = new URL(pathname + search, new URL(d.url).origin);
  const headers = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (["host", "origin", "referer", "authorization", "cookie", "connection", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"].includes(k)) continue;
    headers[k] = v;
  }
  headers.authorization = `Bearer ${d.token}`;
  const out = http.request(target, { method: req.method, headers }, (up) => {
    const h = { ...up.headers, "cache-control": "no-store" };
    delete h.connection;
    res.writeHead(up.statusCode ?? 502, h);
    up.pipe(res);
    up.on("error", () => res.destroy());
  });
  out.on("error", (e) => { if (!res.headersSent) json(res, 502, { error: `Nexus Core unreachable: ${e.code ?? e.message}`, code: "unreachable" }); else res.destroy(); });
  res.on("close", () => out.destroy()); // browser aborted (AbortController / reload): drop the SSE upstream
  req.pipe(out);
}

function serveStatic(req, res) {
  if (upstream) {
    const out = http.request({ host: upstream.hostname, port: upstream.port, path: req.url, method: req.method, headers: { ...req.headers, host: upstream.host } }, (up) => { res.writeHead(up.statusCode ?? 502, up.headers); up.pipe(res); });
    out.on("error", () => json(res, 502, { error: "nsh dev server unreachable" }));
    return req.pipe(out);
  }
  let rel;
  try { rel = decodeURIComponent(new URL(req.url, "http://x").pathname); } catch { return json(res, 400, { error: "malformed path" }); }
  if (rel.endsWith("/")) rel += "index.html";
  const file = path.join(staticDir, path.normalize(rel));
  if (!file.startsWith(staticDir + path.sep) && file !== staticDir) return json(res, 403, { error: "forbidden" });
  fs.readFile(file, (err, buf) => {
    if (err) return json(res, 404, { error: "not found (run npm run build)" });
    const ext = path.extname(file);
    res.writeHead(200, { "content-type": MIME[ext] ?? "application/octet-stream", "cache-control": rel.endsWith("index.html") ? "no-cache" : "public, max-age=3600" });
    res.end(buf);
  });
}

http.createServer((req, res) => {
  try { route(req, res); } catch (e) {
    console.error("request failed:", e);
    if (!res.headersSent) json(res, 500, { error: "host error" }); else res.destroy();
  }
}).listen(port, "127.0.0.1", () => console.log(`nexus-nsh host on http://127.0.0.1:${port}  home=${nexusHome}  ${upstream ? "upstream=" + upstream.href : "static=" + staticDir}`));

function route(req, res) {
  if (!guard(req, res)) return;
  const url = new URL(req.url, "http://x");
  if (url.pathname === "/__nexus/host") {
    const d = daemonInfo();
    return json(res, 200, { host: "nexus-nsh proxy", home: nexusHome, userHome: os.homedir(), daemon: d ? { url: d.url, pid: d.pid } : null, canStart: !noStart, startCommand: `${coreCmd.join(" ")} daemon start` });
  }
  if (url.pathname === "/__nexus/start" && req.method === "POST") {
    if (noStart) return json(res, 403, { error: "starting Core is disabled (NEXUS_NSH_NO_START)" });
    return startCore().then((code) => json(res, code === 0 ? 200 : 500, { exit: code }));
  }
  if (url.pathname === "/api" || url.pathname.startsWith("/api/") || url.pathname === "/inspector") return proxyApi(req, res);
  serveStatic(req, res);
}
