#!/usr/bin/env node
// Visual capture: 390x844, 1024x768, 1440x900 x dark/light x states, against real Core processes.
//   node e2e/shots.mjs --out <dir> --scripted http://127.0.0.1:4820 --real http://127.0.0.1:4821 --ids ids.json --kill-pid <pid>
// `--real` is a host in front of `nexus daemon start` with no projects (empty workspace);
// `--scripted` is a host in front of e2e/scripted_core.py seeded by e2e/seed.mjs.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const OUT = arg("out"); const SCRIPTED = arg("scripted"); const REAL = arg("real");
const ids = JSON.parse(fs.readFileSync(arg("ids"), "utf8")); const killPid = Number(arg("kill-pid", 0));
const only = arg("only"); const sizes = [["390x844", 390, 844], ["1024x768", 1024, 768], ["1440x900", 1440, 900]];
fs.mkdirSync(OUT, { recursive: true });
const mod = process.platform === "darwin" ? "Meta" : "Control";
const tid = (p, id) => p.locator(`[data-testid="${id}"]`);
const settle = (p, ms = 500) => p.waitForTimeout(ms);
const shot = async (p, name, tag) => { await settle(p, 350); await p.screenshot({ path: path.join(OUT, `${tag}-${name}.png`) }); console.log("shot", `${tag}-${name}`); };

async function open(browser, url, w, h, scheme) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: scheme, deviceScaleFactor: 1, hasTouch: w < 500 });
  const p = await ctx.newPage();
  p.on("crash", () => console.log("PAGE CRASH")); p.on("close", () => console.log("page closed")); p.on("pageerror", (e) => console.log("pageerror", e.message.slice(0, 200)));
  await p.goto(url); await p.waitForSelector('[data-testid="header"]', { timeout: 15000 }); await settle(p, 1800);
  return { ctx, p };
}
async function openDrawerLeft(p, w) { if (w < 820) { await tid(p, "toggle-left").click(); await settle(p, 600); } }
// One Escape per open layer: further presses would walk the manifest chain on (interrupt, back), as designed.
const closeAll = async (p) => { await p.keyboard.press("Escape"); await settle(p, 400); };

const browser = await chromium.launch();
const live = []; // contexts kept for the reconnecting pass
for (const scheme of ["dark", "light"]) for (const [label, w, h] of sizes) {
  const tag = `${label}-${scheme}`;
  if (only && !tag.includes(only)) continue;
  // 1. empty workspace on a real `nexus daemon start`
  { const { ctx, p } = await open(browser, REAL, w, h, scheme); await shot(p, "01-empty-workspace", tag); if (w < 820) { await openDrawerLeft(p, w); await shot(p, "01b-empty-drawer", tag); } await ctx.close(); }
  const { ctx, p } = await open(browser, SCRIPTED, w, h, scheme);
  await openDrawerLeft(p, w);
  await tid(p, "chip-global").click(); await settle(p, 400);
  await shot(p, "02-sessions-list", tag);
  await tid(p, `session-${ids.refactor}`).click(); await settle(p, 1200);
  await shot(p, "03-session-messages", tag);
  await tid(p, "context-fold").click(); await settle(p, 300); await tid(p, "ctx-system-open").click(); await settle(p, 400); await shot(p, "03c-context-detail", tag); await closeAll(p);
  if (w >= 820) { await tid(p, "toggle-left").click(); await settle(p, 400); await shot(p, "03a-session-rail", tag); await tid(p, "toggle-left").click(); await settle(p, 300); }
  if (w >= 820) { await tid(p, "toggle-right").click(); await settle(p, 500); await shot(p, "03b-session-info", tag); await tid(p, "toggle-right").click(); await settle(p, 300); }
  else { await tid(p, "toggle-right").click(); await settle(p, 700); await shot(p, "08-drawer-right", tag); await closeAll(p); }
  // waiting approval
  await openDrawerLeft(p, w); await tid(p, `session-${ids.flaky}`).click(); await settle(p, 1200);
  await shot(p, "04-waiting-approval", tag);
  // palette
  await p.keyboard.press(`${mod}+p`); await settle(p, 700);
  await shot(p, "05-palette", tag);
  await p.keyboard.type("arch"); await settle(p, 400); await shot(p, "05b-palette-filtered", tag);
  await closeAll(p);
  // settings
  await p.keyboard.press(`${mod}+,`); await settle(p, 900);
  await shot(p, "06-settings", tag);
  await tid(p, "section-providers").click(); await settle(p, 700); await shot(p, "06b-settings-providers", tag);
  await p.keyboard.press("Escape"); await settle(p, 400);
  // confirm dialog
  await openDrawerLeft(p, w); await tid(p, `session-${ids.release}`).click(); await settle(p, 900);
  await p.keyboard.press(`${mod}+p`); await settle(p, 500); await p.keyboard.type("Delete session"); await settle(p, 300); await p.keyboard.press("Enter"); await settle(p, 700);
  await shot(p, "07-confirm-delete", tag);
  await closeAll(p);
  if (w < 820) { await openDrawerLeft(p, w); await shot(p, "08b-drawer-left-open", tag); await closeAll(p); }
  live.push({ ctx, p, tag, w });
}
// reconnecting banner: every page is live; stop the daemon, wait for the banner
if (killPid) {
  try { process.kill(killPid, "SIGTERM"); } catch (e) { console.log("kill failed", e.message); }
  await new Promise((r) => setTimeout(r, 3500));
  for (const { p, tag } of live) { await shot(p, "09-reconnecting", tag); }
}
for (const { ctx } of live) await ctx.close();
await browser.close();
