import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const output = resolve('node_modules/.tmp/personal-attention-actions');
await mkdir(output, { recursive: true });
await build({ entryPoints: ['scripts/personal-attention-actions-fixture.tsx'], outfile: resolve(output, 'fixture.js'),
  bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' }, loader: { '.css': 'empty' },
  alias: { '@hugagent/workbench-protocol': resolve('../../packages/workbench-protocol/dist/index.js') } });
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined });
try {
  const page = await browser.newPage();
  const errors = [];
  const calls = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('https://attention.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/fixture.js') return route.fulfill({ contentType: 'text/javascript', body: await readFile(resolve(output, 'fixture.js')) });
    if (url.pathname.includes('/v1/personal-agent/')) {
      const path = url.pathname.replace(/^.*\/v1\/personal-agent/, '');
      calls.push(route.request().method() + ' ' + path + url.search + ' ' + (route.request().postData() || ''));
      const data = path === '/search'
        ? { messages: [{ message_id: 'pa_msg_1', chat_id: 'pa_chat_channel_x', role: 'user', seq: 3, preview: '帮我订会议室', at: new Date().toISOString(), channel_type: 'lark', task_ids: ['blocked'] }],
            tasks: [{ id: 'blocked', goal: '订会议室', status: 'waiting' }] }
        : { status: 'pending' };
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 0, message: 'ok', data }) });
    }
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script type="module" src="/fixture.js"></script>' });
  });
  await page.goto('https://attention.test');
  const bar = page.getByTestId('pa-attention');
  await bar.waitFor();
  assert.match(await bar.locator('header').innerText(), /\(4\)/);
  // The question comes first and is answered inline as an update to its task.
  const first = bar.locator('.pa-attention-item').first();
  assert.match(await first.innerText(), /订会议室[\s\S]*订 3 楼还是 5 楼？/);
  await first.getByRole('textbox').fill('5 楼');
  await first.getByRole('button', { name: /^回\s?答$/ }).click();
  await page.waitForFunction(() => document.querySelector('[data-testid=refreshed]')?.textContent === '1');
  const answer = JSON.parse(calls.find(call => call.startsWith('POST /messages ')).split(' ').slice(2).join(' '));
  assert.deepEqual([answer.content, answer.intent, answer.work_item_ids], ['5 楼', 'update', ['blocked']]);
  // A failed message and an undelivered reply can be started again.
  await page.getByTestId('pa-attention-turn').getByRole('button', { name: '重新处理' }).click();
  await page.getByTestId('pa-attention-delivery').getByText('回复没有发到飞书').waitFor();
  await page.getByTestId('pa-attention-delivery').getByRole('button', { name: '重新发送' }).click();
  await page.waitForFunction(() => document.querySelector('[data-testid=refreshed]')?.textContent === '3');
  assert.ok(calls.some(call => call.startsWith('POST /messages/pa_msg_turn/reprocess')));
  assert.ok(calls.some(call => call.startsWith('POST /deliveries/pa_msg_reply/retry')));
  // Search by message ID shows the task and the message; picking the message opens its conversation.
  await page.getByRole('button', { name: '打开查找' }).click();
  await page.getByRole('searchbox').fill('om_abc');
  await page.getByRole('button', { name: /^查\s?找$/ }).click();
  await page.getByText('帮我订会议室').waitFor();
  await page.screenshot({ path: resolve(output, 'search.png') });
  assert.ok(calls.some(call => call.startsWith('GET /search?q=om_abc')));
  await page.getByRole('button', { name: /帮我订会议室/ }).click();
  assert.equal(await page.getByTestId('opened').innerText(), 'chat:pa_chat_channel_x');
  await page.screenshot({ path: resolve(output, 'attention.png'), fullPage: true });
  assert.equal(await page.getByTestId('errors').innerText(), '');
  assert.deepEqual(errors, []);
  console.log('PASS: questions are answered inline, failed messages and deliveries can run again, search opens the matching conversation.');
} finally { await browser.close(); }
