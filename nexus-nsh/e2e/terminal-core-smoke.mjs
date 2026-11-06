// Requires a disposable authenticated Core, a selected project and the NSH host at NEXUS_NSH_URL.
import { chromium } from "playwright";
import assert from "node:assert/strict";
const url = process.env.NEXUS_NSH_URL;
if (!url) throw new Error("Set NEXUS_NSH_URL to an isolated NSH host");
const browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  await page.goto(url);
  await page.getByRole("button", {name:"Terminal", exact:true}).click({timeout:20000});
  const stream = page.waitForResponse(r => r.url().includes("/api/terminal/") && r.url().includes("/stream?") && r.status() === 200);
  await page.getByRole("button", {name:"New", exact:true}).click();
  await stream;
  await page.getByRole("button", {name:"Take control"}).click({timeout:10000});
  await page.getByRole("button", {name:"Release control"}).waitFor();
  await page.getByRole("button", {name:"Rename", exact:true}).click();
  await page.getByLabel("Terminal name").locator("input").fill("Terminal smoke");
  await page.getByRole("button", {name:"Rename", exact:true}).last().click();
  await page.getByText("Terminal smoke", {exact:true}).waitFor();
  await page.getByRole("button", {name:"Release control"}).click();
  await page.getByRole("button", {name:"Take control"}).click();
  await page.getByRole("button", {name:"Release control"}).waitFor();
  let screen;
  for (let i = 0; i < 100 && !screen; i++) {
    for (const frame of page.frames()) if (await frame.locator(".xterm-helper-textarea").count()) screen = frame;
    if (!screen) await page.waitForTimeout(100);
  }
  assert(screen, "xterm WebView did not load");
  await screen.locator(".xterm-helper-textarea").focus();
  await page.keyboard.type("sleep 30");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(500);
  await page.keyboard.press("Control+c");
  await page.waitForTimeout(200);
  await page.keyboard.type("printf NEXUS_TERMINAL_SMOKE");
  await page.keyboard.press("Enter");
  await screen.waitForFunction(() => (document.querySelector('.xterm-rows')?.textContent?.match(/NEXUS_TERMINAL_SMOKE/g) ?? []).length >= 2, null, {timeout:10000});
  await page.keyboard.press("Control+]");
  await page.getByRole("button", {name:"Close", exact:true}).click();
  await page.getByText("Close running terminal?").waitFor();
  await page.getByRole("button", {name:"Close terminal", exact:true}).click();
  await page.getByText(/closed · No controller/).last().waitFor({timeout:10000});
  console.log("real Core: create, attach, control, shell input/output and confirmed close passed");
} finally { await browser.close(); }
