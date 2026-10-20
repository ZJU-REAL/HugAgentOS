import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const root = resolve('../..');
const output = resolve('node_modules/.tmp/personal-delivery');
await mkdir(output, { recursive: true });
const fixturePath = resolve(output, 'contract.json');
execFileSync(resolve(root, '.venv/bin/python'), ['-m', 'pytest',
  'src/backend/tests/test_personal_agent_delivery.py::test_screenshot_file_and_channel_end_to_end',
  '-q', '--tb=short', '--show-capture=no'], { cwd: root, env: {
    ...process.env, PYTHONPATH: 'src/backend', PA_DELIVERY_BROWSER_DATA: fixturePath,
  } });
const fixture = JSON.parse(await readFile(fixturePath, 'utf8'));
await build({ entryPoints: ['scripts/personal-delivery-fixture.tsx'], outfile: resolve(output, 'fixture.js'),
  external: ['/loader.gif', '/loader-done.png'],
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.svg': 'dataurl', '.png': 'dataurl' },
  bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') } });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 850 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://delivery.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/fixture-messages') return route.fulfill({ json: fixture.messages });
    if (url.pathname === '/fixture.js' || url.pathname === '/fixture.css') {
      return route.fulfill({ contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript',
        body: await readFile(resolve(output, url.pathname.slice(1))) });
    }
    if (url.pathname.startsWith('/icons/file/')) {
      return route.fulfill({ contentType: 'image/svg+xml',
        body: await readFile(resolve('public', url.pathname.slice(1))) });
    }
    const id = url.pathname.match(/\/(?:api\/)?files\/([^/]+)/)?.[1];
    if (id && fixture.files[id]) {
      const file = fixture.files[id];
      return route.fulfill({ contentType: file.mime, body: Buffer.from(file.data, 'base64') });
    }
    if (url.pathname !== '/') return route.fulfill({ status: 404 });
    return route.fulfill({ contentType: 'text/html',
      body: '<link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>' });
  });
  await page.goto('http://delivery.test/');
  await page.getByText('任务仍在执行').waitFor();
  await page.getByRole('button', { name: '预览文件：云电脑截图.png' }).waitFor();
  assert.equal(await page.locator('[data-presented-file]').count(), 2);

  assert.equal(await page.locator('.pa-message-files').count(), 0);
  assert.equal(await page.getByText('报告.txt', { exact: true }).count(), 1);
  await page.getByRole('button', { name: '预览文件：云电脑截图.png' }).click();
  await page.getByLabel('文件预览').waitFor();
  const downloadWait = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载 报告.txt' }).click();
  assert.equal((await downloadWait).suggestedFilename(), '报告.txt');
  await page.getByRole('button', {name:'预览文件：报告.txt'}).click();
  await page.getByLabel('文件预览').getByText('verified output', { exact: false }).waitFor();
  await page.screenshot({ path: resolve(output, 'delivery-desktop.png'), fullPage: true });
  await page.reload();
  await page.getByRole('button', { name: '预览文件：云电脑截图.png' }).waitFor();
  assert.equal(await page.locator('[data-presented-file]').count(), 2);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: resolve(output, 'delivery-mobile.png'), fullPage: true });
  const fileName = await page.locator('.hg-delivery-name').last().boundingBox();
  assert.ok(fileName && fileName.width >= 80, 'Mobile file name must remain readable beside its controls');
  await page.screenshot({ path: resolve(output, 'delivery-mobile.png'), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  assert.deepEqual(errors, []);
  console.log('PASS: tool-generated delivery -> unified file cards, Sidebar image/text preview, cloud download, reload and mobile layout');
} finally { await browser.close(); }
