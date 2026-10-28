// Screenshots of the real `nexus-tui` binary running in a PTY against a real
// (isolated) Core. Usage:
//   node shots.mjs --env <root>/env --out <dir> [--bin <path>] [--only <substr>]
// <root>/env comes from seed.sh. Each shot is an xterm.js terminal of exactly
// cols x rows cells, rendered by headless Chromium with Monaspace Argon.
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "./server.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const arg = (n, d) => {
  const i = process.argv.indexOf(`--${n}`);
  return i > 0 ? process.argv[i + 1] : d;
};
const envFile = arg("env");
const outDir = arg("out");
const only = arg("only", "");
const bin = path.resolve(arg("bin", path.join(here, "../../target/debug/nexus-tui")));
if (!envFile || !outDir) {
  console.error("usage: node shots.mjs --env <root>/env --out <dir> [--bin path] [--only substr]");
  process.exit(2);
}
const cfg = Object.fromEntries(
  fs.readFileSync(envFile, "utf8").trim().split("\n").map((l) => l.split(/=(.*)/s).slice(0, 2)),
);
fs.mkdirSync(outDir, { recursive: true });

const core = path.resolve(here, "../../../nexus-core");
const nexus = (home, port, ...args) =>
  execFileSync("uv", ["run", "nexus", ...args], {
    cwd: core,
    env: { ...process.env, NEXUS10_HOME: home, NEXUS_PORT: String(port) },
    stdio: "pipe",
  });

