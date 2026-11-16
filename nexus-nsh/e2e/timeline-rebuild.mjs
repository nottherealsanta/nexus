// Browser-coordinate regression for a virtual list whose distant rows update together.
// Run against `nsh dev ScrollRebuildFixture.tsx --target web` with --url.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const i = process.argv.indexOf('--url');
assert.ok(i >= 0 && process.argv[i + 1], 'Pass --url for the fixture development host');
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1024, height: 768 } });
  await page.goto(process.argv[i + 1]);
  const list = page.getByTestId('rebuild-list');
  await list.evaluate(el => { el.scrollTop = 2600; });
  const marker = page.getByText('Row 30 generation 0', { exact: true });
  await marker.waitFor({ state: 'attached' });
  await page.waitForTimeout(500);
  const before = await marker.boundingBox();
  assert.ok(before && before.y >= 0 && before.y < 768, 'marker must start in the viewport');
  await page.getByTestId('rebuild').click();
  await page.waitForTimeout(500);
  const after = await marker.boundingBox();
  assert.ok(after, 'the visible row must remain mounted');
  console.log(JSON.stringify({ before: before.y, after: after.y, delta: after.y - before.y }));
  assert.ok(Math.abs(after.y - before.y) <= 2, 'updating distant rows must preserve the visible row');
  await list.evaluate(el => { el.scrollTop = 0; });
  await page.getByText('Row 0 generation 1', { exact: true }).waitFor();
} finally { await browser.close(); }
