import { chromium } from "../../nexus-nsh/node_modules/playwright/index.mjs";
import fs from "node:fs/promises";
import path from "node:path";

const home = process.argv[2];
if (!home?.startsWith("/tmp/nexus-21-23.")) throw new Error("Pass the isolated visual Core's scratch home");
const ids = JSON.parse(await fs.readFile(path.join(home, "ids.json"), "utf8"));
const out = path.dirname(new URL(import.meta.url).pathname);
const browser = await chromium.launch();
try {
  for (const [tag, width, height] of [["desktop", 1440, 900], ["phone", 390, 844]]) {
    const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: "dark", hasTouch: width < 500 });
    const p = await ctx.newPage();
    p.on("pageerror", (e) => console.log("page error:", e.message));
    await p.goto("http://127.0.0.1:48733");
    const session = p.locator(`[data-testid="session-${ids.parent}"]`);
    await p.locator('[data-testid="app"]').waitFor({ timeout: 15000 });
    if (width < 500) await p.locator('[data-testid="toggle-left"]').click();
    await session.waitFor({ timeout: 15000 });
    await session.click();
    await p.locator('[data-testid="session-status"]').waitFor();
    await p.waitForTimeout(600);
    await p.screenshot({ path: path.join(out, `nsh-${tag}-status.png`) });
    const input = p.locator('[data-testid="composer-input"]');
    await input.click();
    await p.keyboard.type("/btw Which port?");
    await p.keyboard.press("Enter");
    await p.locator('[data-testid="btw-modal"]').waitFor();
    await p.getByText("The Core listens on", { exact: false }).waitFor();
    await p.waitForTimeout(600);
    await p.screenshot({ path: path.join(out, `nsh-${tag}-btw.png`) });
    console.log(tag, (await p.locator('[data-testid="btw-progress"]').innerText()).slice(0, 800));
    await p.keyboard.press("Escape");
    await ctx.close();
  }
  for (const name of ["tui-status", "tui-btw"]) {
    const p = await browser.newPage({ viewport: { width: 1428, height: 900 } });
    await p.goto(`file://${path.join(out, `${name}.svg`)}`);
    await p.screenshot({ path: path.join(out, `${name}.png`) });
    await p.close();
  }
} finally {
  await browser.close();
}
