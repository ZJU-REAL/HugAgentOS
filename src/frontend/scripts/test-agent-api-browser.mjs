import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const playwright = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { chromium } = playwright.default || playwright;
const output = resolve('node_modules/.tmp/agent-api-browser');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider, theme } from 'antd';
import { AgentDetailView } from './src/components/agent/AgentDetailView';
import './src/styles/variables.css';
import './src/styles/catalog.css';
const base = { agent_id: 'agent-a', name: 'Agent Alpha', avatar: '🤖', owner_type: 'user', user_id: 'owner', team_id: null, description: 'API access test agent', system_prompt: 'Test assistant', welcome_message: '', suggested_questions: [], skill_ids: [], plugin_ids: [], mcp_server_ids: [], kb_ids: [], max_iters: 10, extra_config: {}, version: 'V1', change_history: [], is_enabled: true };
function Fixture() {
 const [agent, setAgent] = useState(base);
 const [allowed, setAllowed] = useState(true);
 const [dark, setDark] = useState(false);
 return <ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}><nav>
   <button onClick={() => setAgent({ ...base, agent_id: 'agent-b', name: 'Agent Beta' })}>Switch agent</button>
   <button onClick={() => setAgent({ ...base, is_enabled: false })}>Disabled agent</button>
   <button onClick={() => setAllowed(false)}>No permission</button>
   <button onClick={() => { document.documentElement.dataset.theme = 'dark'; setDark(true); }}>Dark</button>
 </nav><AgentDetailView key={agent.agent_id} selectedAgent={agent} availableResources={null}
   colorIndex={0} canEdit={true} canAddAgent={false} canUseApiKey={allowed} channelBotEnabled={false}
   navDir={null} onBack={() => {}} onEdit={() => {}} startAgentChat={() => {}}
   handleExportAgent={async () => {}} handleToggleEnabled={async () => {}} handleDelete={() => {}}
   openSubmit={() => {}} submissionModal={null} /></ConfigProvider>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
