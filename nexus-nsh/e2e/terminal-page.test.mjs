import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { readFile } from 'node:fs/promises';

const root = new URL('../', import.meta.url);
test('terminal page captures keys, copies only the selection, and blocks OSC 52', async () => {
  const pageSource = await readFile(new URL('src/terminal-page.html', root), 'utf8');
  let html = pageSource;
  for (const [marker, file] of [['/* XTERM_CSS */','node_modules/@xterm/xterm/css/xterm.css'],['/* XTERM_JS */','node_modules/@xterm/xterm/lib/xterm.js'],['/* FIT_JS */','node_modules/@xterm/addon-fit/lib/addon-fit.js']]) {
    html = html.replace(marker, await readFile(new URL(file, root), 'utf8'));
  }
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const page = await browser.newPage();
    await page.exposeFunction('capture', data => messages.push(JSON.parse(data)));
    const messages = [];
    await page.addInitScript(() => { window.nsh = { postMessage: data => window.capture(data) }; });
    await page.goto('about:blank');
    await page.evaluate(() => { window.nsh = { postMessage: data => window.capture(data) }; });
    await page.setContent(html);
    await page.waitForFunction(() => document.querySelector('.xterm-helper-textarea'));
    await page.evaluate(() => window.postMessage(JSON.stringify({type:'writable',value:true}), '*'));
    await page.locator('.xterm-helper-textarea').focus();
    await page.keyboard.press('Control+c');
    await page.keyboard.press('Control+]');
    await page.waitForTimeout(100);
    assert(messages.some(m => m.type === 'input' && m.data === '\x03'));
    assert(messages.some(m => m.type === 'focus-list'));
    assert.equal(messages.filter(m => m.type === 'input').length, 1);
    await page.evaluate(() => window.postMessage(JSON.stringify({type:'write', data:btoa('\x1b]52;c;SGk=\x07')}), '*'));
    await page.waitForTimeout(50);
    assert(!messages.some(m => m.type === 'selection'));
  } finally { await browser.close(); }
});
