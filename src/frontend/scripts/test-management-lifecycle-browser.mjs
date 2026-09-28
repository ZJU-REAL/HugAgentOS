import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const backend = process.env.MANAGEMENT_TEST_BACKEND || 'http://127.0.0.1:18769';
const output = resolve('node_modules/.tmp/management-lifecycle-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router';
import { SkillsPage } from './src/components/catalog/SkillsPage';
import { PluginsPage } from './src/components/catalog/PluginsPage';
import { useCatalogStore, usePluginStore, useAuthStore } from './src/stores';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import { refreshTargetForTool } from './src/utils/toolRefresh';
import './src/styles/variables.css';
import './src/styles/catalog.css';
import './src/styles/mcp.css';
useDeploymentModeStore.setState({ provisionMode: 'local_only', isDesktop: true, activeLocal: true });
useAuthStore.setState({ authUser: { user_id: 'browser-owner', can_add_skill: true, can_import_plugin: true } });
window.afterTool = async name => {
  const target = refreshTargetForTool(name);
  if(target === 'catalog') await useCatalogStore.getState().fetchCatalog();
  if(target === 'plugins') await usePluginStore.getState().fetchInstalled(true);
};
function Fixture() {
 const [tab, setTab] = useState('skills');
 return <MemoryRouter><button onClick={() => setTab('skills')}>TEST skills</button><button onClick={() => setTab('plugins')}>TEST plugins</button>
 {tab === 'skills' ? <SkillsPage embedded /> : <PluginsPage />}</MemoryRouter>;
}
useCatalogStore.getState().fetchCatalog();
createRoot(document.getElementById('root')).render(<Fixture />);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' }, external: ['/loader.gif', '/loader-done.png'],
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
});
async function post(path, body) {
 const response = await fetch(backend + path, { method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body) });
 assert.equal(response.status, 200, await response.clone().text());
 return response.json();
}
const browser = await chromium.launch({ headless: true,
 ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}) });
try {
 const page = await browser.newPage({ viewport: {width: 1280, height: 900} });
 const errors = [];
 page.on('pageerror', error => errors.push(error.message));
 await page.route('https://manager.test/**', async route => {
  const url = new URL(route.request().url());
  if(['/fixture.js','/fixture.css'].includes(url.pathname)) return route.fulfill({
   contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css', body: await readFile(resolve(output, url.pathname.slice(1))) });
  if(url.pathname.startsWith('/api/v1/catalog') || url.pathname.startsWith('/api/v1/plugins') || url.pathname.startsWith('/api/v1/me/skills')) {
   const response = await page.request.fetch(backend + url.pathname + url.search, {method: route.request().method(), headers: route.request().headers(), data: route.request().postDataBuffer() || undefined});
   return route.fulfill({response});
  }
  if(url.pathname.startsWith('/api')) return route.fulfill({json: {code: 10000, data: {items: [], skills: [], mcp: [], agents: [], kb: []}}});
  return route.fulfill({contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>'});
 });
 await page.goto('https://manager.test/');
 for(const kind of ['skill','plugin']) {
  const name = 'browser-' + kind;
  const source = await post('/test/package/' + kind, {name});
  await page.getByRole('button', {name: 'TEST ' + kind + 's', exact: true}).click();
  assert.equal(await page.getByText(name, {exact: true}).count(), 0);
  const installed = await post('/test/tool/' + kind + '-manager/install_' + kind, {source: {kind: 'local_path', path: source.path}});
  assert.equal(installed.ok, true, JSON.stringify(installed));
  await page.evaluate(name => window.afterTool(name), 'install_' + kind);
  await page.getByText(name, {exact: true}).first().waitFor();
  await page.screenshot({path: resolve(output, kind + '-installed.png'), fullPage: true});
  if(kind === 'plugin') {
   await page.getByText(name, {exact: true}).first().click();
   await page.getByText('child-skill', {exact: true}).waitFor();
   await page.screenshot({path: resolve(output, 'plugin-detail.png'), fullPage: true});
   await page.getByRole('button', {name: 'TEST skills', exact:true}).click();
   assert.equal(await page.getByText('child-skill', {exact:true}).count(), 0);
   await page.getByRole('button', {name: 'TEST plugins', exact:true}).click();
  }
  await post('/test/package/' + kind, {name, text: 'Version two'});
  const updated = await post('/test/tool/' + kind + '-manager/update_' + kind, {
   install_id: installed.install_id, expected_revision: installed.revision, source: {kind:'local_path',path:source.path}});
  assert.equal(updated.ok, true, JSON.stringify(updated));
  assert.notEqual(updated.revision, installed.revision);
  const stale = await post('/test/tool/' + kind + '-manager/uninstall_' + kind, {install_id: installed.install_id, expected_revision: installed.revision});
  assert.equal(stale.ok, false);
  const removed = await post('/test/tool/' + kind + '-manager/uninstall_' + kind, {install_id: installed.install_id, expected_revision: updated.revision});
  assert.equal(removed.ok, true, JSON.stringify(removed));
  await page.evaluate(name => window.afterTool(name), 'uninstall_' + kind);
  await page.getByText(name, {exact: true}).waitFor({state:'detached'});
 }
 for (const kind of ['skill', 'plugin']) {
  const name = 'ui-upload-' + kind;
  await post('/test/package/' + kind, {name});
  const archive = Buffer.from(await (await fetch(backend + '/test/archive/' + name)).arrayBuffer());
  await page.getByRole('button', {name: 'TEST ' + kind + 's', exact:true}).click();
  await page.locator('input[type=file]').first().setInputFiles({name: name + '.zip', mimeType: 'application/zip', buffer: archive});
  await page.getByText(name, {exact:true}).first().waitFor();
  const listing = await post('/test/tool/' + kind + '-manager/list_' + kind + 's', {});
  const item = listing.items.find(item => item.name === name);
  assert.ok(item, 'UI upload must appear in the same manager registry');
  const removed = await post('/test/tool/' + kind + '-manager/uninstall_' + kind, {install_id:item.install_id, expected_revision:item.revision});
  assert.equal(removed.ok, true);
  await page.evaluate(name => window.afterTool(name), 'uninstall_' + kind);
  await page.getByText(name, {exact:true}).waitFor({state:'detached'});
 }
 assert.deepEqual(errors, []);
 console.log('PASS: real manager tools → SQLite registry → real catalog/plugin routes → browser cards and plugin child detail; update conflicts and removal verified.');
 console.log('Screenshots: ' + output);
} finally { await browser.close(); }
