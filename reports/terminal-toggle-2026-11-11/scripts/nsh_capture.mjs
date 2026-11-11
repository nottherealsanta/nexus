// Real NSH web captures for the Ctrl+` / Ctrl+~ terminal toggle against the same
// tokenless scratch Core and the same terminal resources the TUI used.
// Usage: node nsh_capture.mjs <out-dir>
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const nshDir = path.resolve(process.env.NEXUS_NSH_DIR ?? path.join(here, "../../../nexus-nsh"));
const { chromium } = createRequire(path.join(nshDir, "package.json"))("playwright");

const out = path.resolve(process.argv[2] ?? "after");
const scratch = process.env.NEXUS_TOGGLE_SCRATCH ?? "/tmp/nexus-terminal-toggle-2026-11-11";
const url = process.env.NEXUS_TOGGLE_NSH_URL ?? "http://127.0.0.1:47972";
const ids = JSON.parse(fs.readFileSync(path.join(scratch, "ids.json"), "utf8"));
const daemon = JSON.parse(fs.readFileSync(path.join(scratch, "home", "daemon.json"), "utf8"));
assert.equal(daemon.token, "", "expected tokenless Core");
fs.mkdirSync(out, { recursive: true });

const tuiIdFile = path.join(out, "tui-terminal-id.txt");
const tuiTerminalId = fs.existsSync(tuiIdFile) ? fs.readFileSync(tuiIdFile, "utf8").trim() : "";

const api = async (p, init = {}) => {
  const r = await fetch(daemon.url + p, init);
  const text = await r.text();
  return { status: r.status, body: text ? JSON.parse(text) : null };
};
const list = async () => {
  const r = await api(`/api/terminal?project_id=${ids.project_id}`);
  assert.equal(r.status, 200, `terminal list: ${r.status} ${JSON.stringify(r.body)}`);
  return r.body;
};
const resource = async id => {
  const r = await api(`/api/terminal/${id}`);
  assert.equal(r.status, 200, `terminal get: ${r.status} ${JSON.stringify(r.body)}`);
  return r.body;
};

