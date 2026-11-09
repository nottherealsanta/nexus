// Requires a disposable authenticated Core, a selected project and the NSH host at NEXUS_NSH_URL.
import { chromium, devices } from "playwright";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const scenario = JSON.parse(await readFile(new URL("../../interface/scenarios/terminal-dock.json", import.meta.url), "utf8"));
const url = process.env.NEXUS_NSH_URL;
if (!url) throw new Error("Set NEXUS_NSH_URL to an isolated NSH host");
const browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  await page.goto(url);
  if (process.env.NEXUS_TERMINAL_SESSION) await page.getByTestId(`session-${process.env.NEXUS_TERMINAL_SESSION}`).click();
  await page.getByTestId("composer-panel").waitFor({timeout:20000});
  if (await page.getByTestId("location-shell").count()) await page.getByTestId("location-shell").click();
  else {
    await page.keyboard.press("Meta+p");
    await page.getByTestId("palette-input").locator("input").fill("Terminal");
    await page.getByTestId("cmd-terminal.open").click();
  }
  const streams = new Set(), acquired = new Set(), terminalResponses = [];
  page.on("response", r => {
    if (r.url().includes("/api/terminal/")) terminalResponses.push([r.status(), r.url(), r.request().postData()]);
    const match = r.url().match(/\/api\/terminal\/([^/]+)\/(stream|control)(?:\?|$)/);
    if (!match || r.status() !== 200) return;
    if (match[2] === "stream") streams.add(match[1]);
    if (match[2] === "control" && r.request().postDataJSON()?.action === "acquire") acquired.add(match[1]);
  });
  const created = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/api/terminal") && r.status() === 201);
  await page.getByRole("button", {name:"New terminal", exact:true}).click();
  const terminal = await (await created).json();
  const id = terminal.id;
  await page.waitForTimeout(100);
  for (let i = 0; i < 100 && (!streams.has(id) || !acquired.has(id)); i++) await page.waitForTimeout(100);
  assert(streams.has(id) && acquired.has(id), `new terminal did not attach and acquire control: ${JSON.stringify(terminalResponses)}`);
  await page.getByRole("button", {name:"Release control"}).waitFor({timeout:10000});
  let previousX = -1;
  for (const action of scenario.actions) {
    const control = `terminal-${action.id === "hide" ? "panel-close" : action.id}`;
    const label = action.label_controlled ?? action.label;
    const icon = action.icon_controlled ?? action.icon;
    const button = page.getByTestId(control);
    assert.equal(await button.getAttribute("aria-label"), label);
    assert.equal(await button.getByTestId(`terminal-icon-${icon}`).count(), 1);
    const bounds = await button.boundingBox();
    assert(bounds && bounds.x > previousX, `toolbar action ${action.id} is out of order`);
    previousX = bounds.x;
    await page.getByTestId(control).hover();
    assert.equal(await page.getByTestId(`${control}-tooltip`).innerText(), label);
  }
  const formatTip = template => template.replace("{name}", terminal.name).replace("{status}", terminal.status);
  await page.getByTestId(`terminal-resource-${id}`).hover();
  assert.equal(await page.getByTestId(`terminal-resource-${id}-tooltip`).innerText(), formatTip(scenario.tab_tooltip));
  await page.getByTestId(`terminal-tab-close-${id}`).hover();
  assert.equal(await page.getByTestId(`terminal-tab-close-${id}-tooltip`).innerText(), formatTip(scenario.close_tooltip));
  let screen;
  for (let i = 0; i < 100 && !screen; i++) {
    for (const frame of page.frames()) if (await frame.locator(".xterm-helper-textarea").count()) screen = frame;
    if (!screen) await page.waitForTimeout(100);
  }
  assert(screen, "xterm WebView did not load");
  await screen.waitForFunction(() => document.activeElement?.classList.contains("xterm-helper-textarea"), null, {timeout:10000});
  await screen.waitForFunction(() => writable, null, {timeout:10000});
  await page.keyboard.type("printf '%s%s' NEXUS_AUTO_ CONTROL");
  await page.keyboard.press("Enter");
  await screen.waitForFunction(() => document.querySelector('.xterm-rows')?.textContent?.includes('NEXUS_AUTO_CONTROL'), null, {timeout:15000});
  await page.getByRole("button", {name:"Rename terminal", exact:true}).click();
  const name = `Terminal smoke ${Date.now()}`;
  await page.getByLabel("Terminal name").locator("input").fill(name);
  const renamed = page.waitForResponse(r => r.request().method() === "PATCH" && r.url().includes("/api/terminal/"));
  await page.getByRole("button", {name:"[Rename]", exact:true}).click();
  assert.equal((await (await renamed).json()).name, name);
  await page.getByRole("button", {name:"Release control"}).click();
  await page.getByRole("button", {name:"Take control"}).hover();
  const controlAction = scenario.actions.find(action => action.id === "control");
  assert.equal(await page.getByTestId("terminal-control-tooltip").innerText(), controlAction.label);
  assert.equal(await page.getByTestId("terminal-control").getByTestId(`terminal-icon-${controlAction.icon}`).count(), 1);
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
  if (process.env.NEXUS_TERMINAL_SCREENSHOT) await page.screenshot({path:process.env.NEXUS_TERMINAL_SCREENSHOT});
  let evidenceSends = 0;
  page.on("request", request => { if (request.method() === "POST" && /\/api\/session\/[^/]+\/(prompt|input)$/.test(new URL(request.url()).pathname)) evidenceSends++; });
  await screen.evaluate(() => {
    const marker = "NEXUS_TERMINAL_SMOKE";
    for (let row = term.buffer.active.length - 1; row >= 0; row--) {
      const column = term.buffer.active.getLine(row)?.translateToString(true).indexOf(marker) ?? -1;
      if (column >= 0) { term.select(column, row, marker.length); return; }
    }
    throw new Error("terminal marker missing from selectable buffer");
  });
  await page.getByTestId("terminal-attach").click();
  await page.getByTestId("confirm-dialog").waitFor();
  const evidencePreview = await page.getByTestId("confirm-dialog").innerText();
  assert(evidencePreview.replace(/\s/g, "").includes(id), evidencePreview);
  await page.getByTestId("confirm-body-scroll").hover();
  await page.mouse.wheel(0, 1500);
  await page.waitForTimeout(200);
  const scrolledPreview = await page.getByTestId("confirm-dialog").innerText();
  assert(scrolledPreview.includes("NEXUS_TERMINAL_SMOKE"), scrolledPreview);
  if (process.env.NEXUS_TERMINAL_EVIDENCE_SCREENSHOT) await page.screenshot({path:process.env.NEXUS_TERMINAL_EVIDENCE_SCREENSHOT});
  await page.getByTestId("confirm-cancel").click();
  await page.getByTestId("terminal-attach").click();
  await page.getByTestId("confirm-dialog").waitFor();
  await page.getByTestId("confirm-ok").click();
  const composer = page.getByTestId("composer-input").locator("textarea");
  assert((await composer.inputValue()).includes("Untrusted terminal output"));
  assert((await composer.inputValue()).includes("NEXUS_TERMINAL_SMOKE"));
  assert.equal(evidenceSends, 0, "evidence must edit the draft, never send automatically");
  await page.getByTestId("terminal-close").click();
  await page.getByText("Close running terminal?").waitFor();
  const closed = page.waitForResponse(r => r.request().method() === "DELETE" && r.url().includes("/api/terminal/"));
  await page.getByTestId("confirm-dialog").getByRole("button", {name:"[Close terminal]", exact:true}).click();
  assert.equal((await (await closed).json()).status, "closed");
  const mobile = await browser.newContext({...devices["iPhone 13"]});
  const phone = await mobile.newPage();
  await phone.goto(url);
  await phone.waitForTimeout(2000);
  assert.equal(await phone.getByRole("button", {name:"Terminal", exact:true}).count(), 0);
  await mobile.close();
  console.log("real Core: create, rename, attach, control, Ctrl+C, output, confirmed close and mobile exclusion passed");
} finally { await browser.close(); }
