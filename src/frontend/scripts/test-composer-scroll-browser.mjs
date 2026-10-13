/** Real composer gestures and message presentation; network is a fixture. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const output = resolve('node_modules/.tmp/composer-scroll-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { ChatArea } from './src/components/chat/ChatArea';
import { useChatViewport } from './src/hooks/useChatViewport';
import { readComposer } from './src/stores/composerStore';
import { useChatStore } from './src/stores/chatStore';
import { useAgentStore } from './src/stores/agentStore';
import 'antd/dist/reset.css';
import './src/styles/variables.css';
import './src/styles/chat.css';
import './src/styles/mobile.css';
useChatStore.setState({ currentUserId: 'owner', currentChatId: 'source',
  activeRuns: {}, sendingChatIds: new Set(), sending: false,
  store: { order: ['source'], chats: { source: { id: 'source', title: 'Markdown',
    createdAt: 1, updatedAt: 1, messages: Array.from({length: 30}, (_, i) => ({uid: String(i), role: 'user', content: 'History ' + i, ts: i})), runTarget: 'cloud' } } },
}); readComposer().setInput('');
useAgentStore.setState({ agents: [{ agent_id: 'test-agent', name: '测试智能体', description: '测试', welcome_message: '', is_enabled: true }] });
window.smoke = { setUser: id => useChatStore.setState({ currentUserId: id }), state: () => readComposer(), set: text => readComposer().setInput(text), sent: [] };
function Fixture() {
  const hasMessages = useChatStore(s => !!s.store.chats.source.messages.length);
  const viewport = useChatViewport('source', hasMessages, true);
  window.smoke.empty = () => useChatStore.setState(s => ({store: {...s.store, chats: {...s.store.chats, source: {...s.store.chats.source, messages: []}}}}));
  window.smoke.append = () => useChatStore.setState(s => ({store: {...s.store, chats: {...s.store.chats, source: {...s.store.chats.source, messages: [...s.store.chats.source.messages, {uid: String(Date.now()), role: 'user', content: 'Stream growth', ts: Date.now()}]}}}}));
  return <main className={'jx-content jx-content--chatSurface' + (hasMessages ? ' jx-content--messageSurface' : '')} style={{height: 600, width: '100%'}}><div className="jx-panel" data-panel="chat">
    <ChatArea {...viewport} onViewportMount={viewport.handleContentRef} send={() => {}} exportChatRecord={async () => {}} createChatShare={async () => ({})} handleFileSelect={() => {}} removeFile={() => {}} />
  </div></main>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.svg': 'dataurl' },
});
const origin = 'http://127.0.0.1:5197';
const browser = await chromium.launch({ headless: true,
  ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}),
});
const errors = [];
try {
  for (const width of [1400, 768, 390]) {
  const page = await browser.newPage({viewport: {width, height: 800}});
  page.on('pageerror', e => errors.push(e.message));
  await page.route(origin + '/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/fixture.css') return route.fulfill({ contentType: 'text/css', body: await readFile(resolve(output, 'fixture.css')) });
    if (path === '/fixture.js') return route.fulfill({ contentType: 'text/javascript', body: await readFile(resolve(output, 'fixture.js')) });
    if (path.startsWith('/api')) return route.fulfill({ json: { code: 20000, data: { items: [] } } });
    return route.fulfill({ contentType: 'text/html', body: '<link rel="stylesheet" href="/fixture.css"><div id="root"></div><script type="module" src="/fixture.js"></script>' });
  });
  await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
  await page.goto(origin);
  const editor = page.locator('.jx-inputArea [contenteditable="true"]');
  await editor.waitFor();

  const failures = [];
  await page.locator('.jx-chatViewport').evaluate(el => { el.scrollTop = el.scrollHeight; });
  await editor.pressSequentially('Scroll regression');
  for (let i = 0; i < 24; i++) {
    await editor.press('Shift+Enter');
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  }
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const caret = await editor.evaluate(el => {
    const line = el.querySelector('.ProseMirror-trailingBreak').getBoundingClientRect();
    const toolbar = el.closest('.jx-composerWrap').querySelector('.jx-composerBar').getBoundingClientRect();
    const box = el.getBoundingClientRect();
    return { toolbarTop: toolbar.top, editorBottom: box.bottom, top: line.top, bottom: line.bottom, viewportTop: box.top, viewportBottom: Math.min(box.bottom, toolbar.top) };
  });
  if (caret.bottom > caret.viewportBottom + 1 || caret.top < caret.viewportTop - 1) failures.push('Shift+Enter caret is hidden: ' + JSON.stringify(caret));
  assert.ok(caret.editorBottom <= caret.toolbarTop + 1, 'Toolbar must occupy its own layout row');
  const bounds = await editor.boundingBox();
  await page.mouse.move(bounds.x + 40, bounds.y + 30);
  const start = await page.locator('.jx-chatViewport').evaluate(el => el.scrollTop);
  await page.mouse.wheel(0, 600);
  await page.waitForTimeout(200);
  assert.ok(Math.abs(await page.locator('.jx-chatViewport').evaluate(el => el.scrollTop) - start) <= 1, 'Editor bottom wheel stays isolated');
  await page.mouse.wheel(0, -10000);
  await page.waitForTimeout(200);
  const before = await page.locator('.jx-chatViewport').evaluate(el => el.scrollTop);
  await page.mouse.wheel(0, -600);
  await page.waitForTimeout(200);
  const after = await page.locator('.jx-chatViewport').evaluate(el => el.scrollTop);
  assert.ok(await page.locator('.jx-chatFooter').evaluate(el => el.clientWidth <= 760), 'Composer keeps centered width');
  assert.ok(start > 0, 'History must overflow for wheel isolation test');
  assert.equal(await editor.evaluate(el => el.scrollTop), 0, 'Wheel must actually reach editor top');
  await page.evaluate(() => window.smoke.append());
  await page.waitForTimeout(200);
  assert.ok(await page.locator('.jx-chatViewport').evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop < 2), 'Editor wheel must not disable stream following');
  assert.deepEqual(errors, []);
  if (Math.abs(start - before) > 1) failures.push('Editor wheel chains into history: ' + start + ' -> ' + before);
  if (Math.abs(before - after) > 1) failures.push('Editor wheel scrolls history: ' + before + ' -> ' + after);
  assert.deepEqual(failures, [], failures.join('\n'));
  const history = page.locator('.jx-chatViewport');
  const historyBounds = await history.boundingBox();
  await page.mouse.move(historyBounds.x + 20, historyBounds.y + 40);
  await page.mouse.wheel(0, -300);
  await page.waitForTimeout(200);
  const detached = await history.evaluate(el => el.scrollTop);
  await page.evaluate(() => window.smoke.append());
  await page.waitForTimeout(200);
  assert.ok(Math.abs(await history.evaluate(el => el.scrollTop) - detached) <= 1, 'Manual history scrolling pauses follow');
  await page.getByRole('button', {name: '回到底部', exact:true}).click();
  await page.waitForTimeout(600);
  assert.ok(await history.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop < 2), 'Return button reaches history bottom');
  if (width === 1400) {
    await page.locator('.jx-conversationNav-button').first().click();
    await page.waitForFunction(() => document.querySelector('.jx-chatViewport').scrollTop < 30, null, {timeout: 3000});
  }
  await page.evaluate(() => window.smoke.empty());
  await page.locator('.jx-homeInput').waitFor();
  assert.notEqual(await page.locator('main').evaluate(el => getComputedStyle(el).overflowY), 'hidden', 'Welcome keeps its scroll owner');
  await editor.press('Control+End');
  await editor.press('Shift+Enter');
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.ok(await editor.evaluate(el => { const line = el.querySelector('.ProseMirror-trailingBreak').getBoundingClientRect(); const box = el.getBoundingClientRect(); return line.top >= box.top - 1 && line.bottom <= box.bottom + 1; }), 'Welcome Shift+Enter remains visible');
  await page.evaluate(() => window.smoke.append());
  await history.waitFor();
  assert.ok(await history.evaluate(el => el.clientHeight > 0), 'Empty-to-message transition mounts viewport');
  assert.deepEqual(errors, [], 'Browser has no runtime errors');
  await page.close();
  console.log('PASS: composer/history scrolling, follow, welcome transition at width ' + width);
  }
} finally { await browser.close(); }