const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark", deviceScaleFactor: 1 });
  await context.addInitScript(() => {
    localStorage.setItem("nexus.prefs", JSON.stringify({ theme: "dark", leftOpen: true, rightOpen: true, leftTab: "sessions", timelineDetail: "medium", scrollBar: true }));
  });
  const page = await context.newPage();
  const terminalResponses = [];
  let creates = 0;
  page.on("response", r => {
    if (!r.url().includes("/api/terminal")) return;
    terminalResponses.push({ status: r.status(), method: r.request().method(), url: r.url(), body: r.request().postData()?.slice(0, 200) });
    if (r.request().method() === "POST" && new URL(r.url()).pathname === "/api/terminal") creates++;
  });
  page.on("pageerror", e => console.error("pageerror:", e.message));

  await page.goto(url);
  await page.getByTestId("composer-panel").waitFor({ timeout: 20000 });

  // Open the seeded session from the sidebar; fall back to the session switcher.
  try {
    await page.getByTestId(`session-${ids.session_id}`).click({ timeout: 8000 });
  } catch {
    await page.keyboard.press("Meta+k");
    await page.getByTestId(`switch-session-${ids.session_id}`).click();
  }

  // Open the dock through the palette (no terminal shell chip exists yet).
  await page.keyboard.press("Meta+p");
  await page.getByTestId("palette-input").locator("input").fill("Terminal");
  await page.getByTestId("cmd-terminal.open").click();
  await page.getByTestId("terminal-panel").waitFor({ timeout: 10000 });

  const createdResponse = page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname === "/api/terminal");
  await page.getByTestId("terminal-new").click();
  const created = await createdResponse;
  const createdStatus = created.status();
  const createdBody = await created.json();
  assert.equal(createdStatus, 201, `New terminal creation: ${createdStatus} ${JSON.stringify(createdBody)} ${JSON.stringify(terminalResponses)}`);
  assert.ok(createdBody.id, "creation returned no id");
  const id = createdBody.id;
  await page.getByTestId(`terminal-resource-${id}`).waitFor({ timeout: 10000 });

  const findTerminal = async () => {
    for (let i = 0; i < 120; i++) {
      for (const frame of page.frames()) if (await frame.locator(".xterm-helper-textarea").count()) return frame;
      await page.waitForTimeout(100);
    }
    throw new Error("xterm WebView did not load");
  };
  const terminal = await findTerminal();
  await terminal.waitForFunction(() => document.activeElement?.classList.contains("xterm-helper-textarea"), null, { timeout: 10000 });
  await terminal.waitForFunction(() => writable, null, { timeout: 10000 });
  await page.waitForTimeout(400);

  // Run cat -v so any toggle byte that reaches the PTY becomes visible.
  await page.keyboard.type("cat -v");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(400);
  const beforeClose = await list();
  const before = beforeClose.find(t => t.id === id);
  assert.ok(before, "created terminal missing from list");
  await page.screenshot({ path: path.join(out, "nsh-01-created.png") });

  const rowsText = async frame => frame.evaluate(() => document.querySelector(".xterm-rows")?.textContent ?? "");
  const openPanel = () => page.getByTestId("terminal-panel").waitFor({ timeout: 10000 });
  const panelGone = async () => {
    await page.getByTestId("terminal-panel").waitFor({ state: "detached", timeout: 10000 });
  };

  // Ctrl+` from focused shell: closes the panel, must detach and leave the PTY running.
  await terminal.locator(".xterm-helper-textarea").focus();
  await page.keyboard.press("Control+Backquote");
  await panelGone();
  const closed = await resource(id);
  assert.equal(closed.status, "running", `panel close terminated the terminal: ${JSON.stringify(closed)}`);
  await page.screenshot({ path: path.join(out, "nsh-02-toggle-backtick-closed.png") });

  // Reopen with Ctrl+` from the page (no terminal focus); same resource, no new creation.
  await page.keyboard.press("Control+Backquote");
  await openPanel();
  await page.getByTestId(`terminal-resource-${id}`).waitFor({ timeout: 10000 });
  const reopenedFrame = await findTerminal();
  await reopenedFrame.waitForFunction(() => writable, null, { timeout: 10000 });
  await page.waitForTimeout(400);
  assert.equal(creates, 1, "reopening created another terminal");
  assert.equal((await list()).length, beforeClose.length, "reopening changed the terminal count");

  // Ctrl+~ from focused shell also closes (and must not reach cat -v).
  await reopenedFrame.locator(".xterm-helper-textarea").focus();
  await page.keyboard.press("Control+Shift+Backquote");
  await panelGone();
  assert.equal((await resource(id)).status, "running", "Ctrl+~ close terminated the terminal");
  await page.screenshot({ path: path.join(out, "nsh-03-toggle-tilde-closed.png") });

  // Reopen the same resource and type into cat -v; leaked toggle bytes would show ^@ or ^^.
  await page.keyboard.press("Control+Backquote");
  await openPanel();
  const finalFrame = await findTerminal();
  await finalFrame.waitForFunction(() => writable, null, { timeout: 10000 });
  await finalFrame.locator(".xterm-helper-textarea").focus();
  await page.keyboard.type("X");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(700);
  const finalText = await rowsText(finalFrame);
  assert(!finalText.includes("^@"), `Ctrl+backquote reached the PTY (^@): ${finalText.slice(-500)}`);
  assert(!finalText.includes("^^"), `Ctrl+tilde reached the PTY (^^): ${finalText.slice(-500)}`);
  assert(finalText.includes("X"), "typed marker did not reach the same terminal");
  await page.screenshot({ path: path.join(out, "nsh-04-reopened.png") });

  // Parity: the TUI-created resource, opened in NSH and captured at the same state.
  const tuiList = await list();
  const tuiResource = tuiTerminalId ? tuiList.find(t => t.id === tuiTerminalId) : null;
  if (tuiResource) {
    await page.getByTestId(`terminal-resource-${tuiTerminalId}`).click();
    await page.waitForTimeout(1200);
    const parityFrame = await findTerminal();
    const parityText = await rowsText(parityFrame);
    assert(parityText.includes("NEXUS_TUI_TOGGLE_PARITY"), `TUI parity marker missing in NSH: ${parityText.slice(-500)}`);
    await page.screenshot({ path: path.join(out, "nsh-05-parity-tui-terminals.png") });
  }

  const errors = terminalResponses.filter(r => r.status === 403);
  assert.equal(errors.length, 0, `terminal 403 responses: ${JSON.stringify(errors)}`);
  assert(terminalResponses.some(r => r.method === "POST" && r.status === 201), "no successful terminal creation observed");
  fs.writeFileSync(path.join(out, "nsh-network.json"), JSON.stringify(terminalResponses, null, 2) + "\n");
  fs.writeFileSync(path.join(out, "nsh-terminal.json"), JSON.stringify({
    id, created_status: createdStatus, creates,
    final_status: (await resource(id)).status,
    tui_terminal_id: tuiTerminalId || null,
    final_text_tail: finalText.slice(-800),
  }, null, 2) + "\n");
  console.log(JSON.stringify({ id, createdStatus, creates, toggle: "ok", parity: !!tuiResource }));
} finally {
  await browser.close();
}
