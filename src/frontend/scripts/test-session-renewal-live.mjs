import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { createServer, request as httpRequest } from 'node:http';

// Keep real admission headers immediate while delaying only its real SSE bytes.
// This reproduces a slow response without depending on a model/tool running for 31s.
async function delayedResponseProxy(upstreamBase) {
  const timers = new Set();
  const proxy = createServer((request, response) => {
    const target = new URL(request.url, upstreamBase);
    const upstream = httpRequest(target, { method: request.method, headers: { ...request.headers, host: target.host } }, incoming => {
      response.writeHead(incoming.statusCode, incoming.headers);
      if (target.pathname.endsWith('/v1/agents/responses') && incoming.statusCode === 200) {
        response.flushHeaders();
        const chunks = [];
        incoming.on('data', chunk => chunks.push(chunk));
        incoming.on('end', () => {
          const timer = setTimeout(() => { timers.delete(timer); response.end(Buffer.concat(chunks)); }, 45000);
          timers.add(timer);
        });
      } else incoming.pipe(response);
    });
    upstream.on('error', error => response.destroy(error));
    request.pipe(upstream);
  });
  await new Promise(done => proxy.listen(0, '127.0.0.1', done));
  return {
    base: `http://127.0.0.1:${proxy.address().port}`,
    async close() {
      for (const timer of timers) clearTimeout(timer);
      proxy.closeAllConnections();
      await new Promise(done => proxy.close(done));
    },
  };
}

