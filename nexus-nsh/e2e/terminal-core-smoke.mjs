// Requires a disposable authenticated Core, a selected project and the NSH host at NEXUS_NSH_URL.
import { chromium, devices } from "playwright";
import assert from "node:assert/strict";
const url = process.env.NEXUS_NSH_URL;
if (!url) throw new Error("Set NEXUS_NSH_URL to an isolated NSH host");
const browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  await page.goto(url);
  await page.getByTestId("location-shell").click({timeout:20000});
  const stream = page.waitForResponse(r => r.url().includes("/api/terminal/") && r.url().includes("/stream?") && r.status() === 200);
  await page.getByRole("button", {name:"New terminal", exact:true}).click();
  await stream;
  await page.getByRole("button", {name:"Release control"}).waitFor({timeout:10000});
  let screen;
  for (let i = 0; i < 100 && !screen; i++) {
    for (const frame of page.frames()) if (await frame.locator(".xterm-helper-textarea").count()) screen = frame;
    if (!screen) await page.waitForTimeout(100);
  }
  assert(screen, "xterm WebView did not load");
  await screen.waitForFunction(() => document.activeElement?.classList.contains("xterm-helper-textarea"), null, {timeout:10000});
  await page.keyboard.type("printf '%s%s' NEXUS_AUTO_ CONTROL");
  await page.keyboard.press("Enter");
  await screen.waitForFunction(() => document.querySelector('.xterm-rows')?.textContent?.includes('NEXUS_AUTO_CONTROL'), null, {timeout:15000});
  await page.getByRole("button", {name:"Rename", exact:true}).click();
  const name = `Terminal smoke ${Date.now()}`;
  await page.getByLabel("Terminal name").locator("input").fill(name);
  const renamed = page.waitForResponse(r => r.request().method() === "PATCH" && r.url().includes("/api/terminal/"));
  await page.getByRole("button", {name:"Rename", exact:true}).last().click();
  assert.equal((await (await renamed).json()).name, name);
  await page.getByRole("button", {name:"Release control"}).click();
  await page.getByRole("button", {name:"Take control"}).click();
  await page.getByRole("button", {name:"Release control"}).waitFor();
  await screen.locator(".xterm-helper-textarea").focus();
  await page.keyboard.type("sleep 30");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(500);
  await page.keyboard.press("Control+c");
  await page.waitForTimeout(200);
  await page.keyboard.type("seq 1 500; printf '%s%s' NEXUS_TERMINAL_ SMOKE");
  await page.keyboard.press("Enter");
  await screen.waitForFunction(() => document.querySelector('.xterm-rows')?.textContent?.includes('NEXUS_TERMINAL_SMOKE'), null, {timeout:15000});
  await page.keyboard.press("Control+]");
  await page.getByRole("button", {name:"Close", exact:true}).click();
  await page.getByText("Close running terminal?").waitFor();
  const closed = page.waitForResponse(r => r.request().method() === "DELETE" && r.url().includes("/api/terminal/"));
  await page.getByRole("button", {name:"Close terminal", exact:true}).click();
  assert.equal((await (await closed).json()).status, "closed");
  const mobile = await browser.newContext({...devices["iPhone 13"]});
  const phone = await mobile.newPage();
  await phone.goto(url);
  await phone.waitForTimeout(2000);
  assert.equal(await phone.getByRole("button", {name:"Terminal", exact:true}).count(), 0);
  await mobile.close();
  console.log("real Core: create, rename, attach, control, Ctrl+C, output, confirmed close and mobile exclusion passed");
} finally { await browser.close(); }
