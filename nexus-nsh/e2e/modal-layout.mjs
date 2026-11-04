// Check modal geometry against a running, disposable visual host.
// node e2e/modal-layout.mjs --url http://127.0.0.1:4820 [--out /tmp/modal-shots] [--browser /path/to/chrome]
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';
const arg = (key) => { const i = process.argv.indexOf(`--${key}`); return i < 0 ? undefined : process.argv[i + 1]; };
const focusScenario = JSON.parse(fs.readFileSync(new URL("../../interface/scenarios/settings-focus.json", import.meta.url), "utf8"));
const url = arg('url');
assert.ok(url, 'Pass --url for a disposable visual host');
const out = arg('out');
if (out) fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: arg('browser') });
try {
  for (const [width, height, colorScheme] of [[1024, 768, 'dark'], [1440, 900, 'dark'], [672, 432, 'light']]) {
    const context = await browser.newContext({ viewport: { width, height }, colorScheme });
    try {
      const page = await context.newPage();
      await page.goto(url);
      await page.getByTestId('header').waitFor();
      await page.getByTestId('splash').waitFor({ state: 'hidden' });
      for (const [kind, key, wide] of [['palette', 'p', 600], ['settings', ',', 740]]) {
        await page.keyboard.press(`${process.platform === 'darwin' ? 'Meta' : 'Control'}+${key}`);
        const shell = page.getByTestId(`${kind}-shell`);
        await shell.waitFor();
        await page.waitForTimeout(350);
        const box = await shell.boundingBox();
        assert.ok(box, `${kind}: missing bounds`);
        assert.ok(Math.abs(box.x + box.width / 2 - width / 2) <= 1, `${kind}: horizontal centre`);
        assert.ok(Math.abs(box.y + box.height / 2 - height / 2) <= 1, `${kind}: vertical centre`);
        assert.equal(box.width, width < 820 ? width - 24 : wide, `${kind}: shared width class`);
        assert.ok(box.height <= Math.ceil(height * 0.7), `${kind}: height cap`);
        assert.equal(await shell.getByText('esc', { exact: true }).count(), 0, `${kind}: duplicate title hint`);
        const footer = await shell.getByText(/esc close/).boundingBox();
        assert.ok(footer && footer.y >= box.y && footer.y + footer.height <= box.y + box.height - 12, `${kind}: footer clipped`);
        if (out) await page.screenshot({ path: path.join(out, `${kind}-${width}x${height}-${colorScheme}.png`) });
        if (kind === 'settings') {
          await page.getByTestId('section-appearance').click();
          const row = page.getByTestId('setting-appearance.theme');
          for (let tab = 0; tab < 30 && !await page.getByTestId('settings-row-focus').count(); tab++) await page.keyboard.press('Tab');
          await page.getByTestId('settings-row-focus').waitFor();
          const selected = await row.locator('[role=radio][aria-selected=true]').getAttribute('aria-label');
          const arrow = (key) => `Arrow${key[0].toUpperCase()}${key.slice(1)}`;
          await page.keyboard.press(arrow(focusScenario.return_key));
          assert.equal(await page.getByTestId('section-appearance').evaluate((node) => node === document.activeElement), true, 'Left returns focus to the heading');
          assert.equal(await row.locator('[role=radio][aria-selected=true]').getAttribute('aria-label'), selected, 'Left keeps the setting value');
          await page.getByTestId('settings-section-focus').waitFor();
          await page.keyboard.press(arrow(focusScenario.next_section_key));
          assert.equal(await page.getByTestId(`section-${focusScenario.next_section}`).evaluate((node) => node === document.activeElement), true, 'Heading keys work after Left');
        }
        await page.keyboard.press('Escape');
        await shell.waitFor({ state: 'hidden' });
        console.log(`ok ${kind} ${width}x${height} ${colorScheme}`);
      }
    } finally { await context.close(); }
  }
} finally { await browser.close(); }