// Authorized local stack only. Identity contains a short-lived, isolated test session.
const [identityPath, base = 'http://127.0.0.1:3000', outputPath = '../../.scratch/copy-message-fix/e2e'] = process.argv.slice(2);
assert.ok(identityPath, 'Pass a protected local test identity file');
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'Local acceptance only');
const identity = JSON.parse(await readFile(resolve(identityPath), 'utf8'));
const output = resolve(outputPath);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const results = [];
const scenarios = [
  { name: 'early-copy', copy: true },
  { name: 'renew-copy', copy: true, renew: true },
  { name: 'renew-click', renew: true },
  { name: 'renew-keyboard', renew: true, keyboard: true },
  { name: 'renew-copy-fallback', copy: true, renew: true, fallback: true },
  { name: 'streaming-history-reload', copy: true, renew: true, history: true },
  { name: 'real-31-second-copy', copy: true, renew: true, realWait: true },
  { name: 'identical-submissions', repeat: true },
];
try {
  for (const scenario of scenarios.filter(item => !process.env.SESSION_RENEWAL_SCENARIO || item.name === process.env.SESSION_RENEWAL_SCENARIO)) {
    const delayed = scenario.realWait ? await delayedResponseProxy(base) : null;
    const scenarioBase = delayed?.base ?? base;
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, permissions: ['clipboard-read', 'clipboard-write'] });
    await context.addCookies([{ name: identity.cookie_name, value: identity.token, url: scenarioBase, httpOnly: true, sameSite: 'Lax' }]);
    await context.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
    if (scenario.fallback) await context.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { value: undefined }));
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    if (!scenario.realWait) await page.clock.install();
    const requests = [], errors = [], renewals = [], acknowledgments = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.url().includes('/v1/') && (request.url().includes('/messages?') || request.url().includes('/session/check') || request.url().endsWith('/responses'))) {
        requests.push({ method: request.method(), path: new URL(request.url()).pathname, at: Date.now() });
      }
    });
    page.on('response', async response => {
      if (response.url().endsWith('/v1/auth/session/check')) {
        const body = await response.json().catch(() => null);
        renewals.push(body?.data?.expires_at);
      }
    });
    let chat;
    const users = () => page.locator('.jx-msg.user').evaluateAll(nodes => nodes.map(node => ({ uid: node.dataset.messageUid, text: node.innerText })));
    const historyCount = () => requests.filter(request => request.path.endsWith('/messages')).length;
    try {
      await page.goto(scenarioBase);
      const editor = page.locator('.jx-composerEditor');
      await editor.waitFor({ timeout: 45000 });
      const text = (scenario.repeat || scenario.realWait) ? '请仅回复：相同消息测试通过。不要调用工具。' : '请用 Bash 执行 sleep 45; printf COPY_RENEWAL_DONE。只执行一次这条测试命令，等待完成后报告输出。';
      async function send() {
        const admitted = page.waitForResponse(response => response.url().endsWith('/v1/agents/responses'));
        await editor.fill(text);
        await editor.press('Enter');
        const response = await admitted;
        assert.equal(response.status(), 200);
        chat = new URL(page.url()).pathname.split('/c/')[1];
        const id = response.headers()['x-hugagent-user-message-id'];
        assert.ok(id, 'Admission must acknowledge the persisted user message before SSE');
        acknowledgments.push(id);
        return response;
      }
      await send();
      await page.locator('.jx-msg.assistant').first().waitFor();
      chat = new URL(page.url()).pathname.split('/c/')[1];
      assert.ok(chat);
      await page.waitForTimeout(500);
      assert.equal((await users()).length, 1);
      const beforeHistory = historyCount();
      if (scenario.renew) {
        if (scenario.realWait) await page.waitForTimeout(31000);
        else await page.clock.fastForward(31000);
        await page.locator('.jx-sendBtn[aria-label="中止"]').waitFor();
        const renewed = page.waitForResponse(item => item.url().endsWith('/v1/auth/session/check'));
        await page.locator('.jx-msg.user').first().hover();
        if (scenario.copy) {
          await page.locator('.jx-msg.user button[title="复制内容"]').first().click();
          await page.locator('.jx-msg.user button[title="已复制"]').first().waitFor();
        } else if (scenario.keyboard) await editor.press('ArrowLeft');
        else await page.locator('.jx-msg.user .jx-bubble').first().click();
        await renewed;
        await page.waitForTimeout(800);
        assert.ok(renewals.length >= 2);
        assert.notEqual(renewals[0], renewals.at(-1), 'Expiry must still renew');
        assert.equal(historyCount(), beforeHistory, 'Renewal alone must not reload history');
      } else if (scenario.copy) {
        await page.locator('.jx-msg.user').first().hover();
        await page.locator('.jx-msg.user button[title="复制内容"]').first().click();
        await page.locator('.jx-msg.user button[title="已复制"]').first().waitFor();
      }
      if (scenario.history) {
        await page.locator('.jx-newChatBtn').click();
        await page.goBack();
        await page.locator('.jx-msg.user').first().waitFor();
        await page.waitForTimeout(800);
        assert.ok(historyCount() > beforeHistory, 'Must exercise real history hydration during streaming');
      }
      if (scenario.repeat) {
        // SSE response.finished() can remain pending after the client stops
        // reading at its terminal frame. Wait for the actual composer state.
        await page.locator('.jx-sendBtn[aria-label="发送"]').waitFor({ timeout: 120000 });
        await send();
        await page.locator('.jx-sendBtn[aria-label="发送"]').waitFor({ timeout: 120000 });
        await page.waitForTimeout(500);
        assert.notEqual(acknowledgments[0], acknowledgments[1]);
      }
      const expected = scenario.repeat ? 2 : 1;
      assert.equal((await users()).length, expected, 'User message bubbles must not duplicate');
      const history = await context.request.get(`${scenarioBase}/api/v1/chats/${chat}/messages?page_size=100`);
      assert.ok(history.ok());
      const rows = (await history.json()).data.items.filter(item => item.role === 'user');
      assert.equal(rows.length, expected);
      assert.deepEqual(new Set(rows.map(item => item.message_id)), new Set(acknowledgments));
      assert.equal(requests.filter(item => item.method === 'POST' && item.path.endsWith('/responses')).length, expected);
      assert.deepEqual(errors, []);
      const result = { scenario: scenario.name, chat, uiUsers: (await users()).length, serverUsers: rows.length, acknowledgments, renewals, requests, errors, realWait: !!scenario.realWait, delayedResponseBody: !!delayed };
      await writeFile(resolve(output, `${scenario.name}.json`), JSON.stringify(result, null, 2));
      await page.screenshot({ path: resolve(output, `${scenario.name}.png`), fullPage: true });
      results.push({ scenario: scenario.name, result: 'passed', uiUsers: expected, serverUsers: rows.length });
      console.log(JSON.stringify(results.at(-1)));
    } catch (error) {
      await page.screenshot({ path: resolve(output, `${scenario.name}-failure.png`), fullPage: true });
      await writeFile(resolve(output, `${scenario.name}-failure.json`), JSON.stringify({ chat, acknowledgments, requests, renewals, errors, users: await users(), failure: String(error) }, null, 2));
      throw error;
    } finally {
      if (chat) {
        const commands = await context.request.get(`${scenarioBase}/api/v1/commands?chat_id=${encodeURIComponent(chat)}`);
        for (const command of commands.ok() ? (await commands.json()).data : []) {
          if (['starting', 'running'].includes(command.status)) {
            const cancelled = await context.request.post(`${scenarioBase}/api/v1/commands/${command.task_id}/cancel`);
            assert.ok(cancelled.ok(), 'Test command must be stopped');
          }
        }
        const current = await context.request.get(`${scenarioBase}/api/v1/chats/${chat}/active-run`);
        const active = current.ok() ? (await current.json()).data : null;
        if (active?.run_id && ['running', 'pending'].includes(active.status)) {
          const cancelled = await context.request.post(`${scenarioBase}/api/v1/chat-runs/${active.run_id}/cancel`);
          assert.ok(cancelled.ok(), 'Test run must be stopped');
        }
      }
      await context.close();
      await delayed?.close();
    }
  }
} finally {
  await writeFile(resolve(output, 'summary.json'), JSON.stringify(results, null, 2));
  await browser.close();
}