`, resolveDir: process.cwd(), loader: 'tsx' }, outfile: resolve(output, 'fixture.js'),
bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
external: ['/loader.gif', '/loader-done.png'], loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' } });
let page;
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
try {
  page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const keys = new Map([['agent-a', []], ['agent-b', []]]);
  let failKeys = true, failCalls = true, created = 0, delayedCreate = false, releaseCreate;
  const calls = [];
  await page.route('https://agent.test/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname, method = route.request().method();
    if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({
      contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css', body: await readFile(resolve(output, path.slice(1))),
    });
    const match = path.match(/^\/api\/v1\/agents\/(agent-[ab])\/(api-keys|api-calls)(.*)$/);
    if (match) {
      const [, agentId, kind, suffix] = match;
      calls.push({ agentId, kind, suffix, method, query: url.search, body: route.request().postDataJSON() });
      if (kind === 'api-calls') {
        if (failCalls) { failCalls = false; return route.fulfill({ status: 503, json: { detail: 'Temporary call log failure' } }); }
        const pageNumber = Number(url.searchParams.get('page') || 1);
        const item = { id: 'call-' + pageNumber, key_id: 'key-1', key_name: 'Integration', key_prefix: 'sk-test', agent_id: agentId,
          chat_id: 'api-chat-' + pageNumber, run_id: 'run-' + pageNumber, stream: true, status: 'completed',
          http_status: 200, created_at: '2026-09-26T00:00:00Z', duration_ms: 1250, total_tokens: 15 };
        return route.fulfill({ json: { code: 10000, data: { items: [item], pagination: { page: pageNumber, page_size: 10, total_items: 11, total_pages: 2, has_previous: pageNumber > 1, has_next: pageNumber < 2 } } } });
      }
      if (method === 'GET' && suffix.endsWith('/reveal')) return route.fulfill({ json: { code: 10000, data: { api_key: 'fake-secret-revealed' } } });
      if (method === 'GET') {
        if (failKeys) { failKeys = false; return route.fulfill({ status: 503, json: { detail: 'Temporary key failure' } }); }
        return route.fulfill({ json: { code: 10000, data: { items: keys.get(agentId) } } });
      }
      if (method === 'POST') {
        created++;
        const row = { id: 'key-' + created, agent_id: agentId, key_prefix: 'sk-test', enabled: true, revealable: true,
          name: route.request().postDataJSON().name, created_at: '2026-09-26T00:00:00Z' };
        keys.get(agentId).push(row);
        if (delayedCreate) await new Promise(resolve => { releaseCreate = resolve; });
        return route.fulfill({ json: { code: 10000, data: { ...row, api_key: 'fake-secret-created-' + created } } }).catch(() => {});
      }
      if (method === 'PATCH') {
        const row = keys.get(agentId).find(key => suffix === '/' + key.id);
        row.enabled = route.request().postDataJSON().enabled;
        return route.fulfill({ json: { code: 10000, data: row } });
      }
      if (method === 'DELETE') {
        keys.set(agentId, keys.get(agentId).filter(key => suffix !== '/' + key.id));
        return route.fulfill({ json: { code: 10000, data: { revoked: true } } });
      }
    }
    if (path.startsWith('/api')) return route.fulfill({ json: { code: 10000, data: [] } });
    return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:14px Arial;color:var(--color-text);background:var(--color-bg-container)}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto('https://agent.test/');
  await page.getByRole('button', { name: 'API-Key', exact: true }).click();
  await page.getByText('Temporary key failure', { exact: false }).waitFor();
  await page.getByRole('button', { name: /重\s*试/ }).click();
  await page.getByText('还没有 API-Key', { exact: true }).waitFor();
  await page.getByRole('textbox', { name: '密钥名称', exact: true }).fill('Integration');
  await page.getByRole('button', { name: /新建 Key/ }).click();
  await page.getByRole('textbox', { name: '完整密钥', exact: true }).waitFor();
  assert.equal(await page.getByRole('textbox', { name: '完整密钥', exact: true }).inputValue(), 'fake-secret-created-1');
  await page.getByRole('button', { name: '我已保存', exact: true }).click();
  await page.getByRole('switch', { name: '启用密钥', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[role="switch"][aria-label="启用密钥"]').getAttribute('aria-checked') === 'false');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: 'https://agent.test' });
  await page.getByRole('button', { name: '复制完整密钥', exact: true }).click();
  await page.waitForFunction(async () => await navigator.clipboard.readText() === 'fake-secret-revealed');
  assert.ok(!(await page.evaluate(() => JSON.stringify(localStorage))).includes('fake-secret'));
  await page.screenshot({ path: resolve(output, 'keys.png'), fullPage: true });
  await page.getByRole('tab', { name: '调用方式', exact: true }).click();
  assert.ok((await page.locator('.jx-agentApi-code').innerText()).includes('/v1/agents/responses'));
  assert.ok((await page.locator('.jx-agentApi-code').innerText()).includes('"stream": true'));
  assert.ok((await page.locator('.jx-agentApi-code').innerText()).includes('"agent_id": "agent-a"'));
  await page.getByText('非流式 · stream: false', { exact: true }).click();
  assert.ok((await page.locator('.jx-agentApi-code').innerText()).includes('"stream": false'));
  await page.getByRole('tab', { name: 'Python', exact: true }).click();
  assert.ok((await page.locator('.jx-agentApi-code:visible').innerText()).includes('response.json()'));
  await page.screenshot({ path: resolve(output, 'guide.png'), fullPage: true });
  await page.getByRole('tab', { name: '调用记录', exact: true }).click();
  await page.getByText('Temporary call log failure', { exact: true }).waitFor();
  await page.getByRole('button', { name: /重\s*试/ }).click();
  await page.getByText('1.25 s', { exact: true }).waitFor();
  await page.locator('.ant-pagination-item-2').click();
  await page.waitForFunction(() => document.querySelector('.ant-pagination-item-2')?.classList.contains('ant-pagination-item-active'));
  await page.getByText('1.25 s', { exact: true }).waitFor();
  assert.ok(calls.some(call => call.query.includes('page=2')));
  await page.getByRole('combobox', { name: '筛选状态' }).click();
  await page.locator('.ant-select-item-option-content').getByText('已完成', { exact: true }).click();
  await page.getByText('1.25 s', { exact: true }).waitFor();
  assert.ok(calls.some(call => call.query.includes('status=completed') && call.query.includes('page=1')));
  await page.getByText('「Agent Alpha」的 API-Key', { exact: true }).click();
  await page.locator('.ant-message-notice').first().waitFor({ state: 'hidden' });
  await page.screenshot({ path: resolve(output, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.screenshot({ path: resolve(output, 'mobile.png'), fullPage: true });
  const box = await page.getByRole('dialog').boundingBox();
  assert.ok(box.x >= 0 && box.x + box.width <= 375, 'Dialog fits narrow screens');
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'Dark', exact: true }).click();
  await page.getByRole('button', { name: 'API-Key', exact: true }).click();
  await page.getByRole('button', { name: /撤\s*销/ }).click();
  await page.getByRole('button', { name: /撤\s*销/ }).last().click();
  await page.getByText('还没有 API-Key', { exact: true }).waitFor();
  await page.screenshot({ path: resolve(output, 'dark.png'), fullPage: true });
  delayedCreate = true;
  await page.getByRole('button', { name: /新建 Key/ }).click();
  await page.waitForFunction(() => !!document.querySelector('.jx-agentApi-create .ant-btn-loading'));
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  releaseCreate();
  await page.getByRole('button', { name: 'Switch agent', exact: true }).click();
  await page.getByRole('button', { name: 'API-Key', exact: true }).click();
  await page.getByText('还没有 API-Key', { exact: true }).waitFor();
  assert.equal(await page.getByRole('textbox', { name: '完整密钥', exact: true }).count(), 0, 'Late creation cannot leak plaintext into a new agent dialog');
  assert.ok(calls.some(call => call.agentId === 'agent-b'));
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'Disabled agent', exact: true }).click();
  await page.getByRole('button', { name: 'API-Key', exact: true }).click();
  assert.equal(await page.getByRole('button', { name: /新建 Key/ }).isDisabled(), false);
  await page.getByText('此智能体已停用，但仍可通过 API 单独调用。').waitFor();
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'No permission', exact: true }).click();
  assert.equal(await page.getByRole('button', { name: 'API-Key', exact: true }).count(), 0);
  assert.deepEqual(errors, []);
  console.log('PASS: real detail toolbar, key failure/retry/create/toggle/revoke, examples, call retry/filter/page, secret cleanup, agent isolation, permission/disabled states, 375px and dark screenshots');
} catch (error) {
  if (page) {
    console.error(await page.locator('body').innerText());
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true });
  }
  throw error;
} finally { await browser.close(); }
