import { chromium } from "playwright";
import fs from "node:fs";
const S = fs.readFileSync("/tmp/nsh-scroll-dir","utf8").trim();
const ids = JSON.parse(fs.readFileSync(`${S}/ids.json`,"utf8"));
const URL = process.argv[2];
const browser = await chromium.launch();
const ctx = await browser.newContext({viewport:{width:1440,height:900}, colorScheme:"dark", deviceScaleFactor:1});
const p = await ctx.newPage();
p.on("pageerror", e => console.log("pageerror", e.message));
const t0=Date.now();
await p.goto(URL);
await p.waitForSelector('[data-testid="header"]',{timeout:30000});
console.log("ready ms", Date.now()-t0);
await p.waitForTimeout(1500);
const chip = p.locator('[data-testid="chip-all-projects"]'); if (await chip.count()) await chip.click();
await p.waitForTimeout(400);
await p.locator(`[data-testid="session-${ids.refactor}"]`).click();
await p.waitForTimeout(1500);
const sc = p.locator('[data-testid="timeline"] .nsh-scroll').first();
const box = await sc.boundingBox();
console.log("box", box);
await p.mouse.move(box.x + box.width/2, box.y + box.height/2);
async function sample(label, wait=600) {
  const out = await p.evaluate(async (wait) => {
    const el = document.querySelector('[data-testid="timeline"] .nsh-scroll');
    const t0 = performance.now(); const vals=[];
    return await new Promise(res => {
      function tick() { vals.push([Math.round(performance.now()-t0), el.scrollTop]); if (performance.now()-t0 < wait) requestAnimationFrame(tick); else res(vals); }
      requestAnimationFrame(tick);
    });
  }, wait);
  console.log(label, out.filter((_,i)=>i%3===0).map(([t,v])=>`${t}:${v}`).join(" "));
}
console.log("start", await sc.evaluate(e=>e.scrollTop));
await p.mouse.wheel(0, 100); await sample("wheel100");
await p.waitForTimeout(400); console.log("after", await sc.evaluate(e=>e.scrollTop));
await p.mouse.wheel(0, 100); await sample("wheel100b");
await p.waitForTimeout(400); await p.mouse.wheel(0, -200); await sample("wheel-200");
await p.waitForTimeout(400); console.log("final", await sc.evaluate(e=>e.scrollTop));
await browser.close();