const THEMES = {
  dark: { bg: "#0B0B0B", fg: "#e6e7e8", flag: "--dark" },
  light: { bg: "#f7f7f5", fg: "#1f2124", flag: "--light" },
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const server = createServer();
const port = await server.listen(0);
console.log("server on", port);
const browser = await chromium.launch();
const results = [];
console.log("browser up");

// One shot: spawn the binary in a PTY, drive it with `steps`, photograph it.
async function shot({ name, cols, rows, theme = "dark", args = [], env = {}, home = cfg.WEBTERM_HOME, steps = async () => {} }) {
  if (only && !name.includes(only)) return;
  const t = THEMES[theme];
  // Each shot starts from default client preferences.
  fs.rmSync(path.join(home, "tui.json"), { force: true });
  const profile = `p${results.length}`;
  server.profiles.set(profile, {
    file: bin,
    args: [t.flag, ...args],
    env: { NEXUS10_HOME: home, NEXUS_TUI_NO_START: "1", NEXUS10_REDUCED_MOTION: "1", ...env },
  });
  const page = await browser.newPage({ viewport: { width: 1800, height: 1100 } });
  await page.goto(`http://127.0.0.1:${port}/?profile=${profile}&cols=${cols}&rows=${rows}&bg=${encodeURIComponent(t.bg)}&fg=${encodeURIComponent(t.fg)}`);
  const h = {
    page,
    idle: (ms = 500) => page.evaluate((m) => window.__idle(m), ms),
    key: async (k, wait = 250) => {
      await page.keyboard.press(k);
      await sleep(wait);
    },
    type: async (s, wait = 250) => {
      await page.keyboard.type(s, { delay: 15 });
      await sleep(wait);
    },
    text: () => page.evaluate(() => window.__text()),
    waitFor: async (needle, ms = 15000) => {
      const end = Date.now() + ms;
      while (Date.now() < end) {
        if ((await h.text()).includes(needle)) return;
        await sleep(100);
      }
      throw new Error(`${name}: "${needle}" never appeared:\n${await h.text()}`);
    },
    // Move the pointer to cell (col, row), zero-based.
    hover: async (col, row, wait = 300) => {
      const box = await page.locator("#term .xterm-screen").boundingBox();
      await page.mouse.move(box.x + (col + 0.5) * (box.width / cols), box.y + (row + 0.5) * (box.height / rows));
      await sleep(wait);
    },
    // Switch the left sidebar between full, rail and hidden from Settings.
    sidebar: async (steps = 1) => {
      await h.key("Control+p", 300);
      await h.type("open settings", 300);
      await h.key("Enter", 400);
      await h.type("sidebar", 200);
      for (let i = 0; i < steps; i++) await h.key("ArrowRight", 100);
      await h.key("Escape", 100);
      await h.key("Escape", 300);
    },
    // Open the seeded conversation through the session switcher (ctrl+k).
    openSession: async () => {
      await h.key("Control+k", 400);
      await h.type("Refactor", 300);
      await h.key("Enter", 600);
      await h.waitFor("Refactor the loader");
      await h.idle(500);
    },
  };
  const log = (m) => process.env.WEBTERM_DEBUG && console.log(`  [${name}] ${m}`);
  log("waiting for header");
  await h.waitFor("New session", 20000).catch(async () => {});
  log("idle");
  await h.idle(500);
  log("steps");
  await steps(h);
  log("settle");
  await h.idle(400);
  const file = path.join(outDir, `${name}.png`);
  await page.locator("#term").screenshot({ path: file });
  fs.writeFileSync(file.replace(/\.png$/, ".txt"), (await h.text()) + "\n");
  results.push(file);
  console.log("shot", file);
  await page.close();
}

const SIZES = [[80, 24], [120, 36], [160, 48]];

// 1. The shell with a conversation, three sizes, dark and light.
for (const [cols, rows] of SIZES) {
  for (const theme of ["dark", "light"]) {
    await shot({ name: `session-${cols}x${rows}-${theme}`, cols, rows, theme, steps: (h) => h.openSession() });
  }
}
// 2. Variants at 120x36: monochrome, ASCII glyphs, 256 colours, no sessions open.
await shot({ name: "session-120x36-mono", cols: 120, rows: 36, args: ["--mono"], steps: (h) => h.openSession() });
await shot({ name: "session-120x36-ascii", cols: 120, rows: 36, args: ["--ascii"], steps: (h) => h.openSession() });
await shot({ name: "session-120x36-256", cols: 120, rows: 36, args: ["--256"], env: { COLORTERM: "" }, steps: (h) => h.openSession() });
await shot({ name: "workspace-120x36-dark", cols: 120, rows: 36 });
// 3. States at 120x36 dark (light for the palette).
await shot({ name: "empty-workspace-120x36-dark", cols: 120, rows: 36, home: cfg.WEBTERM_HOME_EMPTY });
await shot({ name: "palette-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.key("Control+p", 400); } });
await shot({ name: "palette-120x36-light", cols: 120, rows: 36, theme: "light", steps: async (h) => { await h.openSession(); await h.key("Control+p", 400); } });
await shot({ name: "palette-filtered-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.key("Control+p", 300); await h.type("steer", 400); } });
await shot({ name: "palette-80x24-dark", cols: 80, rows: 24, steps: async (h) => { await h.openSession(); await h.key("Control+p", 300); await h.type("sess", 400); } });
await shot({ name: "settings-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.key("Control+p", 300); await h.type("open settings", 300); await h.key("Enter", 500); } });
await shot({ name: "settings-about-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.key("Control+p", 300); await h.type("open settings", 300); await h.key("Enter", 400); for (let i = 0; i < 11; i++) await h.key("ArrowDown", 60); await h.key("Enter", 400); } });
await shot({ name: "settings-light-80x24", cols: 80, rows: 24, theme: "light", steps: async (h) => { await h.key("Control+p", 300); await h.type("open settings", 300); await h.key("Enter", 500); } });
await shot({ name: "confirm-delete-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.key("Control+p", 300); await h.type("delete sess", 300); await h.key("Enter", 500); } });
await shot({ name: "toast-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.key("Control+p", 300); await h.type("copy session id", 300); await h.key("Enter", 400); } });
await shot({ name: "composer-multiline-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.type("first line of a longer draft", 100); await h.key("Control+j", 100); await h.type("second line", 100); await h.key("Control+j", 100); await h.type("third 日本語 line", 300); } });
await shot({ name: "drawer-80x24-dark", cols: 80, rows: 24, steps: async (h) => { await h.openSession(); await h.key("Control+b", 500); } });
await shot({ name: "drawer-right-80x24-dark", cols: 80, rows: 24, steps: async (h) => { await h.openSession(); await h.key("Alt+b", 500); } });
await shot({ name: "drawer-80x24-light", cols: 80, rows: 24, theme: "light", steps: async (h) => { await h.openSession(); await h.key("Control+b", 500); } });
await shot({ name: "sidebars-closed-160x48-dark", cols: 160, rows: 48, steps: async (h) => { await h.openSession(); await h.key("Control+b", 200); await h.key("Alt+b", 400); } });
await shot({ name: "rail-160x48-dark", cols: 160, rows: 48, steps: async (h) => { await h.openSession(); await h.sidebar(1); } });
await shot({ name: "rail-hover-160x48-dark", cols: 160, rows: 48, steps: async (h) => { await h.openSession(); await h.sidebar(1); await h.hover(3, 5, 500); } });
await shot({ name: "rail-hover-120x36-light", cols: 120, rows: 36, theme: "light", steps: async (h) => { await h.openSession(); await h.sidebar(1); await h.hover(3, 8, 500); } });
await shot({ name: "tab-hover-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.hover(6, 1, 500); } });
await shot({ name: "complete-slash-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.type("/mo", 600); } });
await shot({ name: "complete-at-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.waitFor("side-project"); await h.type("look at @s", 1500); } });
await shot({ name: "new-session-120x36-dark", cols: 120, rows: 36 });
await shot({ name: "projects-tab-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.key("Alt+3", 500); } });
await shot({ name: "files-tab-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.key("Alt+2", 500); } });
await shot({ name: "editor-selection-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.type("Selected text", 100); for (let i = 0; i < 4; i++) await h.key("Shift+ArrowLeft", 80); } });
await shot({ name: "editor-paste-80x24-light", cols: 80, rows: 24, theme: "light", steps: async (h) => { await h.openSession(); await h.page.evaluate(() => window.term.paste("first line\nsecond line\nthird line")); await h.waitFor("Pasted ~"); } });
await shot({ name: "attention-error-120x36-dark", cols: 120, rows: 36, steps: async (h) => {
  await h.openSession();
  const daemon = JSON.parse(fs.readFileSync(path.join(cfg.WEBTERM_HOME, "daemon.json"), "utf8"));
  const headers = { authorization: `Bearer ${daemon.token}`, "content-type": "application/json" };
  const sessions = await (await fetch(`${daemon.url}/api/session`, { headers })).json();
  const target = sessions.find((session) => session.title === "Fix flaky test in sync");
  if (!target) throw new Error("attention fixture session missing");
  const model = await fetch(`${daemon.url}/api/session/${target.id}/model`, { method: "POST", headers, body: JSON.stringify({ model: "codex/gpt-5.5" }) });
  if (!model.ok) throw new Error(`attention fixture model: ${model.status}`);
  const prompt = await fetch(`${daemon.url}/api/session/${target.id}/prompt`, { method: "POST", headers, body: JSON.stringify({ text: "local attention fixture" }) });
  if (!prompt.ok) throw new Error(`attention fixture prompt: ${prompt.status}`);
  await h.waitFor("✕ Fix flaky");
} });
await shot({ name: "mermaid-review-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.waitFor("source below"); } });
await shot({ name: "settings-detail-120x36-light", cols: 120, rows: 36, theme: "light", steps: async (h) => { await h.key("Control+p", 200); await h.type("open settings", 200); await h.key("Enter", 300); await h.key("Enter", 300); } });
await shot({ name: "fence-review-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.waitFor("printf done"); await h.key("Alt+k", 150); for (let i = 0; i < 24; i++) await h.key("ArrowDown", 40); await h.key("ArrowUp", 200); await h.waitFor("Copy"); } });
await shot({ name: "tools-expanded-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.openSession(); await h.key("Alt+k", 200); await h.key("Home", 200); for (let i = 0; i < 4; i++) await h.key("ArrowDown", 80); await h.key("Enter", 300); } });
await shot({ name: "files-open-120x36-dark", cols: 120, rows: 36, steps: async (h) => { await h.waitFor("side-project"); await h.key("Alt+2", 500); await h.waitFor("hello.txt"); for (let i = 0; i < 3; i++) await h.key("ArrowDown", 80); await h.key("Enter", 500); await h.waitFor("Real Core file browsing works."); } });
await shot({ name: "files-tree-120x36-dark", cols: 120, rows: 36, steps: async (h) => {
  await h.key("Control+k", 400); await h.type("Spike", 300); await h.key("Enter", 600);
  await h.waitFor("Spike: terminal harness");
  await h.key("Alt+2", 500); await h.waitFor("src");
  await h.key("Enter", 600); await h.waitFor("new.rs");
} });
// 4. Connection loss: stop the daemon under a live TUI (it never restarts it).
if (!only || "reconnecting".includes(only) || only.includes("reconnect")) {
  await shot({
    name: "reconnecting-120x36-dark", cols: 120, rows: 36,
    steps: async (h) => {
      await h.openSession();
      nexus(cfg.WEBTERM_HOME, cfg.WEBTERM_PORT, "daemon", "stop");
      await h.waitFor("Disconnected", 20000);
      await sleep(600);
    },
  });
  nexus(cfg.WEBTERM_HOME, cfg.WEBTERM_PORT, "daemon", "start");
  await shot({
    name: "reconnecting-80x24-light", cols: 80, rows: 24, theme: "light",
    steps: async (h) => {
      await h.openSession();
      nexus(cfg.WEBTERM_HOME, cfg.WEBTERM_PORT, "daemon", "stop");
      await h.waitFor("Disconnected", 20000);
      await sleep(600);
    },
  });
  nexus(cfg.WEBTERM_HOME, cfg.WEBTERM_PORT, "daemon", "start");
}

await browser.close();
server.close();
console.log(`${results.length} screenshots in ${outDir}`);
process.exit(0);
