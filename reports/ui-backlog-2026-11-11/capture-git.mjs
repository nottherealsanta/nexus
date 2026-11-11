import { chromium } from "../../nexus-nsh/node_modules/playwright/index.mjs";
import path from "node:path";
const out = path.dirname(new URL(import.meta.url).pathname);
const browser = await chromium.launch();
try {
  const p = await browser.newPage({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
  p.on("pageerror", (e) => console.log("page error:", e.message));
  await p.goto("http://127.0.0.1:49502");
  await p.locator('[data-testid="app"]').waitFor({ timeout: 20000 });
  await p.waitForTimeout(1500);
  const tab = p.locator('[data-testid="tab-git"]');
  console.log("git tab count", await tab.count());
  if (await tab.count()) await tab.first().click();
  await p.waitForTimeout(1200);
  await p.screenshot({ path: path.join(out, "nsh-git-tab-1440.png") });
  const t = await p.locator('[data-testid]').evaluateAll((els) => els.map((e) => e.dataset.testid).filter((x) => /tab|git/.test(x)).slice(0, 30));
  console.log(t.join(","));
} finally { await browser.close(); }
