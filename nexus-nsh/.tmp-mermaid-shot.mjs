import { chromium } from "playwright";
import fs from "node:fs";
const S = process.env.S;
const OUT = process.env.OUT;
const ids = JSON.parse(fs.readFileSync(`${S}/ids.json`, "utf8"));
const browser = await chromium.launch();
for (const scheme of ["dark", "light"]) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1400 }, colorScheme: scheme, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => console.log("pageerror", e.message.slice(0, 200)));
  await p.goto("http://127.0.0.1:47933");
  await p.waitForSelector('[data-testid="header"]', { timeout: 20000 });
  await p.waitForTimeout(2200);
  await p.locator(`[data-testid="rail-project-${ids.projectA}"]`).click().catch(() => {});
  await p.waitForTimeout(600);
  await p.locator(`[data-testid="session-${ids.diagrams}"]`).click();
  await p.waitForTimeout(1800);
  await p.screenshot({ path: `${OUT}/nsh-1440x1400-${scheme}.png` });
  console.log("shot", scheme, await p.locator('[data-testid="mermaid-fence"]').count(), await p.locator('[data-testid="mermaid-diagram"]').count(), await p.locator('[data-testid="mermaid-text"]').count());
  await ctx.close();
}
await browser.close();
