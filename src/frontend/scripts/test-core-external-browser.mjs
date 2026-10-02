// Opt-in real model E2E against the isolated core_external_server host.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { writeFile } from 'node:fs/promises';

const base = 'http://127.0.0.1:' + (process.env.CORE_E2E_PORT || '38273');
const output = process.env.CORE_E2E_ROOT;
assert.ok(output?.startsWith('/tmp/'), 'Use a disposable result directory');
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/openapi.json');
  const result = await page.evaluate(async () => {
    const post = (path, body) => fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    await post('/__test/login/core-owner', {});
    const title = 'Real model verification ' + Date.now();
    const created = await post('/api/v1/chats', { title });
    const chat = (await created.json()).data.chat_id;
    const response = await post('/api/v1/agents/responses', {
      chat_id: chat, message: 'Reply with exactly CORE_EXTERNAL_OK. Do not use tools.',
      stream: true, enabled_skills: [], enabled_mcps: [], enabled_agents: [],
      memory_enabled: false, chat_mode: 'fast',
    });
    const wire = await response.text();
    const events = wire.split('\n').filter(x => x.startsWith('data: {'))
      .map(x => JSON.parse(x.slice(6)));
    const text = events.filter(x => x.type === 'content').map(x => x.delta || '').join('');
    return { chat, title, status: response.status, text, events, done: wire.includes('data: [DONE]') };
  });
  assert.equal(result.status, 200);
  assert.equal(result.text.trim(), 'CORE_EXTERNAL_OK');
  assert.equal(result.done, true);
  assert.equal(result.events.some(e => e.type === 'error'), false);
  assert.ok(result.events.some(e => e.type === 'context_usage' && e.source === 'provider'));
  await page.goto(base);
  await page.getByText(result.title, { exact: true }).first().click();
  await page.getByText('CORE_EXTERNAL_OK', { exact: true }).first().waitFor();
  await page.reload();
  await page.getByText('CORE_EXTERNAL_OK', { exact: true }).first().waitFor();
  await page.screenshot({ path: output + '/real-model-browser.png', fullPage: true });
  const denied = await page.evaluate(async chat => {
    await fetch('/__test/login/core-other', { method: 'POST' });
    return (await fetch('/api/v1/chats/' + chat + '/messages')).status;
  }, result.chat);
  assert.ok([403, 404].includes(denied));
  assert.deepEqual(errors, []);
  await writeFile(output + '/real-model-browser.json', JSON.stringify({
    stream: true, externalProviderUsage: true, persistedHistory: true,
    browserReload: true, crossUserDenied: true, browserErrors: errors,
  }, null, 2));
  console.log('PASS: real external model SSE, persisted UI, reload, cross-user isolation');
} finally {
  await browser.close();
}
