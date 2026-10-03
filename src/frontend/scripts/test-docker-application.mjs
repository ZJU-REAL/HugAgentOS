// Opt-in E2E against the actual Compose frontend/backend/PostgreSQL/Redis stack.
// Supply two disposable local accounts in an owner-only JSON file.
import assert from 'node:assert/strict';
import { readFile, writeFile, stat } from 'node:fs/promises';
import { chromium } from 'playwright';

const base = process.env.DOCKER_E2E_URL || 'http://127.0.0.1:3000';
const root = process.env.DOCKER_E2E_ROOT;
assert.ok(root?.startsWith('/tmp/'));
const credentials = root + '/accounts.json';
assert.equal((await stat(credentials)).mode & 0o077, 0);
const accounts = JSON.parse(await readFile(credentials, 'utf8'));
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
});
const result = { checks: [], createdChats: [], createdFiles: [], createdAgents: [], createdPlugins: [] };
const errors = [];
const contexts = [];
async function login(account) {
  const context = await browser.newContext();
  contexts.push(context);
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(base + '/login?redirect=' + encodeURIComponent(base + '/'));
  await page.locator('#username').fill(account.username);
  await page.locator('#password').fill(account.password);
  await page.locator('button[type="submit"]').filter({ hasText: /登录|Log in/ }).first().click();
  await page.waitForFunction(async () => {
    const r = await fetch('/api/v1/auth/session/check');
    if (!r.ok || !r.headers.get('content-type')?.includes('application/json')) return false;
    const body = await r.json();
    return Boolean((body.data || body).user_id);
  }, undefined, { timeout: 30000 });
  return page;
}
async function call(page, path, body, method = body === undefined ? 'GET' : 'POST') {
  return page.evaluate(async ({ path, body, method }) => {
    const r = await fetch('/api' + path, { method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await r.text(); let data;
    try { data = JSON.parse(text); } catch { data = text; }
    return { status: r.status, data };
  }, { path, body, method });
}
async function stream(page, path, body) {
  const response = await call(page, path, body);
  assert.equal(response.status, 200, JSON.stringify(response.data));
  const wire = response.data;
  assert.equal(typeof wire, 'string');
  assert.ok(wire.includes('data: [DONE]'));
  const events = wire.split('\n').filter(x => x.startsWith('data: {')).map(x => JSON.parse(x.slice(6)));
  assert.deepEqual(events.filter(e => e.type === 'error'), []);
  return { text: events.filter(e => e.type === 'content').map(e => e.delta || '').join('').trim(), events };
}
let owner;
try {
  owner = await login(accounts[0]);
  const other = await login(accounts[1]);
  result.checks.push('Real browser password login for two isolated users');
  for (const path of ['/health', '/ready', '/v1/catalog', '/v1/memories/settings', '/v1/chats/active-runs']) {
    assert.equal((await call(owner, path)).status, 200, path);
  }
  result.checks.push('Health/readiness/catalog/memory/active-run endpoints');
  // A interrupted earlier attempt can leave this synthetic upload behind.
  const leftovers = await call(owner, '/v1/artifacts');
  for (const item of leftovers.data.data?.items || []) {
    if (item.name === 'docker-e2e.txt') await call(owner, `/v1/artifacts/${item.id}`, undefined, 'DELETE');
  }
  const title = 'Docker apply E2E ' + Date.now();
  const created = await call(owner, '/v1/chats', { title });
  assert.equal(created.status, 201);
  const chat = created.data.data.chat_id;
  result.createdChats.push(chat);
  const common = { chat_id: chat, stream: true, enabled_skills: [], enabled_mcps: [],
    enabled_agents: [], memory_enabled: false, chat_mode: 'fast' };
  const initial = await stream(owner, '/v1/agents/responses', {
    ...common, message: 'Reply exactly DOCKER_ORIGINAL_OK. Do not use tools.' });
  assert.equal(initial.text, 'DOCKER_ORIGINAL_OK');
  const regenerated = await stream(owner, `/v1/chats/${chat}/regenerate`, { message_index: 1 });
  assert.equal(regenerated.text, 'DOCKER_ORIGINAL_OK');
  const run = regenerated.events.find(e => e.type === 'run_started');
  assert.ok(run?.run_id);
  const replay = await stream(owner, `/v1/chats/stream/${run.run_id}?from=0`);
  assert.equal(replay.text, regenerated.text);
  const edited = await stream(owner, `/v1/chats/${chat}/edit`, {
    message_index: 0, new_content: 'Reply exactly DOCKER_EDITED_OK. Do not use tools.' });
  assert.equal(edited.text, 'DOCKER_EDITED_OK');
  assert.ok(initial.events.some(e => e.type === 'context_usage' && e.source === 'provider'));
  result.checks.push('Real model initial/regenerate/edit and Redis durable SSE replay');
  const history = await call(owner, `/v1/chats/${chat}/messages`);
  for (const path of [`/v1/chats/${chat}/messages`, `/v1/chats/stream/${run.run_id}`]) {
    assert.ok([403, 404].includes((await call(other, path)).status));
  }
  for (const [action, body] of [['regenerate', { message_index: 1 }], ['edit', { message_index: 0, new_content: 'forbidden' }]]) {
    assert.ok([403, 404].includes((await call(other, `/v1/chats/${chat}/${action}`, body)).status));
  }
  const after = await call(owner, `/v1/chats/${chat}/messages`);
  // Background evolution settlement may enrich metadata after SSE completion.
  // Compare the authoritative message identity/order/content for write isolation.
  const stableHistory = response => response.data.data.items.map(message => ({
    id: message.message_id, seq: message.chat_seq, role: message.role,
    content: message.content, thinking: message.thinking, tools: message.tool_calls,
  }));
  assert.deepEqual(stableHistory(after), stableHistory(history));
  result.checks.push('Cross-user read/write/replay isolation without history mutation');
  const ordered = await call(owner, '/v1/chats/sidebar-order', { order: [chat, chat] }, 'PUT');
  assert.deepEqual(ordered.data.data.order, [chat]);
  assert.deepEqual((await call(owner, '/v1/chats/sidebar-order')).data.data.order, [chat]);
  const upload = await owner.evaluate(async () => {
    const form = new FormData();
    form.append('file', new Blob(['Docker E2E file integrity'], { type: 'text/plain' }), 'docker-e2e.txt');
    const r = await fetch('/api/v1/file/upload', { method: 'POST', body: form });
    return { status: r.status, data: await r.json() };
  });
  assert.equal(upload.status, 200);
  const file = (upload.data.data || upload.data).file_id;
  assert.ok(file); result.createdFiles.push(file);
  assert.equal((await call(owner, `/files/${file}`)).data, 'Docker E2E file integrity');
  assert.ok([403, 404].includes((await call(other, `/files/${file}`)).status));
  result.checks.push('Sidebar persistence and real storage upload/download/isolation');
  const sandboxChat = await call(owner, '/v1/chats', { title: 'Docker sandbox E2E ' + Date.now() });
  assert.equal(sandboxChat.status, 201);
  const sandboxId = sandboxChat.data.data.chat_id;
  result.createdChats.push(sandboxId);
  const toolRun = await stream(owner, '/v1/agents/responses', {
    ...common, chat_id: sandboxId,
    message: "Use the Bash tool to execute exactly: printf DOCKER_SANDBOX_OK . Then report its output. You must actually run the tool; do not merely state the expected result.",
  });
  const toolResults = toolRun.events.filter(e => e.type === 'tool_result');
  result.sandbox = { toolCalls: toolRun.events.filter(e => e.type === 'tool_call').length,
    resultCount: toolResults.length,
    outputMarker: toolResults.some(e => JSON.stringify(e).includes('DOCKER_SANDBOX_OK')) };
  assert.ok(result.sandbox.toolCalls > 0 && result.sandbox.outputMarker, JSON.stringify(toolResults));
  result.checks.push('Real model invokes configured sandbox and receives command output');
  if (process.env.DOCKER_E2E_CAPABILITIES === '1') {
    const installed = await call(owner, '/v1/plugins/installed');
    assert.equal(installed.status, 200);
    let plugin = installed.data.data.items.find(item => item.slug === 'skill-manager');
    if (!plugin) {
      const install = await call(owner, '/v1/plugins/skill-manager/install', {});
      assert.equal(install.status, 201, JSON.stringify(install.data));
      plugin = install.data.data;
      result.createdPlugins.push(plugin.install_id);
    }
    assert.ok(plugin?.install_id, 'skill-manager must be installed for capability E2E');
    const pluginChat = await call(owner, '/v1/chats', { title: 'Package plugin E2E ' + Date.now() });
    const pluginChatId = pluginChat.data.data.chat_id;
    result.createdChats.push(pluginChatId);
    const pluginRun = await stream(owner, '/v1/agents/responses', {
      ...common, chat_id: pluginChatId, plugin_id: plugin.install_id, plugin_name: plugin.name,
      message: '请实际加载技能管理插件，读取 skill-creator 的 SKILL.md，然后调用 list_skills 列出当前已安装技能。不要创建、更新、删除任何技能。完成后回复 PACKAGE_PLUGIN_SKILL_OK。',
    });
    await writeFile(root + '/plugin-events.json', JSON.stringify(pluginRun.events, null, 2));
    const pluginCalls = pluginRun.events.filter(e => e.type === 'tool_call');
    const pluginResults = pluginRun.events.filter(e => e.type === 'tool_result');
    assert.ok(pluginCalls.some(e => JSON.stringify(e).includes('list_skills')));
    assert.ok(pluginCalls.some(e => /SKILL\.md|skill-creator/.test(JSON.stringify(e))));
    for (const name of ['load_plugin', 'load_skill', 'list_skills']) {
      assert.ok(pluginResults.some(e => e.tool_name === name && e.status === 'success'), name);
    }
    result.checks.push('Real plugin activation, skill instructions read and MCP list_skills result');
    const agentResponse = await call(owner, '/v1/agents', {
      name: 'Package arithmetic E2E', description: 'Temporary E2E agent',
      system_prompt: 'Compute the requested integer multiplication accurately. Return the result and PACKAGE_SUBAGENT_OK.',
      mcp_server_ids: [], skill_ids: [], plugin_ids: [], max_iters: 3,
    });
    assert.equal(agentResponse.status, 200);
    const agentId = agentResponse.data.data.agent_id;
    assert.ok(agentId);
    result.createdAgents.push(agentId);
    const agentChat = await call(owner, '/v1/chats', { title: 'Package delegate E2E ' + Date.now() });
    const agentChatId = agentChat.data.data.chat_id;
    result.createdChats.push(agentChatId);
    const agentRun = await stream(owner, '/v1/agents/responses', {
      ...common, chat_id: agentChatId, mention_agent_id: agentId, enabled_agents: [agentId],
      message: '请实际调用指定子智能体计算 23×7，转述它的结果与标记。必须调用子智能体，不要自行代答。',
    });
    await writeFile(root + '/subagent-events.json', JSON.stringify(agentRun.events, null, 2));
    assert.ok(agentRun.events.some(e => e.type === 'tool_call' && JSON.stringify(e).includes('call_subagent')));
    assert.ok(agentRun.events.some(e => e.type === 'tool_result' && e.tool_name === 'call_subagent' && e.status === 'success' && JSON.stringify(e).includes('161')));
    assert.ok(agentRun.text.includes('161'));
    result.checks.push('Real call_subagent dispatch and child model result 23 × 7 = 161');
  }
  await owner.goto(base + '/');
  await owner.getByText(title, { exact: true }).first().click();
  await owner.getByText('DOCKER_EDITED_OK', { exact: true }).first().waitFor();
  await owner.reload();
  await owner.getByText('DOCKER_EDITED_OK', { exact: true }).first().waitFor();
  await owner.screenshot({ path: root + '/browser.png', fullPage: true });
  result.checks.push('Rebuilt frontend renders persisted history after reload');
  assert.deepEqual(errors, []);
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.error = String(error);
  throw error;
} finally {
  if (owner) {
    for (const plugin of result.createdPlugins) await call(owner, `/v1/plugins/installed/${encodeURIComponent(plugin)}`, undefined, 'DELETE');
    for (const agent of result.createdAgents) await call(owner, `/v1/agents/${agent}`, undefined, 'DELETE');
    for (const file of result.createdFiles) await call(owner, `/v1/artifacts/${file}`, undefined, 'DELETE');
    for (const chat of result.createdChats) await call(owner, `/v1/chats/${chat}`, undefined, 'DELETE');
  }
  for (const context of contexts) {
    const pages = context.pages();
    if (pages[0]) await call(pages[0], '/v1/auth/logout', {});
  }
  result.browserErrors = errors;
  await writeFile(root + '/browser-result.json', JSON.stringify(result, null, 2));
  await browser.close();
}
console.log(JSON.stringify({ passed: result.passed, checks: result.checks }, null, 2));
