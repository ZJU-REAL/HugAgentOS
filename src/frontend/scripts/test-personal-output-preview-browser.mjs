import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import * as XLSX from 'xlsx';

const output = resolve('node_modules/.tmp/personal-output-preview');
await mkdir(output, { recursive: true });
const names = ['report.docx', 'slides.pptx', 'report.pdf', 'wide.xlsx', 'wide.csv', 'long.md', 'long.txt'];
await build({
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { PersonalOutputs } from './src/components/personal-agent/PersonalOutputs';
import './src/styles/variables.css';
import './src/styles/common.css';
import './src/styles/chat/import-browser.css';
import './src/styles/chat/rich-content.css';
import './src/styles/myspace.css';
const items = ${JSON.stringify(names)}.map(name => ({
  id: name, name, mime_type: 'application/octet-stream', size_bytes: 1024, created_at: '',
}));
createRoot(document.getElementById('root')).render(<PersonalOutputs page={{ items, total: 20, has_more: true }} />);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
  external: ['/loader.gif', '/loader-done.png'],
});
const rows = Array.from({ length: 180 }, (_, r) => Array.from({ length: 40 }, (_, c) => `Row ${r} column ${c} long value`));
const workbook = XLSX.utils.book_new();
for (const name of ['First sheet', 'Second sheet']) XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(rows), name);
const excel = XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' });
const browser = await chromium.launch({ headless: true,
  ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}),
});
const errors = [];
const results = [];
try {
  const page = await browser.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
  await page.route('https://output-preview.test/**', async route => {
    const url = new URL(route.request().url());
    if (/^\/fixture\.(js|css)$/.test(url.pathname)) return route.fulfill({
      contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
      body: await readFile(resolve(output, url.pathname.slice(1))),
    });
    if (url.pathname.includes('/files/')) {
      if (url.pathname.includes('wide.xlsx')) return route.fulfill({ body: excel });
      if (url.pathname.includes('wide.csv')) return route.fulfill({ body: rows.map(row => row.join(',')).join('\n') });
      if (/long\.(md|txt)/.test(url.pathname)) return route.fulfill({ body: '# Report\n\n' + 'Long report line\n\n'.repeat(500) });
      // Layout test: a deterministic embedded document avoids browser PDF plugin differences.
      return route.fulfill({ contentType: 'text/html', body: '<h1>Document preview</h1><div style="height:1800px">Page content</div>' });
    }
    if (url.pathname.startsWith('/api/')) return route.fulfill({ json: { code: 0, data: { items: names.map(name => ({ id: name, name, mime_type: 'application/octet-stream', size_bytes: 1024 })), total: 7, has_more: false } } });
    return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 600 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
    await page.setViewportSize(viewport);
    await page.goto('https://output-preview.test/');
    await page.getByRole('button', { name: '展开更多' }).click();
    for (const name of names) {
      await page.locator('.pa-output-name').filter({ hasText: name }).click();
      const dialog = page.getByRole('dialog', { name, exact: true });
      await dialog.waitFor();
      const leaf = dialog.locator(name.endsWith('xlsx') || name.endsWith('csv') ? '.jx-spaceImportPreview-table' :
        name.endsWith('.md') ? '.jx-spaceImportPreview-md' : name.endsWith('.txt') ? '.jx-spaceImportPreview-text' : 'iframe');
      await leaf.waitFor();
      await page.waitForTimeout(300);
      const box = await dialog.boundingBox();
      assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1, `${name} fits ${JSON.stringify(viewport)}: ${JSON.stringify(box)}`);
      const body = await dialog.locator('.jx-spaceImportPreview-body').boundingBox();
      assert.ok(body.height > viewport.height * 0.45, `${name} preview must use available height: ${body.height}`);
      assert.equal(await dialog.evaluate(el => el.scrollWidth > el.clientWidth + 1), false, 'No dialog horizontal overflow');
      if (/xlsx|csv/.test(name)) {
        const scroll = dialog.locator('.jx-spaceImportPreview-tableWrap');
        assert.ok(await scroll.evaluate(el => el.scrollWidth > el.clientWidth && el.scrollHeight > el.clientHeight), 'Wide/tall table scrolls inside preview');
        if (name.endsWith('xlsx')) await dialog.getByRole('button', { name: 'Second sheet', exact: true }).click();
      }
      if (viewport.width === 1440 && /docx|xlsx/.test(name)) await page.screenshot({ path: resolve(output, name + '.png') });
      results.push({ name, viewport, previewHeight: Math.round(body.height) });
      await dialog.getByRole('button', { name: 'Close', exact: true }).click();
      await dialog.waitFor({ state: 'hidden' });
    }
  }
  // The history drawer uses the same output component and must retain the same bounds.
  await page.getByRole('button', { name: /查看全部输出/ }).click();
  const drawer = page.locator('.ant-drawer-body');
  await drawer.locator('.pa-output-name').filter({ hasText: 'wide.xlsx' }).click();
  const historyDialog = page.getByRole('dialog', { name: 'wide.xlsx', exact: true });
  await historyDialog.locator('.jx-spaceImportPreview-table').waitFor();
  await page.setViewportSize({ width: 500, height: 500 });
  await page.waitForTimeout(400);
  const resized = await historyDialog.boundingBox();
  assert.ok(resized.x >= 0 && resized.y >= 0 && resized.x + resized.width <= 501 && resized.y + resized.height <= 501,
    'An open history preview adapts when the window resizes');
  await historyDialog.getByRole('button', { name: 'Close', exact: true }).click();
  await historyDialog.waitFor({ state: 'hidden' });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ checks: results.length, results }));
} finally {
  await browser.close();
}
