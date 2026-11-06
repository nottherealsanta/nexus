// Full-app viewport regression against an isolated fake Core. Run with --url pointing
// to `nsh dev App.tsx --target web`; no daemon or real Nexus state is used.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const arg = (name) => { const i = process.argv.indexOf(`--${name}`); return i < 0 ? undefined : process.argv[i + 1]; };
const url = arg('url');
const more = process.argv.includes('--more');
const move = process.argv.includes('--move');
assert.ok(url, 'Pass --url for the NSH web development host');
const session = { id: 'ses_1', project_id: 'prj_1', directory: '/work/demo', title: 'Long session', model: '', permission: 'ask', created: 1, updated: 2, effort: null, agent: 'build', parent_id: null, archived: 0, revision: 1, disabled_tools: '[]' };
const messages = Array.from({ length: 60 }, (_, i) => ({ id: `m${i}`, session_id: 'ses_1', seq: i + 1, role: i % 2 ? 'assistant' : 'user', parts: [{ type: 'text', text: `Message ${i}\n\nSome text to give the row a height.` }], created: i + 1 }));
const snapshot = { watermark: { generation: 'log_t', head: 0, floor: 0, head_cursor: 'log_t:0' }, projects: [{ id: 'prj_1', path: '/work/demo', name: 'demo', created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null };
let release, olderRequested;
const olderReady = new Promise(resolve => { olderRequested = resolve; });
const olderGate = new Promise(resolve => { release = resolve; });
const browser = await chromium.launch({ executablePath: arg('browser') });
try {
  const page = await browser.newPage({ viewport: { width: 1024, height: 768 } });
  await page.route('**/*', async route => {
    const u = new URL(route.request().url());
    if (u.pathname === '/__nexus/host') return route.fulfill({ json: { host: 'test', canStart: false } });
    if (!u.pathname.startsWith('/api/')) return route.continue();
    if (u.pathname === '/api/event') return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n' });
    let body = {};
    if (u.pathname === '/api/info') body = { name: 'nexus', version: '0.10.0.dev0', pid: 1 };
    else if (u.pathname === '/api/snapshot') body = snapshot;
    else if (u.pathname === '/api/provider') body = [];
    else if (u.pathname === '/api/model/last') body = { model: 'scripted/m' };
    else if (u.pathname === '/api/model') body = { source: 't', models: [], model: 'scripted/m' };
    else if (u.pathname === '/api/agent') body = { agents: [], problems: [] };
    else if (u.pathname === '/api/skill') body = { skills: [], problems: [] };
    else if (u.pathname === '/api/mcp') body = { servers: [] };
    else if (u.pathname === '/api/kernel' || u.pathname === '/api/bash') body = [];
    else if (u.pathname === '/api/session/ses_1/message') {
      if (u.searchParams.has('before')) { olderRequested(); await olderGate; body = { messages: messages.slice(0, 20), more }; }
      else body = { messages: messages.slice(20), more: true };
    } else if (u.pathname.endsWith('/turn')) body = { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } };
    else if (u.pathname.endsWith('/snapshot')) body = null;
    else if (u.pathname.endsWith('/worktrees')) body = { worktrees: [] };
    else if (u.pathname === '/api/session/ses_1') body = session;
    return route.fulfill({ json: body });
  });
  await page.goto(url);
  await page.getByTestId('session-ses_1').click();
  await page.getByTestId('timeline').waitFor();
  await page.waitForTimeout(1000);
  const scroller = page.getByTestId('timeline').locator('.nsh-scroll');
  await scroller.evaluate(el => el.scrollTo(0, 0));
  const older = page.getByTestId('load-older');
  await older.waitFor({ state: 'attached' });
  await older.scrollIntoViewIfNeeded();
  await older.click();
  await olderReady;
  if (move) {
    await scroller.evaluate(el => el.scrollBy(0, 300));
    await page.waitForTimeout(300);
  }
  const marker = page.getByText(move ? 'Message 22' : 'Message 20', { exact: false }).first();
  await marker.waitFor({ state: 'visible' });
  await page.waitForTimeout(300);
  const before = await marker.boundingBox();
  const beforeScroll = await scroller.evaluate(el => el.scrollTop);
  const response = page.waitForResponse(r => r.url().includes('/message?') && r.url().includes('before=21'));
  release();
  await response;
  await page.waitForTimeout(1000);
  const after = await marker.count() ? await marker.boundingBox() : null;
  const afterScroll = await scroller.evaluate(el => el.scrollTop);
  console.log(JSON.stringify({ before, after, beforeScroll, afterScroll, delta: after && before ? after.y - before.y : null }));
  assert.ok(after && before, 'same message must remain rendered');
  assert.ok(Math.abs(after.y - before.y) <= 2, 'loading older history must preserve the visible message position');
} finally { await browser.close(); }
