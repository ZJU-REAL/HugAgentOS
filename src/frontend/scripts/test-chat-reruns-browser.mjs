// Production chat routes, real configured model and browser persistence.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { writeFile } from 'node:fs/promises';

const base = 'http://127.0.0.1:' + (process.env.CORE_E2E_PORT || '38273');
const output = process.env.CORE_E2E_ROOT;
assert.ok(output?.startsWith('/tmp/'), 'Use disposable test data');
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(base + '/openapi.json');
  const result = await page.evaluate(async () => {
    const request = (path, body, method = 'POST') => fetch(path, {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const checked = async response => {
      if (!response.ok) throw new Error('HTTP ' + response.status + ': ' + await response.text());
      return response.json();
    };
    const stream = async response => {
      if (response.status !== 200) throw new Error('Stream HTTP ' + response.status + ': ' + await response.text());
      const wire = await response.text();
      const events = wire.split('\n').filter(x => x.startsWith('data: {')).map(x => JSON.parse(x.slice(6)));
      if (events.some(e => e.type === 'error')) throw new Error(JSON.stringify(events.filter(e => e.type === 'error')));
      if (!wire.includes('data: [DONE]')) throw new Error('Missing stream completion');
      return { text: events.filter(e => e.type === 'content').map(e => e.delta || '').join('').trim(),
        providerUsage: events.some(e => e.type === 'context_usage' && e.source === 'provider') };
    };
    await checked(await request('/__test/login/core-owner', {}));
    const title = 'Chat split E2E ' + Date.now();
    const chat = (await checked(await request('/api/v1/chats', { title }))).data.chat_id;
    const prompt = 'Reply with exactly CHAT_SPLIT_ORIGINAL. Do not use tools.';
    const initial = await stream(await request('/api/v1/agents/responses', {
      chat_id: chat, message: prompt, stream: true, enabled_skills: [], enabled_mcps: [],
      enabled_agents: [], memory_enabled: false, chat_mode: 'fast',
    }));
    const regenResponse = await request('/api/v1/chats/' + chat + '/regenerate', { message_index: 1 });
    const active = (await checked(await fetch('/api/v1/chats/' + chat + '/active-run'))).data;
    const regenerated = await stream(regenResponse);
    if (!active?.run_id) throw new Error('No active durable run exposed');
    const replay = await stream(await fetch('/api/v1/chats/stream/' + active.run_id + '?from=0'));
    const edited = await stream(await request('/api/v1/chats/' + chat + '/edit', {
      message_index: 0, new_content: 'Reply with exactly CHAT_SPLIT_EDITED. Do not use tools.',
    }));
    const history = (await checked(await fetch('/api/v1/chats/' + chat + '/messages'))).data;
    const order = (await checked(await request('/api/v1/chats/sidebar-order', { order: [chat, chat] }, 'PUT'))).data;
    const restoredOrder = (await checked(await fetch('/api/v1/chats/sidebar-order'))).data;
    await checked(await request('/__test/login/core-other', {}));
    const denied = [];
    for (const [path, body] of [
      ['/api/v1/chats/' + chat + '/regenerate', { message_index: 1 }],
      ['/api/v1/chats/' + chat + '/edit', { message_index: 0, new_content: 'unauthorized' }],
    ]) denied.push((await request(path, body)).status);
    denied.push((await fetch('/api/v1/chats/stream/' + active.run_id)).status);
    await checked(await request('/__test/login/core-owner', {}));
    const afterDenied = (await checked(await fetch('/api/v1/chats/' + chat + '/messages'))).data;
    return { chat, title, initial, regenerated, replay, edited, history, afterDenied, order, restoredOrder, denied };
  });
  assert.equal(result.initial.text, 'CHAT_SPLIT_ORIGINAL');
  assert.equal(result.regenerated.text, 'CHAT_SPLIT_ORIGINAL');
  assert.equal(result.replay.text, result.regenerated.text);
  assert.equal(result.edited.text, 'CHAT_SPLIT_EDITED');
  assert.ok(result.initial.providerUsage && result.regenerated.providerUsage && result.edited.providerUsage);
  assert.deepEqual(result.history, result.afterDenied);
  assert.deepEqual(result.order.order, [result.chat]);
  assert.deepEqual(result.order, result.restoredOrder);
  assert.ok(result.denied.every(status => [403, 404].includes(status)));
  await page.goto(base);
  await page.getByText(result.title, { exact: true }).first().click();
  await page.getByText('CHAT_SPLIT_EDITED', { exact: true }).first().waitFor();
  await page.reload();
  await page.getByText('CHAT_SPLIT_EDITED', { exact: true }).first().waitFor();
  await page.screenshot({ path: output + '/chat-reruns-browser.png', fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(output + '/chat-reruns-browser.json', JSON.stringify({
    initialRealModel: true, regenerateRealModel: true, editRealModel: true,
    completedRunReplay: true, sidebarOrder: true, crossUserWriteAndReplayDenied: true,
    historyUnchangedByDeniedRequests: true, browserReload: true, browserErrors: errors,
  }, null, 2));
  console.log('PASS: real model initial/regenerate/edit, durable replay, sidebar order, ownership, browser reload');
} finally { await browser.close(); }
