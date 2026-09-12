import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');

const output = resolve('node_modules/.tmp/capability-changes-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { AbilityCenterPage } from './src/components/catalog/AbilityCenterPage';
import { useDeploymentModeStore } from './src/stores/deploymentModeStore';
import { useCatalogStore } from './src/stores/catalogStore';
import './src/styles/variables.css';
import './src/styles/catalog.css';
import './src/styles/mcp.css';
useDeploymentModeStore.setState({ provisionMode: 'dual', partialCapabilities: false });
useCatalogStore.setState({ abilityTab: 'skills', visitedAbilityTabs: ['skills'] });
createRoot(document.getElementById('root')).render(<AbilityCenterPage />);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' }, external: ['/loader.gif', '/loader-done.png'],
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
});
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}) });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let dropped = false;
  let commits = 0;
  await page.route('https://capability.test/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') {
      return route.fulfill({ contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
        body: await readFile(resolve(output, url.pathname.slice(1))) });
    }
    if (url.pathname.includes('/installations')) return route.fulfill({ json: { code: 0, data: {
      kind: 'skill', profile_id: 'cloud-account', items: dropped ? [{
        install_id: 'skill:local:dropped', runtime_name: 'dropped', display_name: 'Dropped News',
        description: 'Local news reports', source: 'local', kind: 'skill', enabled: true,
        usable: true, change_state: 'new',
      }] : [],
    } } });
    if (url.pathname.endsWith('/changes/preview')) return route.fulfill({ json: { code: 0, data: {
      preview_id: 'a'.repeat(32), can_edit: true, cloud_exists: true, sensitive_paths: [],
      changes: [
        { path: 'SKILL.md', conflict: true, local: 'Local instructions', cloud: 'Cloud instructions', binary: false },
        { path: 'query.json', conflict: false, local: '{}', cloud: null, binary: false },
      ],
    } } });
    if (url.pathname.endsWith('/changes/commit')) {
      commits += 1;
      assert.equal(route.request().postDataJSON().choices['SKILL.md'].side, 'local');
      return route.fulfill({ json: { code: 0, data: { uploaded: true, local_applied: true, applied: true } } });
    }
    if (url.pathname.startsWith('/api')) return route.fulfill({ json: { code: 0, data: [] } });
    return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:14px Arial;background:var(--color-bg-container);color:var(--color-text)}#root{padding:16px}.jx-abilityCenterPane:not(.active){display:none}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto('https://capability.test/');
  await page.locator('.jx-sk-header').waitFor();
  assert.equal(await page.getByText('Dropped News', { exact: true }).count(), 0);
  dropped = true;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.getByText('Dropped News', { exact: true }).waitFor();
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  assert.equal(await page.getByText('Dropped News', { exact: true }).count(), 1);
  await page.getByRole('button', { name: '比较云端版本' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText('query.json', { exact: true }).waitFor();
  assert.equal(commits, 0);
  assert.ok(await dialog.getByRole('button', { name: '提交到云端' }).isDisabled());
  await dialog.locator('.ant-collapse-header').filter({ hasText: 'SKILL.md' }).click();
  for (const [name, width] of [['desktop', 1280], ['mobile', 390]]) {
    await page.setViewportSize({ width, height: 900 });
    await page.evaluate(() => Promise.all(document.getAnimations()
      .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
      .map((animation) => animation.finished.catch(() => {}))));
    await page.screenshot({ path: resolve(output, name + '.png'), fullPage: true });
    const box = await dialog.boundingBox();
    assert.ok(box && box.x >= 0 && box.x + box.width <= width + 1, name + ' dialog fits viewport');
    assert.equal(await dialog.evaluate((node) => node.scrollWidth > node.clientWidth + 1), false);
  }
  await dialog.getByRole('combobox', { name: '选择文件版本' }).click();
  await page.getByText('使用本地版本', { exact: true }).click();
  await dialog.getByRole('button', { name: '提交到云端' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(commits, 1);
  assert.deepEqual(errors, []);
  console.log('Capability discovery, explicit conflict resolution and responsive modal: passed');
} finally {
  await browser.close();
}
