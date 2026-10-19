import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

const output = resolve('node_modules/.tmp/space-search-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { SpaceFileBrowser } from './src/components/myspace/SpaceFileBrowser';
    createRoot(document.getElementById('root')).render(<SpaceFileBrowser kind="personal" />);
  `, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
  loader: { '.css': 'empty', '.md': 'text', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [{ name: 'canvas-stub', setup(builder) {
    builder.onLoad({ filter: /[/\\]useSpaceCanvas\.tsx?$/ }, () => ({ contents: 'export function useSpaceCanvas() { return () => {}; }', loader: 'ts' }));
  } }],
});
const server = createServer(async (req, res) => {
  res.setHeader('Content-Type', req.url === '/fixture.js' ? 'text/javascript' : 'text/html');
  res.end(req.url === '/fixture.js' ? await readFile(resolve(output, 'fixture.js')) : '<div id="root"></div><script type="module" src="/fixture.js"></script>');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const root = { space_id: 'personal', name: 'Personal', kind: 'personal', permission: 'admin', mount_path: '/personal' };
  const entry = name => ({ name, path: name, is_directory: false, size: 4, modified_at: null });
  const calls = [];
  const cancelled = [];
  page.on('requestfailed', request => cancelled.push(new URL(request.url()).searchParams.toString()));
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const query = url.searchParams.get('keyword') || '';
    const cursor = url.searchParams.get('cursor');
    if (url.pathname.endsWith('/mounts')) return route.fulfill({ json: { data: { items: [root] } } });
    if (!url.pathname.endsWith('/entries')) return route.fulfill({ json: { data: {} } });
    calls.push({ query, cursor });
    if (query === 'slow' || (query === 'paging' && cursor)) await new Promise(resolve => setTimeout(resolve, 800));
    const entries = query === '你好' ? [entry(cursor ? '你好-2.txt' : '你好-1.txt')]
      : query === 'missing' ? [] : query ? [entry(query + '.txt')] : [entry('first-page.txt')];
    await route.fulfill({ json: { data: { space: root, path: '', entries, next_cursor: (!query || query === '你好' || query === 'paging') && !cursor ? 'next' : null } } });
  });
  await page.goto('http://127.0.0.1:' + server.address().port);
  await page.getByText('first-page.txt', { exact: true }).waitFor();
  const search = page.getByPlaceholder('搜索', { exact: true });
  await search.fill('你好');
  await page.getByText('你好-1.txt', { exact: true }).waitFor();
  assert.equal(calls.find(call => call.query === '你好').cursor, null, 'search starts on its first page');
  assert.equal(calls.filter(call => !call.query && call.cursor).length, 0, 'finding a file needs no manual directory pagination');
  await page.getByRole('button', { name: '加载更多', exact: true }).click();
  await page.getByText('你好-2.txt', { exact: true }).waitFor();
  assert.ok(calls.some(call => call.query === '你好' && call.cursor === 'next'), 'search pagination retains keyword');
  await search.fill('slow');
  await page.waitForRequest(request => new URL(request.url()).searchParams.get('keyword') === 'slow');
  await search.fill('new');
  await page.getByText('new.txt', { exact: true }).waitFor();
  await page.waitForTimeout(900);
  assert.equal(await page.getByText('slow.txt', { exact: true }).count(), 0, 'old search cannot replace new results');
  await search.fill('paging');
  await page.getByText('paging.txt', { exact: true }).waitFor();
  const pendingPage = page.waitForRequest(request => new URL(request.url()).searchParams.get('cursor') === 'next');
  await page.getByRole('button', { name: '加载更多', exact: true }).click();
  await pendingPage;
  await search.fill('after-paging');
  await page.getByText('after-paging.txt', { exact: true }).waitFor();
  assert.ok(cancelled.some(params => params.includes('keyword=paging') && params.includes('cursor=next')), 'changing keyword aborts pending pagination');
  await search.fill('missing');
  await page.getByText('当前文件夹暂无文件', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '加载更多', exact: true }).count(), 0);
  await search.fill('');
  await page.getByText('first-page.txt', { exact: true }).waitFor();
  assert.equal(calls.at(-1).cursor, null, 'clearing search resets directory pagination');
  assert.deepEqual(errors, []);
  console.log('PASS search without loading more, keyword pagination, stale response cancellation, empty results and clear search');
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
