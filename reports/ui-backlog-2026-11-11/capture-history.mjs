import { chromium } from "../../nexus-nsh/node_modules/playwright/index.mjs";
import path from "node:path";
const out = path.dirname(new URL(import.meta.url).pathname);
const browser = await chromium.launch();
try {
  const p = await browser.newPage({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
  await p.goto("http://127.0.0.1:49502");
  await p.locator('[data-testid="app"]').waitFor({ timeout: 20000 });
  await p.waitForTimeout(1500);
  await p.locator('[data-testid^="session-ses_"]').first().click();
  await p.waitForTimeout(1500);
  await p.keyboard.press("Control+r");
  await p.locator('[data-testid="picker"]').waitFor({ timeout: 5000 });
  await p.waitForTimeout(500);
  await p.screenshot({ path: path.join(out, "nsh-prompt-history-1440.png") });
} finally { await browser.close(); }
