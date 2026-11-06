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
    await page.evaluate(() => { window.nsh = { postMessage: data => window.capture(data) }; window.clipWrites = []; Object.defineProperty(navigator, 'clipboard', {value:{writeText: text => { window.clipWrites.push(text); return Promise.resolve(); }}, configurable:true}); });
    await page.setContent(html);
    await page.waitForFunction(() => document.querySelector('.xterm-helper-textarea'));
    await page.evaluate(() => { window.postMessage(JSON.stringify({type:'writable',value:true}), '*'); window.postMessage(JSON.stringify({type:'theme',background:'#f7f7f5',foreground:'#1f2124'}), '*'); });
    await page.waitForFunction(() => term.options.theme.background === '#f7f7f5');
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
    assert.deepEqual(await page.evaluate(() => window.clipWrites), []);
    await page.evaluate(() => term.write('\x1b[?2004h', () => term.paste('paste')));
    await page.waitForTimeout(50);
    assert(messages.some(m => m.type === 'input' && m.data === '\x1b[200~paste\x1b[201~'));
    await page.evaluate(() => term.write('\x1b[?1049h界', () => window.doneWriting = true));
    await page.waitForFunction(() => window.doneWriting);
    assert.equal(await page.evaluate(() => term.buffer.active.type), 'alternate');
    assert.equal(await page.evaluate(() => term.buffer.active.getLine(0).getCell(0).getWidth()), 2);
    await page.evaluate(() => {
      for (const m of [{type:'write', id:1, data:btoa('OLD')}, {type:'reset'}, {type:'write', id:2, data:btoa('NEW')}]) window.postMessage(JSON.stringify(m), '*');
    });
    for (let i = 0; i < 20 && !messages.some(m => m.type === 'written' && m.id === 2); i++) await page.waitForTimeout(25);
    assert(messages.some(m => m.type === 'written' && m.id === 2));
    const content = await page.evaluate(() => Array.from({length:term.rows}, (_, i) => term.buffer.active.getLine(i)?.translateToString(true) ?? '').join('\n'));
    assert(content.includes('NEW'));
    assert(!content.includes('OLD'));
  } finally { await browser.close(); }
});
