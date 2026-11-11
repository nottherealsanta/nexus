// Browser regression for WorktreeInfoFixture: package and serve it on localhost; no Core is used.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';

const directory = `/work/${'unbroken_component_'.repeat(60)}final-tail`;
const browser = await chromium.launch({ executablePath: process.env.BROWSER });
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 768 }, colorScheme: 'dark', permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await context.newPage();
  await page.goto(process.env.URL ?? 'http://127.0.0.1:49383');
  await page.getByTestId('open-worktree').click();
  const scroll = page.getByTestId('worktree-info-scroll');
  const copy = page.getByTestId('worktree-path-copy');
  await page.waitForTimeout(500);
  // Check a true pointer hit at the visible center, not programmatic activation or a corner.
  await copy.scrollIntoViewIfNeeded();
  await copy.click();
  await page.waitForFunction(expected => document.querySelector('[data-testid="toast-text"]')?.textContent?.includes(expected), 'Copied');
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), directory);
  // Scroll to the other end and use the visible main-checkout control too.
  await scroll.evaluate(el => { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll')); });
  const main = page.getByTestId('worktree-main-path-copy');
  await main.scrollIntoViewIfNeeded();
  await main.click();
  await page.waitForTimeout(200);
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '/work/demo');
  console.log(`PASS: center pointer copied the exact ${directory.length}-character path and the main checkout`);
} finally { await browser.close(); }
