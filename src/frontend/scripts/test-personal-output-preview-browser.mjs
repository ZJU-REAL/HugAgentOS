import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

// Personal Agent outputs use the chat's file cards: the same look, and a click opens the same canvas.
const output = resolve('node_modules/.tmp/personal-output-preview');
await mkdir(output, { recursive: true });
const names = ['report.docx', 'slides.pptx', 'wide.xlsx'];
await build({
  stdin: { contents: `
import { createRoot } from 'react-dom/client';
import { PersonalOutputs } from './src/components/personal-agent/workspace/PersonalOutputs';
import { useCanvasStore } from './src/stores/canvasStore';
import './src/styles/variables.css';
window.canvasState = () => { const state = useCanvasStore.getState(); return { open: state.isOpen, name: state.artifact?.name || state.tabs?.at?.(-1)?.name }; };
const items = ${JSON.stringify(names)}.map(name => ({ id: 'file-' + name, name, mime_type: 'application/octet-stream', size_bytes: 1024, created_at: '', url: '/api/files/file-' + name }));
createRoot(document.getElementById('root')).render(<div style={{ width: 300 }}>
  <div data-testid="filled"><PersonalOutputs page={{ items, total: 20, has_more: true, images: 0, documents: 3, other: 0 }} /></div>
  <div data-testid="empty"><PersonalOutputs page={{ items: [], total: 0, has_more: false, images: 0, documents: 0, other: 0 }} /></div>
</div>);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.svg': 'dataurl', '.png': 'dataurl' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') },
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined });
const errors = [];
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
    if (url.pathname.endsWith('/outputs')) return route.fulfill({ json: { code: 0, message: 'ok', data: {
      items: ['old.md', 'older.pdf'].map(name => ({ id: 'file-' + name, name, mime_type: 'application/octet-stream', size_bytes: 1, created_at: '', url: '/api/files/file-' + name })),
      total: 2, has_more: false, images: 0, documents: 2, other: 0 } } });
    if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto('https://output-preview.test/');
  const filled = page.getByTestId('filled');
  await filled.locator('.hg-delivery-file').first().waitFor();
  assert.deepEqual(await filled.locator('.hg-delivery-name').allInnerTexts(), names, 'every output is a chat file card');
  assert.equal(await page.getByTestId('empty').innerHTML(), '', 'no outputs renders nothing');
  const columns = await filled.locator('.hg-deliveries-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  assert.equal(columns, 1, 'one card per row in the narrow side panel');
  await filled.getByRole('button', { name: '预览文件：slides.pptx' }).click();
  assert.deepEqual(await page.evaluate(() => window.canvasState()), { open: true, name: 'slides.pptx' }, 'a card opens the canvas');
  // The history drawer uses the same cards and closes when one is opened.
  await filled.getByRole('button', { name: /查看全部输出/ }).click();
  const drawer = page.locator('.ant-drawer-body');
  await drawer.locator('.hg-delivery-file').nth(1).waitFor();
  await drawer.getByRole('button', { name: '预览文件：older.pdf' }).click();
  await page.locator('.ant-drawer-open').waitFor({ state: 'detached' });
  assert.deepEqual(await page.evaluate(() => window.canvasState()), { open: true, name: 'older.pdf' });
  assert.deepEqual(errors, []);
  console.log('PASS: outputs are chat file cards that open the canvas; history drawer closes on open; empty renders nothing.');
} finally {
  await browser.close();
}
