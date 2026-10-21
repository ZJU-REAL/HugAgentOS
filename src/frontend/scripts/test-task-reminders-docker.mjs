import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';

// Load the deployed bundle; only scenario API responses are simulated.
// No model calls or changes to real users, questions, chats, or run verdicts.
const origin = process.env.E2E_BASE_URL || 'http://127.0.0.1:3000';
const output = resolve('../../.scratch/yellow-reminder-docker');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.setDefaultTimeout(25000);
const errors = [], assets = [], checks = [];
page.on('pageerror', error => errors.push(error.message));
page.on('response', response => {
  if (new URL(response.url()).pathname.startsWith('/assets/') && response.ok()) assets.push(response.url());
});
const chatId = 'yellow-reminder-e2e';
const user = { user_id: 'yellow-e2e-user', username: 'E2E tester', allowed_apps: null };
const now = new Date().toISOString();
const session = { chat_id: chatId, title: 'Yellow reminder E2E', created_at: now, updated_at: now, metadata: {}, messages: [] };
let pending = false, serial = 0, answered = null, cancelled = 0, statusPolls = 0;
function question() {
  return { request_id: 'question-' + serial, created_at: Date.now() / 1000,
    expires_at: Date.now() / 1000 + 3600,
    questions: [{ id: 'q', question: '请选择测试方案', options: [
      { id: 'a', label: '方案甲' }, { id: 'b', label: '方案乙' },
    ] }] };
}
const yellow = page.locator('.jx-historyItem [data-task-status="waiting"]');
const panel = page.locator('.jx-askQ');
async function absent(label) {
  await panel.waitFor({ state: 'detached' });
  await page.waitForFunction(() => !document.querySelector('.jx-historyItem [data-task-status="waiting"]'));
  checks.push(label);
}
async function present(label) {
  await panel.waitFor({ state: 'visible' });
  await yellow.waitFor({ state: 'visible' });
  checks.push(label);
}
try {
  for (const name of ['health', 'ready']) {
    const response = await page.request.get(origin + '/api/' + name);
    assert.equal(response.status(), 200, name + ' from the real Docker backend');
  }
  await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
  await page.route(origin + '/api/**', async route => {
    const path = new URL(route.request().url()).pathname.slice(4);
    let data = [];
    if (path === '/v1/auth/session/check' || path === '/v1/me') data = user;
    else if (path.endsWith('/preferences')) data = { sidebar_layout: 'modern' };
    else if (path === '/v1/meta/edition') data = { edition: 'ee', features: { multi_tenancy: true } };
    else if (path === '/v1/workbench/catalog') data = { origin: { kind: 'cloud', id: 'a'.repeat(32) }, revision: 'e2e', policy: { revision: 0, defaultPinned: [], forced: [] }, items: [
      ...['chat', 'projects', 'my_space', 'ability_center', 'app_center', 'automation', 'settings', 'docs'].map(id => ({
        applicationKey: 'builtin:' + id, revision: 1, enabled: true,
        declaration: { schemaVersion: '1.0.0', id, title: id, target: { kind: 'builtin', componentId: id } },
        availability: { available: true, reason: null }, navigation: { defaultPinned: false, forced: false },
      })),
    ] };
    else if (path === '/v1/catalog') data = { skills: [], agents: [], mcp: [], kb: [] };
    else if (path === '/v1/content/page-config' || path === '/v1/content/app-config') data = {};
    else if (path === '/v1/content/docs') data = { updates: [], capabilities: [] };
    else if (path === '/v1/chats') data = { items: [session], pagination: { page: 1, total_items: 1, has_next: false } };
    else if (path === '/v1/chats/active-runs') {
      statusPolls++;
      data = { items: [], recent_chat_ids: [], statuses: [{
        chat_id: chatId, run_id: 'paused-run', message_id: 'answer', status: 'needs_attention',
        created_at: now, updated_at: now,
      }] };
    } else if (path === '/v1/chats/pending-user-questions') data = { items: pending ? [{ chat_id: chatId, ...question() }] : [] };
    else if (path.endsWith('/pending-user-questions')) data = { requests: pending ? [question()] : [] };
    else if (path === '/v1/chats/pending-confirms') data = { items: [] };
    else if (path.endsWith('/pending-confirm')) data = { confirms: [], design_pick: null };
    else if (path.endsWith('/user-questions/question-' + serial + '/answer')) {
      answered = route.request().postDataJSON(); pending = false; data = { ok: true, outcome: 'answered' };
    } else if (path.endsWith('/user-questions/question-' + serial + '/cancel')) {
      cancelled++; pending = false; data = { ok: true, outcome: 'cancelled' };
    } else if (path.endsWith('/messages')) data = { items: [{
      message_id: 'answer', role: 'assistant', content: '需要确认一个选项，请问：', created_at: now, chat_seq: 1,
    }], pagination: { has_next: false } };
    else if (path === '/v1/chats/' + chatId) data = session;
    else if (path.endsWith('/active-run')) data = { active: false };
    else if (path === '/v1/projects') data = { items: [], pagination: { has_next: false } };
    else if (path === '/v1/plugins/installed') data = { items: [] };
    return route.fulfill({ json: { code: 10000, data } });
  });

  await page.goto(origin + '/c/' + chatId);
  await page.getByText('Yellow reminder E2E', { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('.jx-composerEditor'));
  assert.ok(statusPolls > 0, 'the paused backend verdict was consumed');
  await absent('backend needs_attention without question: no yellow');
  await page.screenshot({ path: resolve(output, '01-paused-without-question.png') });

  pending = true; serial++;
  await page.reload();
  await present('refresh restores actionable question and yellow together');
  await page.screenshot({ path: resolve(output, '02-question-with-yellow.png') });
  await page.getByRole('radio', { name: /方案甲/ }).click();
  await page.getByRole('button', { name: '提交回答', exact: true }).click();
  await absent('answer submission removes question and yellow');
  assert.equal(answered.answers[0].selected[0], 'a');

  await page.reload();
  await absent('cached waiting verdict stays unlit after reload');

  pending = true; serial++;
  await page.reload();
  await present('new question lights yellow again');
  await page.getByRole('button', { name: '取消本次提问', exact: true }).click();
  await absent('cancelling removes question and yellow');
  assert.equal(cancelled, 1);

  pending = true; serial++;
  await page.reload();
  await present('pending question before expiry');
  pending = false;
  await panel.waitFor({ state: 'detached', timeout: 20000 });
  await absent('authoritative expiry poll removes question and yellow');
  await page.screenshot({ path: resolve(output, '03-expired-no-yellow.png') });
  assert.ok(assets.some(url => url.endsWith('.js')), 'actual Docker JavaScript assets loaded');
  assert.equal(errors.length, 0, errors.join('; '));
  await writeFile(resolve(output, 'result.json'), JSON.stringify({
    baseUrl: origin, checks, statusPolls, assetCount: assets.length, browserErrors: errors,
    backendChecks: ['health=200', 'ready=200'], apiScenarios: 'simulated; deployed HTML/JS/CSS unchanged',
  }, null, 2));
  console.log('PASS Docker deployed application:', checks.join('; '));
  console.log('Evidence:', output);
} catch (error) {
  await page.screenshot({ path: resolve(output, 'failure.png') }).catch(() => {});
  console.error('PAGE:', (await page.locator('body').innerText()).slice(0, 5000));
  throw error;
} finally {
  await browser.close();
}
