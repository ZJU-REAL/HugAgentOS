import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/project-bootstrap-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { useState } from 'react';
import { useProjectListBootstrap } from './src/hooks/useProjectListBootstrap';
import { useProjectStore } from './src/stores/projectStore';
import { useSidebarHistory } from './src/components/sidebar/useSidebarHistory';
import { isLocalProject } from './src/api';
function Workspace() {
  const [user, setUser] = useState('account-a');
  useProjectListBootstrap(user);
  const { projectGroups } = useSidebarHistory();
  window.fixture = { setUser, store: useProjectStore, isLocalProject };
  return <aside>{projectGroups.map(p => <div key={p.projectId} data-project={p.projectId}>{p.name}</div>)}</aside>;
}
createRoot(document.getElementById('root')).render(<Workspace/>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' }, loader: { '.css': 'empty' },
});
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}) });
const response = items => ({ code: 0, data: { items, pagination: { total_items: items.length } } });
const project = (id, kind) => ({ project_id: id, name: id, kind });
const cloud = response([project('Cloud project', 'personal')]);
const local = response([project('Local project', 'local')]);

async function setup(mode = 'dual', delayFirstCloud = false) {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  let account = 'a';
  let ready = false, localCalls = 0, cloudCalls = 0, releaseCloud;
  await page.addInitScript(mode => {
    if (mode !== 'web') window.__HG_DESKTOP__ = { provision_mode: mode, active_local: mode === 'local_only' };
    window.EventSource = class {
      constructor() { window.desktopEvents = this; }
    };
  }, mode);
  await page.route('http://project-bootstrap.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/fixture.js') return route.fulfill({
      contentType: 'text/javascript', body: await readFile(resolve(output, 'fixture.js')),
    });
    if (url.pathname === '/api/v1/projects') {
      const isLocal = route.request().headers()['x-hugagent-target'] === 'local';
      if (isLocal) {
        localCalls++;
        if (!ready) return route.fulfill({ status: 503, json: { code: 503, message: 'identity not ready' } });
        return route.fulfill({ json: local });
      }
      cloudCalls++;
      const reply = account === 'a' ? cloud : response([project('Account B project', 'personal')]);
      if (delayFirstCloud && cloudCalls === 1) {
        await new Promise(resolve => { releaseCloud = resolve; });
      }
      return route.fulfill({ json: mode === 'local_only' ? local : reply });
    }
    return route.fulfill({ contentType: 'text/html', body:
      '<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto('http://project-bootstrap.test/');
  await page.waitForFunction(() => !!window.fixture);
  const identity = async value => {
    ready = value;
    await page.evaluate(value => window.desktopEvents.onmessage({
      data: JSON.stringify({ bridge: { identity_ready: value } }),
    }), value);
  };
  return { page, errors, identity, calls: () => ({ localCalls, cloudCalls }), account: value => { account = value; }, release: () => releaseCloud?.() };
}

try {
  const normal = await setup();
  await normal.page.getByText('Cloud project', { exact: true }).waitFor();
  assert.equal(await normal.page.getByText('Local project', { exact: true }).count(), 0);
  await normal.identity(true);
  await normal.page.getByText('Local project', { exact: true }).waitFor({ timeout: 3000 });
  assert.equal(await normal.page.evaluate(() => location.pathname), '/', 'project panel was never opened');
  assert.equal(await normal.page.evaluate(() => window.fixture.isLocalProject('Local project')), true);
  const count = normal.calls();
  await normal.identity(true);
  await normal.page.waitForTimeout(100);
  assert.deepEqual(normal.calls(), count, 'unchanged readiness does not refetch');
  await normal.page.screenshot({ path: resolve(output, 'ready-sidebar.png') });

  // The login request is held across readiness; it must not erase the new list.
  const race = await setup('dual', true);
  await race.page.waitForFunction(() => window.fixture.store.getState().listLoading);
  await race.identity(true);
  await race.page.getByText('Local project', { exact: true }).waitFor();
  race.release();
  await race.page.waitForTimeout(150);
  assert.equal(await race.page.getByText('Local project', { exact: true }).count(), 1);

  // Logout must clear the list, and a pending response must not resurrect it.
  const logout = await setup('dual', true);
  await logout.page.waitForFunction(() => window.fixture.store.getState().listLoading);
  await logout.page.evaluate(() => window.fixture.setUser(undefined));
  logout.release();
  await logout.page.waitForTimeout(150);
  assert.equal(await logout.page.locator('[data-project]').count(), 0);
  assert.equal(await logout.page.evaluate(() => window.fixture.store.getState().listLoading), false);

  const account = await setup('dual', true);
  await account.page.waitForFunction(() => window.fixture.store.getState().listLoading);
  account.account('b');
  await account.page.evaluate(() => window.fixture.setUser('account-b'));
  await account.page.getByText('Account B project', { exact: true }).waitFor();
  account.release();
  await account.page.waitForTimeout(150);
  assert.equal(await account.page.getByText('Cloud project', { exact: true }).count(), 0);

  // Exercise list loading/error ownership through the real API and store.
  await normal.page.evaluate(async () => {
    const original = window.fetch;
    const pending = [];
    window.fetch = () => new Promise(resolve => pending.push(resolve));
    const store = window.fixture.store;
    const old = store.getState().fetchProjects();
    const newer = store.getState().fetchProjects();
    // Each dual request issues cloud then local.
    pending[0](new Response('{}', { status: 500 }));
    pending[1](new Response('{}', { status: 503 }));
    await old;
    if (!store.getState().listLoading || store.getState().listError) throw new Error('stale failure modified newer loading/error');
    const reply = { code: 0, data: { items: [{ project_id: 'new', name: 'Newest', kind: 'personal' }] } };
    pending[2](new Response(JSON.stringify(reply)));
    pending[3](new Response('{}', { status: 503 }));
    await newer;
    if (store.getState().listLoading || store.getState().listError || store.getState().list[0]?.name !== 'Newest') throw new Error('new request not accepted');
    window.fetch = original;
  });

  for (const mode of ['web', 'cloud_only', 'local_only']) {
    const single = await setup(mode);
    await single.page.getByText(mode === 'local_only' ? 'Local project' : 'Cloud project', { exact: true }).waitFor();
    assert.equal(single.calls().localCalls, 0, mode + ' keeps default backend routing');
    assert.deepEqual(single.errors, []);
    await single.page.close();
  }
  for (const fixture of [normal, race, logout, account]) {
    assert.deepEqual(fixture.errors, []);
    await fixture.page.close();
  }
  console.log('Project bootstrap browser: delayed identity/503 recovery, no panel navigation, routing registration, repeated SSE, stale success/error/loading, logout, account switch and single modes passed');
} finally { await browser.close(); }
