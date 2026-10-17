import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const output = resolve('node_modules/.tmp/personal-attention');
await mkdir(output, { recursive: true });
await build({ entryPoints: ['scripts/personal-attention-fixture.tsx'], outfile: resolve(output, 'fixture.js'),
  bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') } });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('http://attention.test/**', async route => {
    if (route.request().url().endsWith('/fixture.js')) return route.fulfill({ contentType: 'text/javascript', body: await readFile(resolve(output, 'fixture.js')) });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script type="module" src="/fixture.js"></script>' });
  });
  await page.goto('http://attention.test');
  await page.getByRole('button', { name: '模拟结果未知' }).waitFor();
  assert.equal(await page.getByTestId('pa-attention').count(), 0);
  assert.equal(await page.getByText('请先核对操作结果', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '模拟自动等待电脑' }).click();
  assert.equal(await page.getByTestId('pa-attention').count(), 0);
  assert.equal(await page.getByText('请先核对操作结果', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '模拟结果未知' }).click();
  await page.getByTestId('pa-attention').waitFor();
  assert.equal(await page.getByTestId('pa-attention').getByRole('button', { name: '已执行', exact: true }).count(), 1);
  await page.getByRole('button', { name: '模拟执行中' }).click();
  await page.getByTestId('pa-attention').waitFor({ state: 'detached' });
  assert.equal(await page.getByRole('button', { name: '已执行', exact: true }).count(), 0);
  await page.getByRole('button', { name: '模拟执行成功' }).click();
  assert.equal(await page.getByTestId('pa-attention').count(), 0);
  assert.deepEqual(errors, []);
  console.log('PASS: running actions never request reconciliation in attention area or task dialog; waiting unknown results do; success clears prompts.');
} finally { await browser.close(); }
