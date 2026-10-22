import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/config-app-layout');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { AppThemeProvider } from './src/AppThemeProvider';
import { ApplicationConfigPanel } from './src/components/config/ApplicationConfigPanel';
import './src/styles/variables.css';
createRoot(document.getElementById('root')).render(<AppThemeProvider><ApplicationConfigPanel token="fixture" /></AppThemeProvider>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outdir: output, entryNames: 'fixture', bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' }, loader: { '.css': 'css' },
});
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
const calls = [];
page.on('pageerror', error => errors.push(error.message));
await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
const origin = 'http://config-layout.test';
try {
  await page.route(origin + '/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('.js') || path.endsWith('.css')) return route.fulfill({
      contentType: path.endsWith('.css') ? 'text/css' : 'text/javascript',
      body: await readFile(resolve(output, path.slice(1))),
    });
    if (path.startsWith('/api/')) {
      calls.push(path);
      const data = path.endsWith('/legacy-config') ? { app_config: { apps: [
        { id: 'reports', name: '报告应用', description: '原有应用说明', url: 'https://example.test/reports', enabled: true, icon: '' },
      ] } } : { items: [] };
      return route.fulfill({ json: { code: 0, data } });
    }
    return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto(origin);
  await page.getByText('报告应用', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: /新\s*增\s*应\s*用/ }).count(), 1);
  assert.equal(await page.getByRole('region', { name: '普通外部应用', exact: true }).count(), 0);
  assert.equal(calls.some(path => path.endsWith('/admin/external-applications')), false);
  await page.locator('.ant-collapse-header').filter({ hasText: '普通外部应用' }).click();
  const external = page.getByRole('region', { name: '普通外部应用', exact: true });
  await external.getByRole('button', { name: '保存外部应用', exact: true }).waitFor();
  assert.equal(await external.getByRole('textbox').first().evaluate(el => getComputedStyle(el).borderRadius), '6px');
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.locator('.ant-collapse-header').filter({ hasText: '普通外部应用' }).click();
  await page.screenshot({ path: resolve(output, 'restored.png'), fullPage: true });
  assert.deepEqual(errors, []);
  console.log('Application config: original cards first, extensions collapsed, styled controls, desktop/mobile layout passed');
} finally { await browser.close(); }
