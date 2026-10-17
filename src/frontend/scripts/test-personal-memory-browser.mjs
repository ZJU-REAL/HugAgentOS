import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const output = resolve('node_modules/.tmp/personal-memory');
await mkdir(output, { recursive: true });
await build({ entryPoints: ['scripts/personal-memory-fixture.tsx'], outfile: resolve(output, 'fixture.js'), bundle: true,
  loader: { '.woff2': 'dataurl', '.woff': 'dataurl', '.ttf': 'dataurl' }, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') } });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const requests = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://memory.test/**', async route => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({ contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css', body: await readFile(resolve(output, path.slice(1))) });
    if (path.endsWith('/dreaming')) return route.fulfill({ json: { code: 0, data: { enabled: true, hour: 3, timezone: 'Asia/Shanghai', status: 'done', attempts: 1, next_run: 1792306800 } } });
    if (path.endsWith('/dreaming/run')) return route.fulfill({ json: { code: 0, data: { enabled: true, hour: 3, timezone: 'Asia/Shanghai', status: 'pending', attempts: 0 } } });
    if (path.endsWith('/workspace/file')) return route.fulfill({ json: { code: 0, data: { content: '# Dreaming\nVerified consolidation report', truncated: false } } });
    if (path.endsWith('/settings')) return route.fulfill({ json: { code: 0, data: {} } });
    if (path.includes('/capabilities')) return route.fulfill({ json: { code: 0, data: { skill_ids: [], mcp_ids: [], plugins: [] } } });
    if (path.includes('/catalog')) return route.fulfill({ json: { code: 0, data: { skills: [], mcp: [], agents: [], kb: [] } } });
    if (path.includes('/api/')) return route.fulfill({ json: { code: 0, data: { connected: false, items: [] } } });
    return route.fulfill({ contentType: 'text/html', body: '<link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>' });
  });
  await page.goto('http://memory.test');
  await page.getByText('长期记忆 · MEMORY.md', { exact: true }).waitFor();
  await page.getByText('用户偏好 · USER.md', { exact: true }).waitFor();
  await page.getByText('每日日志 · memory/YYYY-MM-DD.md', { exact: true }).waitFor();
  await page.getByText('待写入日志：2 条', { exact: true }).waitFor();
  assert.equal(await page.getByText('自动记住新信息', { exact: true }).count(), 0);
  assert.equal(await page.getByText('我的分层记忆', { exact: true }).count(), 0);
  assert.equal(requests.some(path => path.includes('/memories')), false);
  await page.getByText('Dreaming · 定期整理', { exact: true }).waitFor();
  await page.getByRole('button', { name: '查看 Dreaming 报告' }).click();
  await page.getByText('Verified consolidation report', { exact: false }).waitFor();
  await page.getByRole('button', { name: 'Close', exact: true }).last().click();
  await page.getByRole('button', { name: '立即整理', exact: true }).click();
  await page.getByText('等待整理', { exact: true }).waitFor();
  assert.equal(requests.some(path => path.endsWith('/dreaming/run')), true);
  await page.getByRole('button', { name: '查看和编辑记忆' }).scrollIntoViewIfNeeded();
  await page.getByRole('button', { name: '查看和编辑记忆' }).screenshot({ path: resolve(output, 'memory-entry.png'), animations: 'disabled' });
  await page.screenshot({ animations: 'disabled', path: resolve(output, 'settings.png'), fullPage: true });
  await page.getByRole('button', { name: '查看和编辑记忆' }).click();
  assert.equal(await page.evaluate(() => document.body.dataset.closed), 'true');
  assert.match(page.url(), /my-space/);
  assert.deepEqual(errors, []);
  console.log('PASS: actual Personal Agent settings show files and pending journals, open workspace, never request layered memory');
} finally { await browser.close(); }
