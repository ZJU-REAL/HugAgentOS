// Requires npm run build. Uses the production bundle with controlled backend and shell-event boundaries.
import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import('playwright');
const output = resolve('node_modules/.tmp/project-bootstrap-browser');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE });
try {
 const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
 const errors = [];
 page.on('pageerror', e => errors.push(e.message));
 let ready = false, localRequests = 0, failedLocalRequests = 0;
 await page.addInitScript(() => {
   localStorage.setItem('jx_lang', 'zh-CN');
   window.__HG_DESKTOP__ = { provision_mode: 'dual' };
   window.EventSource = class { constructor(url) { if (url === '/__desktop/events') window.desktopEvents = this; } close() {} };
 });
 await page.route('http://desktop-project.test/**', async route => {
   const path = new URL(route.request().url()).pathname;
   const local = route.request().headers()['x-hugagent-target'] === 'local';
   if (path === '/api/v1/projects') {
     if (local) { localRequests++; if (!ready) { failedLocalRequests++; return route.fulfill({ status: 503, json: { code: 503, message: 'identity not ready' } }); } }
     return route.fulfill({ json: { code: 0, data: { items: [{
       project_id: local ? 'local-project' : 'cloud-project',
       name: local ? 'E2E local project' : 'E2E cloud project',
       kind: local ? 'local' : 'personal', permission: 'admin',
     }], pagination: { total_items: 1 } } } });
   }
   if (path.startsWith('/api') || path.startsWith('/__desktop')) {
     let data = {};
     if (path.endsWith('/auth/session/check')) data = { user_id: 'e2e-user', username: 'e2e', display_name: 'E2E' };
     else if (path.endsWith('/meta/edition')) data = { edition: 'ee', features: {}, license: {} };
     else if (path.endsWith('/catalog')) data = { skills: [], mcp: [], agents: [], kb: [] };
     else if (path.endsWith('/sync-status')) data = { ready: true, partial: false };
     else if (/chats|tasks|notifications|models|agents|installed|contributions|automations|teams|folders|skills/.test(path)) data = { items: [], models: [], count: 0 };
     return route.fulfill({ json: { code: 0, data } });
   }
   const file = resolve('dist', path === '/' ? 'index.html' : path.slice(1));
   const body = await readFile(file).catch(() => null);
   if (!body) return route.fulfill({ status: 404 });
   return route.fulfill({ contentType: path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : path.endsWith('.svg') ? 'image/svg+xml' : path.startsWith('/assets/') ? 'application/octet-stream' : 'text/html', body });
 });
 await page.goto('http://desktop-project.test/');
 await page.waitForFunction(() => !!window.desktopEvents);
 await page.waitForTimeout(1500);
 assert.ok(failedLocalRequests > 0, 'production app initially attempted local projects before identity readiness');
 ready = true;
 await page.evaluate(() => window.desktopEvents.onmessage({ data: JSON.stringify({
   bridge: { identity_ready: true, capabilities_ready: true, models_ready: true },
   service: { ready: true },
 }) }));
 await page.getByText('E2E local project', { exact: true }).waitFor({ timeout: 15000 });
 assert.equal(await page.evaluate(() => location.pathname), '/');
 assert.ok(localRequests >= 2, 'production controller refreshes after readiness');
 await page.screenshot({ path: resolve(output, 'production-sidebar.png'), fullPage: true });
 assert.deepEqual(errors, []);
 console.log('Production bundle E2E: authentication, initial local 503, identity SSE, capability gate, actual sidebar project rendering without panel navigation passed');
} finally { await browser.close(); }
