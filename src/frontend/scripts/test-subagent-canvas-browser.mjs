import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/subagent-canvas-browser');
await mkdir(output, { recursive: true });
await build({ stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { ToolCallRow } from './src/components/tool/ToolCallRow';
import { ToolMessageContext } from './src/components/tool/ToolMessageContext';
import { SubagentSidebarPanel } from './src/components/canvas/SubagentSidebarPanel';
import { useCanvasStore } from './src/stores/canvasStore';
import { useChatStore } from './src/stores/chatStore';
import { useAgentStore } from './src/stores/agentStore';
import { useCatalogStore } from './src/stores/catalogStore';
import { usePluginStore } from './src/stores/pluginStore';
import { useUIStore } from './src/stores/uiStore';
import './src/styles/variables.css';
import './src/styles/chat.css';
import './src/styles/tool.css';
import './src/styles/catalog.css';
import './src/styles/mcp.css';
import './src/styles/canvas.css';
const tool = { id: 'call-a', name: 'call_subagent', subagentName: 'Research agent', input: { agent_id: 'agent-a', task: 'Review the UI changes' }, status: 'running', timestamp: Date.now(), subSteps: [{ kind: 'content', text: 'Checking the implementation before running tools.' }, { kind: 'tool', toolId: 'nested-a', name: 'bash', input: { command: 'echo checked' }, output: 'checked', status: 'success' }] };
const msg = { uid: 'message-a', messageId: 'persisted-a', role: 'assistant', content: '', ts: Date.now(), isStreaming: true, toolCalls: [tool, { ...tool, id: 'call-b', status: 'success', output: 'Other invocation', subSteps: [] }] };
useChatStore.setState(s => ({ store: { ...s.store, chats: { ...s.store.chats, 'chat-a': { id: 'chat-a', messages: [msg] } } } }));
useAgentStore.setState({ agents: [] });
useUIStore.setState({ dispatchProcessVisible: true });
useCatalogStore.setState(s => ({ catalog: { ...s.catalog,
 skills: [{ id: 'writer', name: 'Writing', icon: '/skill.svg' }],
 mcp: [{ id: 'search', name: 'Search', icon: '/connector.svg', tools: ['search_web'] }],
} }));
usePluginStore.setState({ installed: [{ slug: 'office', install_id: 'office-install', name: 'Office', icon: '/plugin.svg', skills: ['plugin-writer'], tools: ['plugin_search'], mcp: ['plugin-mcp'] }], loaded: true });
function Fixture() {
 const message = useChatStore(s => s.store.chats['chat-a'].messages[0]);
 const open = useCanvasStore(s => s.isOpen);
 const patch = (extra) => useChatStore.setState(s => ({ store: { ...s.store, chats: { ...s.store.chats, 'chat-a': { ...s.store.chats['chat-a'], messages: [{ ...message, toolCalls: [{ ...message.toolCalls[0], ...extra }, message.toolCalls[1]] }] } } } }));
 return <><nav><button onClick={() => patch({ output: 'Live updated answer' })}>Update</button><button onClick={() => patch({ status: 'success', durationMs: 12000, output: { content: [{ type: 'text', text: '【Research agent】的回复：' + String.fromCharCode(10, 10) + 'Final verified answer' }] } })}>Complete</button><button onClick={() => { useCanvasStore.getState().resetSidebar(); patch({ output: 'Short history', outputTruncated: true, outputLoaded: false, status: 'success' }); }}>History</button><button onClick={() => { useCanvasStore.getState().resetSidebar(); }}>Switch chat</button><button onClick={() => document.documentElement.dataset.theme = 'dark'}>Dark</button></nav>
 <main><section id="conversation"><h2>Review interface changes</h2><p>The main conversation keeps a compact invocation row.</p><ToolMessageContext.Provider value={{ chatId: 'chat-a', messageUid: message.uid, messageId: message.messageId }}>{message.toolCalls.map(tool => <ToolCallRow key={tool.id} tool={tool} isStreaming={message.isStreaming} />)}<ToolCallRow tool={{ id: 'normal', name: 'bash', input: { command: 'echo ordinary' }, output: 'ordinary output', status: 'success' }} /></ToolMessageContext.Provider><div id="capabilities">
<ToolCallRow tool={{ id: 'skill', name: 'load_skill', input: { file_path: '/workspace/skills/writer/SKILL.md' }, output: 'loaded', status: 'success' }} />
<ToolCallRow tool={{ id: 'plugin-skill', name: 'load_skill', input: { file_path: '/skills/plugin-writer/SKILL.md' }, status: 'success' }} />
<ToolCallRow tool={{ id: 'plugin-tool', name: 'plugin_search', status: 'success' }} />
<ToolCallRow tool={{ id: 'plugin', name: 'load_plugin', input: { plugin: 'office' }, output: 'loaded', status: 'success' }} />
<ToolCallRow tool={{ id: 'connector', name: 'search_web', inputText: '{', status: 'running' }} isStreaming />
</div></section>{open && <div className="jx-canvasPanelSlot jx-rightSidebarSlot"><SubagentSidebarPanel /></div>}</main></>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
`, resolveDir: process.cwd(), loader: 'tsx' }, outfile: resolve(output, 'fixture.js'), external: ['/loader.gif', '/loader-done.png'], bundle: true, format: 'esm', jsx: 'automatic', define: { 'import.meta.env': '{}' }, loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' } });
const browser = await chromium.launch({ headless: true });
try {
 const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
 const errors = [];
 page.on('pageerror', e => errors.push(e.message));
 let resultRequests = 0;
 let agentRequests = 0;
 await page.route('https://subagent.test/**', async route => {
  const path = new URL(route.request().url()).pathname;
  if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({ contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css', body: await readFile(resolve(output, path.slice(1))) });
  if (path.includes('/tool-calls/')) { resultRequests++; return route.fulfill({ json: { code: 0, data: { result: 'Loaded historical final answer' } } }); }
  if (path === '/api/v1/agents') { agentRequests++; return route.fulfill({ json: { code: 0, data: [{ agent_id: 'agent-a', name: 'Research agent', avatar: '🔎' }] } }); }
  if (path.endsWith('.svg')) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><circle cx="12" cy="12" r="10" fill="blue"/></svg>' });
  if (path.startsWith('/api')) return route.fulfill({ json: { code: 0, data: {} } });
  return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"><style>*{box-sizing:border-box}body{margin:0;font:15px Arial;background:var(--color-bg-container);color:var(--color-text)}nav{height:50px}main{display:flex;height:850px}#conversation{flex:1;padding:35px;min-width:0}.jx-rightSidebarSlot{width:58%}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
 });
 await page.goto('https://subagent.test/');
 const rows = page.getByRole('button', { name: '查看智能体「Research agent」的执行过程' });
 await rows.first().waitFor();
 assert.equal(await page.locator('aside').count(), 0);
 for (const icon of ['skill', 'plugin', 'connector']) {
   await page.locator('#capabilities .jx-tcr-capabilityIcon img[src="/' + icon + '.svg"]').first().waitFor();
 }
 assert.equal(await page.locator('#capabilities img[src="/plugin.svg"]').count(), 3, 'Plugin skills and tools inherit plugin logo');
 assert.equal(await page.locator('#capabilities .anticon-loading').count(), 1, 'Connector spinner is separate from its logo');
 const skillRow = page.locator('#capabilities .jx-tcr-header').first();
 await skillRow.click();
 assert.equal(await page.locator('aside').count(), 0, 'Capability icons retain inline expansion');
 assert.equal(await rows.first().getByLabel('正在执行').count(), 1);
 await rows.first().getByText('🔎', { exact: true }).waitFor();
 await rows.first().focus(); await page.keyboard.press('Enter');
 await page.getByRole('tab', { name: /Research agent/ }).waitFor();
 assert.equal(await page.locator('#conversation .jx-tcr-subagent').count(), 0);
 assert.equal(await page.locator('aside details').count(), 0, 'No bespoke parameter/process sections');
 const assertToolWidth = async (state) => {
   const header = await page.locator('aside .jx-trs-head').boundingBox();
   const transcript = await page.locator('aside .jx-msg').boundingBox();
   assert.ok(Math.abs(header.width - transcript.width) < 2, state + ': tool header must fill the transcript');
 };
 await assertToolWidth('Initially collapsed');
 await page.locator('aside .jx-trs-head').click();
 await assertToolWidth('Expanded batch');
 assert.ok((await page.locator('aside').innerText()).includes('echo checked'));
 await page.locator('aside .jx-tcr-header').click();
 await page.locator('aside').getByText('checked', { exact: true }).waitFor();
 await assertToolWidth('Expanded tool');
 await page.locator('aside .jx-trs-head').click();
 await assertToolWidth('Collapsed again');
 await page.getByRole('button', { name: 'Update', exact: true }).click();
 await page.getByText('Live updated answer', { exact: true }).waitFor();
 await page.getByRole('button', { name: 'Complete', exact: true }).click();
 await page.getByText('Final verified answer', { exact: true }).waitFor();
 assert.ok(!(await page.locator('aside').innerText()).includes('【Research agent】的回复：'));
 assert.equal(await rows.first().getByLabel('正在执行').count(), 0);
 assert.equal(await rows.first().getByLabel('已完成').count(), 0, 'No completion checkmark');
 await rows.nth(1).click();
 await page.getByText('Other invocation', { exact: true }).waitFor();
 assert.equal(await page.getByRole('tab').count(), 2);
 await rows.first().click();
 assert.equal(await page.getByRole('tab').count(), 2);
 assert.ok((await page.locator('aside').textContent()).includes('Checking the implementation before running tools.'), 'Content steps survive completion');
 await page.locator('aside .jx-trs-head').click();
 await page.screenshot({ path: resolve(output, 'light.png'), fullPage: true });
 await page.getByRole('button', { name: 'Dark', exact: true }).click();
 await page.screenshot({ path: resolve(output, 'dark.png'), fullPage: true });
 await page.getByRole('button', { name: 'History', exact: true }).click();
 await rows.first().click();
 await page.getByText('Loaded historical final answer', { exact: true }).waitFor();
 assert.equal(resultRequests, 1);
 await page.getByRole('button', { name: 'Switch chat', exact: true }).click();
 assert.equal(await page.locator('aside').count(), 0);
 const ordinary = page.locator('#conversation .jx-tcr-header[role="button"]').filter({ hasText: 'echo ordinary' });
 await ordinary.click();
 await page.getByText('ordinary output', { exact: true }).waitFor();
 assert.equal(await page.locator('aside').count(), 0);
 assert.equal(agentRequests, 1, 'Visible rows deduplicate metadata requests');
 assert.deepEqual(errors, []);
 console.log('Browser checks passed: avatar/status, keyboard opening, live updates, invocation isolation, history fetch, ordinary tools, light/dark screenshots');
} finally { await browser.close(); }
