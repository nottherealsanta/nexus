// Host proxy hardening (`node --test scripts/`): raw requests a browser page or local
// process can send must never crash the host or send the daemon token elsewhere.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
let daemon, host, port, home;
let finishVoiceStream;
const seen = [];

const listen = (server) => new Promise((r) => server.listen(0, "127.0.0.1", () => r(server.address().port)));

before(async () => {
  daemon = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      seen.push({ url: req.url, method: req.method, auth: req.headers.authorization, body: Buffer.concat(chunks).toString() });
      if (req.url === "/api/voice/speak") {
        res.writeHead(200, { "content-type": "audio/wav" });
        res.flushHeaders();
        res.write("first audio chunk");
        finishVoiceStream = () => { finishVoiceStream = undefined; res.end("second audio chunk"); };
      } else res.end("{}");
    });
  });
  const daemonPort = await listen(daemon);
  home = fs.mkdtempSync(path.join(os.tmpdir(), "nsh-host-"));
  fs.writeFileSync(path.join(home, "daemon.json"), JSON.stringify({ url: `http://127.0.0.1:${daemonPort}`, pid: 1, token: "secret" }));
  const probe = net.createServer();
  port = await listen(probe);
  await new Promise((r) => probe.close(r));
  host = spawn(process.execPath, [path.join(here, "serve.mjs"), "--port", String(port)], {
    env: { ...process.env, NEXUS10_HOME: home, NEXUS_NSH_NO_START: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  await new Promise((r) => host.stdout.once("data", r));
});

after(() => { finishVoiceStream?.(); host?.kill(); daemon?.close(); fs.rmSync(home, { recursive: true, force: true }); });

/** Send a raw HTTP/1.1 request (no client-side normalization) and return the status. */
function raw(target, headers = {}) {
  return new Promise((resolve, reject) => {
    const sock = net.connect(port, "127.0.0.1");
    let data = "";
    sock.on("data", (c) => { data += c; });
    sock.on("end", () => resolve(Number(data.split(" ")[1])));
    sock.on("error", reject);
    const lines = [`GET ${target} HTTP/1.1`, `Host: 127.0.0.1:${port}`, "Connection: close", ...Object.entries(headers).map(([k, v]) => `${k}: ${v}`)];
    sock.write(lines.join("\r\n") + "\r\n\r\n");
  });
}

const alive = async () => assert.equal(await raw("/__nexus/host"), 200);

test("Origin: null is refused, not a crash", async () => {
  assert.equal(await raw("/api/info", { Origin: "null" }), 403);
  await alive();
});

test("a malformed percent escape is refused, not a crash", async () => {
  assert.equal(await raw("/%"), 400);
  assert.equal(await raw("/%E0%A4%A"), 400);
  await alive();
});

test("a protocol-relative target never reaches another host", async () => {
  seen.length = 0;
  assert.equal(await raw("//attacker.example/api/x"), 400);
  assert.equal(seen.length, 0);
  await alive();
});

test("/api requests reach the daemon with its token", async () => {
  seen.length = 0;
  assert.equal(await raw("/api/info?x=1"), 200);
  assert.deepEqual(seen, [{ url: "/api/info?x=1", method: "GET", auth: "Bearer secret", body: "" }]);
});

test("the voice speak POST body and streamed WAV pass through the host proxy", async () => {
  seen.length = 0;
  finishVoiceStream = undefined;
  const response = await fetch(`http://127.0.0.1:${port}/api/voice/speak`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "audio/wav" },
    body: JSON.stringify({ session_id: "ses_voice" }),
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "audio/wav");
  const reader = response.body.getReader();
  const first = await reader.read();
  assert.equal(Buffer.from(first.value).toString(), "first audio chunk");
  finishVoiceStream();
  const second = await reader.read();
  assert.equal(Buffer.from(second.value).toString(), "second audio chunk");
  assert.equal(second.done, false);
  assert.equal((await reader.read()).done, true);
  assert.deepEqual(seen, [{
    url: "/api/voice/speak", method: "POST", auth: "Bearer secret",
    body: JSON.stringify({ session_id: "ses_voice" }),
  }]);
});
