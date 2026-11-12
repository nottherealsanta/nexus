// Phone screenshots of the NSH web build at 390x844 with the phone client forced (`?client=phone`) and the API mocked,
// so the run needs no Core and no provider. Usage: node shots.mjs <dist/Nexus dir> <port> <out dir>
import { chromium } from "../../nexus-nsh/node_modules/playwright/index.mjs";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const [dist, port, out] = [process.argv[2], Number(process.argv[3]), process.argv[4]];
const types = { ".html": "text/html", ".js": "text/javascript", ".wasm": "application/wasm", ".css": "text/css", ".png": "image/png", ".ttf": "font/ttf", ".json": "application/json", ".bin": "application/octet-stream", ".svg": "image/svg+xml" };
const server = http.createServer((req, res) => {
  const p = path.join(dist, decodeURIComponent(new URL(req.url, "http://x").pathname));
  const file = fs.existsSync(p) && fs.statSync(p).isFile() ? p : path.join(dist, "index.html");
  res.writeHead(200, { "content-type": types[path.extname(file)] ?? "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}).listen(port, "127.0.0.1");

const now = Math.floor(Date.now() / 1000);
const base = { model: "anthropic/sonnet", permission: "ask", effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const mk = (id, project, dir, title, ago, extra = {}) => ({ ...base, id, project_id: project, directory: dir, title, created: now - ago - 100, updated: now - ago, ...extra });
const sessions = [
  mk("ses_a", "prj_1", "/work/nexus", "Migrate the sessions table", 40),
  mk("ses_b", "prj_1", "/work/nexus", "Fix flaky timeline scroll test", 300, { status: "busy" }),
  mk("ses_c", "prj_1", "/work/nexus", "Refactor the approval card", 3600),
  mk("ses_d", "prj_2", "/work/site", "Write the release notes", 7200),
  mk("ses_e", "prj_2", "/work/site", "Audit the docs for stale links", 90000),
  mk("ses_f", "prj_1", "/work/nexus", "Plan the mobile redesign", 200000),
];
const projects = [
  { id: "prj_1", path: "/work/nexus", name: "nexus", created: 1, updated: 2, revision: 1 },
  { id: "prj_2", path: "/work/site", name: "site", created: 1, updated: 2, revision: 1 },
];
const approval = { id: "perm_1", session_id: "ses_a", tool: "bash", call_id: "c1", args: { command: "uv run pytest -q tests/test_store.py && git status --short" } };
const messages = (id) => id === "ses_a" ? [
  { id: "m1", role: "user", parts: [{ type: "text", text: "Move the sessions table to the new schema and run the store tests." }], time: now - 120 },
  { id: "m2", role: "assistant", parts: [{ type: "text", text: "I will add the migration, then run the store tests. The change touches `store.py` and one test file." }], time: now - 100 },
] : [];

const json = (route, body, delay = 0) => setTimeout(() => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) }), delay);
async function mock(context) {
  await context.route("**/__nexus/host", (r) => json(r, { host: "test", canStart: false }));
  await context.route("**/api/**", (route) => {
    const u = new URL(route.request().url());
    const p = u.pathname;
    const method = route.request().method();
    if (p === "/api/event") return route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n` });
    if (p === "/api/info") return json(route, { name: "nexus", version: "0.10.0", pid: 4242, home: "~/.nexus" }, 25 + Math.round(Math.random() * 60));
    if (p === "/api/config") return json(route, { revision: "t", values: { defaults: { model: "", effort: "", agent: "", permission: "ask" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true } }, defaults: {}, schema: {}, errors: [] });
    if (p === "/api/snapshot") return json(route, { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects, sessions, running: [], approvals: [approval], questions: [], selected: null });
    let m;
    if ((m = p.match(/^\/api\/session\/([^/]+)\/message/))) return json(route, messages(m[1]));
    if ((m = p.match(/^\/api\/session\/([^/]+)\/turn/))) return json(route, { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } });
    if ((m = p.match(/^\/api\/session\/([^/]+)\/context/))) return json(route, { used: 0, window: null, exact: false });
    if ((m = p.match(/^\/api\/session\/([^/]+)\/worktrees/))) return json(route, { worktrees: [] });
    if ((m = p.match(/^\/api\/session\/([^/]+)\/input/))) return json(route, { held: false, inputs: [] });
    if ((m = p.match(/^\/api\/session\/([^/]+)\/snapshot/))) return json(route, null);
    if ((m = p.match(/^\/api\/session\/([^/]+)$/))) return json(route, sessions.find((s) => s.id === m[1]) ?? sessions[0]);
    if (p === "/api/provider") return json(route, [{ id: "anthropic", label: "Anthropic", connected: true }, { id: "codex", label: "ChatGPT (Codex)", connected: false }]);
    if (p === "/api/model" || p.startsWith("/api/model/")) return json(route, { source: "t", models: [], model: "anthropic/sonnet" });
    if (p === "/api/agent") return json(route, { agents: [], problems: [] });
    if (p === "/api/skill") return json(route, { skills: [], problems: [] });
    if (p === "/api/mcp") return json(route, { servers: [] });
    if (p === "/api/search") return json(route, { hits: [] });
    return json(route, {});
  });
  // A LAN Core (registered last, so it wins over the catch-all above) that cannot be reached (Local Network permission denied, or off): the pairing error state.
  await context.route("http://192.168.1.20:4747/**", (r) => r.abort("connectionrefused"));
}

const browser = await chromium.launch();
try {
  for (const scheme of ["dark", "light"]) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true, colorScheme: scheme });
    await mock(context);
    const page = await context.newPage();
    page.on("pageerror", (e) => console.log("page error:", e.message));
    const shot = async (name) => { await page.waitForTimeout(700); await page.screenshot({ path: path.join(out, `${scheme}-${name}.png`) }); console.log("saved", scheme, name); };
    const tap = async (id) => { await page.locator(`[data-testid="${id}"]`).first().click(); };
    await page.goto(`http://127.0.0.1:${port}/?client=phone`);
    await page.locator('[data-testid="phone-shell"]').waitFor({ timeout: 30000 });
    await page.locator('[data-testid="session-ses_a"]').waitFor();
    await shot("1-sessions");
    await tap("project-selector"); await page.locator('[data-testid="project-sheet"]').waitFor(); await shot("2-project-switcher");
    await tap("project-all");
    await page.locator('[data-testid="session-ses_c"]').first().click({ delay: 700 });
    await page.locator('[data-testid="row-sheet"]').waitFor({ timeout: 5000 }).catch(() => {});
    await shot("3-row-menu");
    await page.keyboard.press("Escape");
    await page.locator('[data-testid="row-sheet-scrim"]').click({ position: { x: 190, y: 120 } }).catch(() => {});
    await tap("phone-tab-manager"); await shot("4-manager");
    await tap("phone-tab-settings"); await page.locator('[data-testid="connection-card"]').waitFor(); await page.waitForTimeout(9000); await shot("5-settings");
    await tap("phone-setting-appearance"); await shot("6-settings-page");
    await tap("phone-settings-back");
    await tap("core-change"); await page.locator('[data-testid="phone-connect"]').waitFor(); await shot("7-pairing");
    await page.locator('[data-testid="connect-link"]').click();
    await page.keyboard.type("nexus://pair?url=http%3A%2F%2F192.168.1.20%3A4747&name=studio");
    await page.locator('[data-testid="connect-submit"]').click();
    await page.locator('[data-testid="connect-error"]').waitFor({ timeout: 20000 }).catch(() => console.log("no connect error shown"));
    await shot("8-pairing-error");
    await page.goto(`http://127.0.0.1:${port}/?client=phone`);
    await page.locator('[data-testid="session-ses_a"]').waitFor({ timeout: 30000 });
    await tap("session-ses_a");
    await page.locator('[data-testid="phone-approval"]').waitFor({ timeout: 15000 });
    await shot("9-approval");
    await tap("phone-drawer-button"); await page.locator('[data-testid="phone-drawer"]').waitFor(); await shot("10-drawer");
    await context.close();
  }
} finally { await browser.close(); server.close(); }
