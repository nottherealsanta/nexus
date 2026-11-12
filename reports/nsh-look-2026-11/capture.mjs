// usage: node capture.mjs <scratch home> <port> <label>   (label: before|after); writes nsh-<w>-<scheme>-<surface>-<label>.png
import { chromium } from "../../nexus-nsh/node_modules/playwright/index.mjs";
import fs from "node:fs/promises";
import path from "node:path";

const [home, port, label] = process.argv.slice(2);
if (!home?.startsWith("/tmp/nexus-look.")) throw new Error("Pass the scratch home");
const ids = JSON.parse(await fs.readFile(path.join(home, "ids.json"), "utf8"));
const out = path.dirname(new URL(import.meta.url).pathname);
const browser = await chromium.launch();
try {
  for (const width of [1024, 1440]) for (const scheme of ["dark", "light"]) {
    const ctx = await browser.newContext({ viewport: { width, height: width === 1024 ? 768 : 900 }, colorScheme: scheme });
    const p = await ctx.newPage();
    p.on("pageerror", (e) => console.log("page error:", e.message));
    const shot = (name) => p.screenshot({ path: path.join(out, `nsh-${width}-${scheme}-${name}-${label}.png`) });
    await p.goto(`http://127.0.0.1:${port}`);
    await p.locator('[data-testid="app"]').waitFor({ timeout: 15000 });
    for (const [name, key] of [["tools", "rich"], ["text", "text"], ["question", "question"], ["approval", "approval"]]) {
      const row = p.locator(`[data-testid="session-${ids[key]}"]`);
      await row.waitFor({ timeout: 15000 });
      await row.click();
      await p.waitForTimeout(900);
      await shot(name);
    }
    await p.keyboard.press("Meta+p");
    await p.locator('[data-testid="palette"]').waitFor({ timeout: 5000 }).catch(() => console.log("no palette"));
    await p.waitForTimeout(300);
    await shot("palette");
    await p.keyboard.press("Escape");
    await p.keyboard.press("Meta+,");
    await p.waitForTimeout(800);
    await shot("settings");
    await ctx.close();
  }
} finally {
  await browser.close();
}
