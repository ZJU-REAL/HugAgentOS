import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/model-status-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { ModelsEditor } from './src/components/admin/ModelsEditor';
    createRoot(document.getElementById('root')).render(<ModelsEditor token="test" />);
  `, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm',
  jsx: 'automatic', define: { 'import.meta.env': '{}' },
});
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
});
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  const provider = { provider_id: 'offline', display_name: 'Offline model', provider_type: 'chat',
    provider: 'openai_compatible', base_url: 'https://offline.test/v1', model_name: 'offline',
    is_active: true, extra_config: { context_length: 32000 }, last_test_status: 'failure' };
  const writes = [];
  let fail = false;
  let release;
  await page.route('https://models.test/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/fixture.js') return route.fulfill({ contentType: 'text/javascript', body: await readFile(resolve(output, 'fixture.js')) });
    if (path === '/api/v1/models/providers/offline' && request.method() === 'PUT') {
      const body = request.postDataJSON();
      writes.push(body);
      if (Object.keys(body).length === 1 && 'is_active' in body) {
        await new Promise(resolve => { release = resolve; });
        if (fail) return route.fulfill({ status: 500, json: { detail: 'Status save failed' } });
      }
      Object.assign(provider, body);
      return route.fulfill({ json: { data: provider } });
    }
    if (path === '/api/v1/models/providers') return route.fulfill({ json: { data: [provider] } });
    if (path === '/api/v1/models/provider-schemas') return route.fulfill({ json: { data: [{
      id: 'openai_compatible', label: 'OpenAI compatible', engine: 'openai', fields: [], supports_types: ['chat'],
    }] } });
    if (path.startsWith('/api/')) return route.fulfill({ json: { data: [] } });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script type="module" src="/fixture.js"></script>' });
  });
  await page.goto('https://models.test/');
  const row = page.getByRole('row').filter({ hasText: 'Offline model' });
  const toggle = row.getByRole('switch');
  await toggle.waitFor({ timeout: 5000 });
  await toggle.click();
  await page.waitForFunction(() => document.querySelector('button[role="switch"]')?.disabled);
  assert.equal(await toggle.getAttribute('aria-checked'), 'true', 'wait for server confirmation');
  while (!release) await new Promise(resolve => setTimeout(resolve, 10));
  release(); release = undefined;
  await page.waitForFunction(() => document.querySelector('button[role="switch"]')?.getAttribute('aria-checked') === 'false');
  assert.deepEqual(writes[0], { is_active: false }, 'offline disable sends no connection configuration');

  fail = true;
  await toggle.click();
  while (!release) await new Promise(resolve => setTimeout(resolve, 10));
  release(); release = undefined;
  await page.getByText('Status save failed', { exact: false }).waitFor();
  assert.equal(await toggle.getAttribute('aria-checked'), 'false', 'failed write preserves saved state');
  fail = false;
  await toggle.click();
  while (!release) await new Promise(resolve => setTimeout(resolve, 10));
  release(); release = undefined;
  await page.waitForFunction(() => document.querySelector('button[role="switch"]')?.getAttribute('aria-checked') === 'true');
  await toggle.click();
  while (!release) await new Promise(resolve => setTimeout(resolve, 10));
  release(); release = undefined;
  await page.waitForFunction(() => document.querySelector('button[role="switch"]')?.getAttribute('aria-checked') === 'false');

  await row.getByRole('button').filter({ has: page.locator('[aria-label="edit"]') }).click();
  const dialog = page.getByRole('dialog');
  await dialog.waitFor();
  assert.equal(await dialog.locator('#is_active').count(), 0, 'edit form has no enable field');
  await dialog.getByRole('button', { name: '保 存' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.ok(!('is_active' in writes.at(-1)), 'editing leaves server status untouched');
  assert.equal(await toggle.getAttribute('aria-checked'), 'false');
  await page.screenshot({ path: resolve(output, 'model-status.png') });
  console.log('PASS: offline disable, pending state, failed save, retry, enable, and edit preserving disabled status');
} finally { await browser.close(); }
