import { chromium } from "playwright";
const browser = await chromium.launch();
const p = await browser.newPage({ viewport: { width: 1430, height: 2161 } });
await p.goto("file:///tmp/mermaid-shots-out/tui-mermaid.svg");
await p.waitForTimeout(500);
await p.screenshot({ path: "/tmp/mermaid-shots-out/tui-mermaid.png" });
await browser.close();
