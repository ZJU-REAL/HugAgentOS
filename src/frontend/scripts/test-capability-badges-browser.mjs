import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/capability-badges-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { DeviceCapabilityBadge } from './src/components/catalog/DeviceCapabilityBadge';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import { useDesktopCapabilityStore } from './src/stores/desktopCapabilityStore';
import './src/styles/variables.css';
useDeploymentModeStore.setState({ provisionMode: 'dual' });
const root = createRoot(document.getElementById('root'));
window.showBadges = async (cards) => {
  root.render(null);
  useDesktopCapabilityStore.getState().reset();
  await Promise.all(['plugin','skill','mcp','agent'].map(kind => useDesktopCapabilityStore.getState().load(kind)));
  root.render(<div>{cards.map(({kind, name}) => <article key={kind + name} data-card={kind + ':' + name}>
    <h3>{kind} / {name}</h3><DeviceCapabilityBadge kind={kind} runtimeName={name}/>
  </article>)}</div>);
};
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
});
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}) });
try {
  const page = await browser.newPage({ viewport: { width: 1000, height: 800 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const kinds = ['plugin', 'skill', 'mcp', 'agent'];
  let listings = Object.fromEntries(kinds.map(kind => [kind, { kind, profile_id: 'p_current', items: [] }]));
  const previews = [];
  let commits = 0;
  await page.route('https://capability.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') return route.fulfill({
      contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
      body: await readFile(resolve(output, url.pathname.slice(1))),
    });
    if (url.pathname.endsWith('/installations')) return route.fulfill({
      json: { code: 0, data: listings[url.searchParams.get('kind')] },
    });
    if (url.pathname.endsWith('/changes/preview')) {
      previews.push(route.request().postDataJSON().install_id);
      return route.fulfill({ json: { code: 0, data: {
        preview_id: 'a'.repeat(32), can_edit: true, cloud_exists: true, sensitive_paths: [], changes: [],
      } } });
    }
    if (url.pathname.endsWith('/changes/commit')) commits += 1;
    if (url.pathname.startsWith('/api')) return route.fulfill({ json: { code: 0, data: [] } });
    return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto('https://capability.test/');
  await page.waitForFunction(() => typeof window.showBadges === 'function');
  const states = ['synced', 'compare', 'unavailable', undefined, 'new', 'modified'];
  let cards = [];
  for (const kind of kinds) {
    listings[kind].items = states.map((state, index) => ({
      kind, install_id: kind + ':p_current:case-' + index, runtime_name: 'case-' + index,
      source: 'cloud', change_state: state, resolution: { outcome: 'chosen' },
    }));
    cards.push(...listings[kind].items.map(item => ({ kind, name: item.runtime_name })));
  }
  await page.evaluate(cards => window.showBadges(cards), cards);
  await page.locator('article').first().waitFor();
  for (const kind of kinds) {
    for (const [index, state] of states.entries()) {
      const buttons = page.locator('[data-card="' + kind + ':case-' + index + '"]').getByRole('button');
      assert.equal(await buttons.count(), state === 'new' || state === 'modified' ? 1 : 0,
        kind + ': upload entry for state ' + state);
    }
  }
  listings.plugin.items = [
    { kind: 'plugin', install_id: 'plugin:local:sites', runtime_name: 'sites', source: 'local', change_state: 'new' },
    { kind: 'plugin', install_id: 'plugin:p_current:sites', runtime_name: 'sites', source: 'cloud', change_state: 'modified' },
  ];
  await page.evaluate(() => window.showBadges([{ kind: 'plugin', name: 'sites' }]));
  const card = page.locator('[data-card="plugin:sites"]');
  await card.waitFor();
  assert.match(await card.innerText(), /云端/);
  assert.doesNotMatch(await card.innerText(), /未提交/);
  await card.getByRole('button').click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText('本地与云端内容一致', { exact: true }).waitFor();
  assert.deepEqual(previews, ['plugin:p_current:sites']);
  assert.ok(await dialog.getByRole('button', { name: '提交到云端' }).isDisabled(),
    'An unchanged preview must not offer a redundant cloud write');
  assert.equal(commits, 0);
  await page.screenshot({ path: resolve(output, 'unchanged-preview.png'), fullPage: true });
  await dialog.getByRole('button', { name: /取\s*消/ }).click();
  if (process.env.CAPABILITY_LISTINGS) {
    const recorded = JSON.parse(await readFile(process.env.CAPABILITY_LISTINGS, 'utf8'));
    listings = Object.fromEntries(recorded.map(list => [list.kind, list]));
    cards = recorded.flatMap(list => list.items.filter(item => item.source === 'cloud')
      .map(item => ({ kind: list.kind, name: item.runtime_name })));
    await page.evaluate(cards => window.showBadges(cards), cards);
    await page.waitForFunction(count => document.querySelectorAll('article').length === count, cards.length);
    assert.equal(await page.locator('.jx-devcap-src-cloud').count(), cards.length,
      'Every cloud card must render its cloud source, not merely omit the wrong label');
    assert.equal(await page.locator('.jx-devcap-src-local,.jx-devcap-src-builtin').count(), 0);
    assert.equal(await page.locator('article button').count(), 0);
    console.log('Windows capability metadata replay: all ' + cards.length + ' cloud cards retain cloud source, zero upload buttons');
  }
  assert.deepEqual(errors, []);
  console.log('Capability badges: four kinds, unchanged/modified/new states, exact preview target, no empty submit passed');
} finally { await browser.close(); }
