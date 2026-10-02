/** Production frontend → real UOS proxy → controlled HTTP upstreams.
 * Only backend responses and native OS APIs are outside this browser seam.
 * Requires npm run build; uses no real accounts, database or installed desktop data.
 */
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { startProxy } from '../../../desktop-uos/src/proxy.mjs';
import { BridgeSync, SessionEpoch } from '../../../desktop-uos/src/session.mjs';
import { FakeLocalServer } from '../../../desktop-uos/test/fixtures/desktop.mjs';
const { chromium } = await import('playwright');
const output = resolve('node_modules/.tmp/desktop-proxy-browser');
await mkdir(output, { recursive: true });
const requests = [];
function upstream(kind) {
  const server = http.createServer((req, res) => {
    const path = new URL(req.url, 'http://upstream.test').pathname;
    requests.push({ kind, path, cookie: req.headers.cookie, bridge: req.headers['x-desktop-bridge'] });
    let data = {};
    if (path.endsWith('/auth/session/check')) data = { user_id: 'e2e-user', username: 'e2e', display_name: 'E2E' };
    else if (path.endsWith('/meta/edition')) data = { edition: 'ee', features: {}, license: {} };
    else if (path.endsWith('/catalog')) data = { skills: [], mcp: [], agents: [], kb: [] };
    else if (path.endsWith('/sync-status')) data = { ready: true, partial: false };
    else if (path === '/api/v1/projects') data = { items: [{ project_id: kind + '-project', name: 'E2E ' + kind + ' project', kind: kind === 'local' ? 'local' : 'personal', permission: 'admin' }], pagination: { total_items: 1 } };
    else if (path === '/api/v1/automations' || path === '/api/v1/automations/notifications/list') data = [];
    else if (path === '/api/v1/chats/e2e-existing-chat') data = { chat_id: 'e2e-existing-chat', title: 'E2E existing chat', metadata: {}, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z' };
    else if (/chats|tasks|notifications|models|agents|installed|contributions|automations|teams|folders|skills/.test(path)) data = { items: [], models: [], count: 0 };
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ code: 10000, data }));
  });
  return server;
}
const cloud = upstream('cloud'), local = upstream('local');
let browser, proxy, diagnosticPage;
try {
  for (const server of [cloud, local]) await new Promise((done, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', done); });
  const localServer = new FakeLocalServer();
  const bridge = new BridgeSync(), epoch = new SessionEpoch();
  const state = {
    http: { agentFor: () => false }, serverBase: 'http://127.0.0.1:' + cloud.address().port,
    cloudServerBase: 'http://127.0.0.1:' + cloud.address().port,
    localBase: 'http://127.0.0.1:' + local.address().port,
    token: 'synthetic-desktop-session', cookieName: 'session', hybridLocal: true,
    bridgeSecret: 'synthetic-bridge', bridgeUser: 'eyJ1c2VyX2lkIjoiZTJlLXVzZXIifQ==',
    provisionMode: 'dual', activeLocal: false, brandName: 'HugAgentOS', localSupported: true,
    continueUrl: async () => '/',
  };
  proxy = await startProxy({ webDir: resolve('dist'), localServer, bridgeSync: bridge,
    getState: async () => ({ ...state, bridge: bridge.snapshot(), sessionActive: epoch.isActive() }) });
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  await context.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
  const errors = [], responses = [];
  const page = await context.newPage();
  diagnosticPage = page;
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', r => responses.push({ path: new URL(r.url()).pathname, status: r.status() }));
  const blockedProjects = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/projects' && r.status() === 503);
  await page.goto(proxy.origin);
  await page.getByText('能力同步中', { exact: true }).waitFor();
  await page.waitForFunction(() => !!window.__HG_DESKTOP__);
  assert.equal(await page.locator('html').getAttribute('data-desktop-platform'), 'uos');
  await blockedProjects;
  // The real proxy blocks local requests before bridge readiness, not the upstream fixture.
  assert.ok(responses.some(r => r.path === '/api/v1/projects' && r.status === 503), 'local requests receive readiness 503');
  assert.equal(requests.some(r => r.kind === 'local'), false, 'no credentials reach an unready local execution plane');
  localServer.becomeReady();
  bridge.patch({ identity_ready: true, capabilities_ready: true, models_ready: true });
  await page.getByText('E2E local project', { exact: true }).waitFor({ timeout: 15000 });
  await page.getByText('E2E cloud project', { exact: true }).waitFor();
  assert.ok(requests.some(r => r.kind === 'cloud' && r.cookie === 'session=synthetic-desktop-session'));
  assert.ok(requests.some(r => r.kind === 'local' && r.bridge === 'synthetic-bridge' && !r.cookie));
  assert.ok(requests.filter(r => r.kind === 'cloud').every(r => !r.bridge), 'local bridge never reaches cloud');
  console.log('PASS: built UI, real proxy authentication, readiness 503 → real SSE → local/cloud projects in sidebar');

  for (const theme of ['light', 'dark']) {
    await page.evaluate(mode => localStorage.setItem('hugagent_theme_mode', mode), theme);
    await page.reload();
    await page.getByText('E2E local project', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.style.colorScheme), theme);
    assert.equal((await page.locator('.jx-moduleRail').boundingBox()).y, 0);
    assert.equal(await page.locator('#hugagent-titlebar, #hugagent-mac-titlebar').count(), 0);
    await page.screenshot({ path: resolve(output, 'uos-production-' + theme + '.png'), fullPage: true });
  }
  await page.goto(proxy.origin + '/c/e2e-existing-chat');
  await page.getByRole('button', { name: '新建对话', exact: true }).waitFor();
  assert.equal(new URL(page.url()).pathname, '/c/e2e-existing-chat');
  await page.getByRole('button', { name: '新建对话', exact: true }).click();
  await page.waitForURL(proxy.origin + '/');
  await page.setViewportSize({ width: 760, height: 800 });
  await page.waitForTimeout(200);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'narrow workspace has no horizontal overflow');
  await page.screenshot({ path: resolve(output, 'uos-production-narrow.png'), fullPage: true });

  const second = await context.newPage();
  second.on('pageerror', e => errors.push(e.message));
  await second.goto(proxy.origin);
  await second.getByText('E2E local project', { exact: true }).waitFor();
  assert.equal(await second.evaluate(() => window.__HG_DESKTOP__.provision_mode), 'dual');
  const quick = await context.newPage();
  quick.on('pageerror', e => errors.push(e.message));
  await quick.setViewportSize({ width: 560, height: 680 });
  await quick.goto(proxy.origin + '/?quickask=1');
  await quick.waitForFunction(() => document.querySelector('.jx-content'));
  assert.equal(await quick.locator('html').getAttribute('data-desktop-platform'), null);
  assert.equal(await quick.locator('html').evaluate(el => el.classList.contains('quickask')), true);
  assert.equal(await quick.locator('.jx-sider').isVisible(), false, 'quick ask hides sidebar');
  await quick.screenshot({ path: resolve(output, 'uos-production-quickask.png'), fullPage: true });
  console.log('PASS: light/dark production UI, hidden web menu, narrow layout, new chat, second page and compact quick ask');

  // HTTP Origin guard, then browser requests after shell-side session invalidation.
  const before = requests.length;
  const rejected = await fetch(proxy.origin + '/api/v1/projects', { headers: { origin: 'https://foreign.example' } });
  assert.equal(rejected.status, 403);
  assert.equal(requests.length, before);
  epoch.advance(); bridge.reset(); state.token = null;
  for (const p of [page, second, quick]) {
    assert.equal(await p.evaluate(async () => (await fetch('/api/v1/projects')).status), 409);
  }
  assert.equal(requests.length, before, 'invalidated session makes no upstream requests');
  assert.deepEqual(errors, [], 'no production UI runtime errors');
  console.log('PASS: foreign origin rejected, shared session invalidation blocks all three pages, no browser runtime errors');
} catch (error) {
  console.error('Upstream paths:', requests.map(r => r.kind + ':' + r.path));
  if (diagnosticPage) {
    console.error('Page:', (await diagnosticPage.locator('body').innerText()).slice(0, 2500));
    await diagnosticPage.screenshot({ path: resolve(output, 'failure.png'), fullPage: true });
  }
  throw error;
} finally {
  const cleanup = await Promise.allSettled([
    browser?.close(), proxy?.close(),
    ...[cloud, local].filter(server => server.listening).map(server => new Promise((done, reject) => server.close(error => error ? reject(error) : done()))),
  ]);
  const failures = cleanup.filter(result => result.status === 'rejected').map(result => result.reason);
  if (failures.length) throw new AggregateError(failures, 'E2E resource cleanup failed');
}
