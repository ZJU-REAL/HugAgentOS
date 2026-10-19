import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { chromium } from 'playwright';

const identityPath = resolve(process.argv[2]);
const identity = JSON.parse(await readFile(identityPath, 'utf8'));
const info = identity.chats.idle;
const output = dirname(identityPath);
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addCookies([{ name: identity.cookie_name, value: identity.token,
    url: 'http://127.0.0.1:3000', httpOnly: true, sameSite: 'Lax' }]);
  await context.addInitScript(() => {
    localStorage.setItem('jx_lang', 'zh-CN');
    window.pushEvidence = { notices: [], subscriptions: [], taskPolls: 0 };
    const original = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input.url;
      const stream = url.match(/\/chats\/stream\/([^/]+)\/subscription/);
      if (stream) window.pushEvidence.subscriptions.push({ runId: stream[1], time: performance.now() });
      if (/\/v1\/commands\?/.test(url)) window.pushEvidence.taskPolls++;
      const response = await original(input, init);
      if (!url.includes('/v1/commands/events?') || !response.ok || !response.body) return response;
      const decoder = new TextDecoder();
      let buffer = '';
      const body = response.body.pipeThrough(new TransformStream({
        transform(chunk, controller) {
          buffer += decoder.decode(chunk, { stream: true });
          const frames = buffer.split('\n\n');
          buffer = frames.pop() || '';
          for (const frame of frames) {
            const line = frame.split('\n').find(value => value.startsWith('data:'));
            if (!line) continue;
            const event = JSON.parse(line.slice(5));
            if (event.type === 'command_runs') {
              event.run_ids.forEach(runId => window.pushEvidence.notices.push({ runId, time: performance.now() }));
            }
          }
          controller.enqueue(chunk);
        },
      }));
      return new Response(body, { status: response.status, headers: response.headers });
    };
  });
  const page = await context.newPage();
  const telemetry = setInterval(async () => {
    try { await writeFile(resolve(output, 'observer-state.json'), JSON.stringify(await page.evaluate(() => window.pushEvidence))); } catch { /* page can close during collection */ }
  }, 5000);
  page.once('close', () => clearInterval(telemetry));
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:3000/c/' + info.chat_id);
  await page.locator('.jx-msg.assistant').first().waitFor({ timeout: 45000 });
  await page.waitForFunction(marker =>
    [...document.querySelectorAll('.jx-msg.assistant')].some(row => row.textContent.includes(marker)),
  'FINISH_' + info.marker, { timeout: 600000 });
  const evidence = await page.evaluate(() => window.pushEvidence);
  const measured = evidence.notices.flatMap(notice => {
    const subscription = evidence.subscriptions.find(item => item.runId === notice.runId);
    return subscription ? [{ runId: notice.runId, delayMs: subscription.time - notice.time }] : [];
  });
  assert.ok(measured.length, JSON.stringify(evidence));
  assert.ok(measured.every(item => item.delayMs >= -500 && item.delayMs < 1500), JSON.stringify(measured));
  assert.equal(evidence.taskPolls, 0);
  assert.equal(await page.getByRole('region', { name: '后台命令' }).count(), 0);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: resolve(output, 'push-live.png'), fullPage: true });
  const result = { result: 'passed', chatId: info.chat_id, measured, taskPolls: evidence.taskPolls };
  await writeFile(resolve(output, 'browser-evidence.json'), JSON.stringify({ ...result, evidence }, null, 2));
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
