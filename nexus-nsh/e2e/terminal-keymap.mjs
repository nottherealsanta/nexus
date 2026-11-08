// Run against `nsh dev TerminalFixture.tsx --target web --port 8899`.
import { chromium } from "playwright";
import assert from "node:assert/strict";
const browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
try {
  const page = await browser.newPage({viewport:{width:1200,height:800}});
  await page.goto(process.env.TERMINAL_FIXTURE_URL ?? "http://127.0.0.1:8899/");
  let terminal;
  for (let i = 0; i < 100 && !terminal; i++) {
    for (const frame of page.frames()) if (await frame.locator(".xterm-helper-textarea").count()) terminal = frame;
    if (!terminal) await page.waitForTimeout(100);
  }
  assert(terminal, "xterm WebView did not load");
  await terminal.evaluate(() => {
    window.captured = [];
    const original = window.nsh.postMessage;
    window.nsh.postMessage = data => { window.captured.push(JSON.parse(data)); original(data); };
    window.postMessage(JSON.stringify({type:"writable",value:true}), "*");
  });
  await terminal.locator(".xterm-helper-textarea").focus();
  await page.waitForTimeout(500);
  await page.keyboard.press("Control+c");
  await page.keyboard.press("Escape");
  await page.keyboard.press("Control+]");
  await page.waitForTimeout(100);
  const captured = await terminal.evaluate(() => window.captured);
  assert(captured.some(m => m.type === "input" && m.data === "\x03"));
  assert(captured.some(m => m.type === "input" && m.data === "\x1b"));
  assert(captured.some(m => m.type === "focus-list"));
  assert.equal(captured.filter(m => m.type === "input").length, 2);
  assert.equal(await page.getByTestId("fixture-shortcuts").innerText(), "0");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  assert.equal(await page.getByTestId("fixture-shortcuts").innerText(), "1");
  console.log("NSH global Ctrl+C/Esc bindings did not fire in xterm; Ctrl+] returned keys to NSH");
} finally { await browser.close(); }
