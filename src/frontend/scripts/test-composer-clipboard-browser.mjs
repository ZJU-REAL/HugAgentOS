/** Message copy → native paste must preserve content without HTML layout blank lines. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const output = resolve('node_modules/.tmp/composer-clipboard-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: `
import React, { useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { InputArea } from './src/components/chat/InputArea';
import { MessageBubble } from './src/components/chat/MessageBubble';
import { readComposer } from './src/stores/composerStore';
import { useChatStore } from './src/stores/chatStore';
import { useAgentStore } from './src/stores/agentStore';
import './src/styles/variables.css';
import './src/styles/chat.css';
useChatStore.setState({ currentUserId: 'owner', currentChatId: 'source',
  activeRuns: {}, sendingChatIds: new Set(), sending: false,
  store: { order: ['source'], chats: { source: { id: 'source', title: 'Markdown',
    createdAt: 1, updatedAt: 1, messages: JSON.parse(sessionStorage.getItem('messages') || '[]'), runTarget: 'cloud' } } },
}); readComposer().setInput('');
useAgentStore.setState({ agents: [{ agent_id: 'test-agent', name: '测试智能体', description: '测试', welcome_message: '', is_enabled: true }] });
window.smoke = { setUser: id => useChatStore.setState({ currentUserId: id }), state: () => readComposer(), set: text => readComposer().setInput(text), sent: [] };
function Fixture() {
  const inputRef = useRef(null), fileRef = useRef(null);
  const messages = useChatStore(s => s.store.chats.source.messages);
  const send = () => {
    const s = useChatStore.getState(), content = readComposer().input;
    window.smoke.sent.push(content);
    sessionStorage.setItem('messages', JSON.stringify([...s.store.chats.source.messages,
      { uid: String(Date.now()), role: 'user', content, ts: Date.now(), isMarkdown: false }]));
    useChatStore.setState({  store: { ...s.store, chats: { ...s.store.chats,
      source: { ...s.store.chats.source, messages: [...s.store.chats.source.messages,
        { uid: String(Date.now()), role: 'user', content, ts: Date.now(), isMarkdown: false }] } } } }); readComposer().setInput('');
  };
  return <main style={{padding: 32, maxWidth: 900}}>
    {messages.map((m, index) => <MessageBubble key={m.uid} m={m} messageIndex={index}
      currentChatId="source" send={send} exportChatRecord={async () => {}}
      editAndResend={(index, content) => { window.smoke.edited = content; useChatStore.getState().setEditingMessageUid(null); }} />)}
    <InputArea inputRef={inputRef} fileInputRef={fileRef} send={send}
      handleFileSelect={() => {}} removeFile={() => {}} />
  </main>;
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
  const page = await browser.newPage();
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

  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => window.smoke.set('对这里的每一个LDBH的职责争议进行排查，然后新增一列是否存在职责争议。'));
  await editor.press('Enter');
  await page.locator('.jx-msg.user').first().hover();
  await page.getByTitle('复制内容', { exact: true }).first().click();
  await page.getByTitle('已复制', { exact: true }).first().waitFor();
  const copied = await page.evaluate(async () => {
    const item = (await navigator.clipboard.read())[0];
    return {html: await (await item.getType('text/html')).text(), text: await (await item.getType('text/plain')).text()};
  });
  assert.equal(copied.text, '对这里的每一个LDBH的职责争议进行排查，然后新增一列是否存在职责争议。');
  await editor.click();
  await editor.press('Control+v');
  assert.equal(await page.evaluate(() => window.smoke.state().input), copied.text);
  assert.equal(await editor.locator('p').count(), 1, 'copy button roundtrip does not add empty paragraphs');
  await page.evaluate(() => window.smoke.set(''));
  await editor.evaluate(el => {
    const data=new DataTransfer();
    data.setData('text/plain', '正文');
    data.setData('text/html', '\n\n<p>正文</p>\n\n');
    el.dispatchEvent(new ClipboardEvent('paste', {clipboardData:data,bubbles:true,cancelable:true}));
  });
  assert.equal(await page.evaluate(() => window.smoke.state().input), '正文');
  assert.equal(await editor.locator('p').count(), 1, 'HTML formatting whitespace is not content');

  for (const html of [
    '<div>\n\n<p>正文</p>\n\n</div>',
    '<!--StartFragment-->\n<p>正文</p>\n<!--EndFragment-->',
    '<p>正文</p>\n',
  ]) {
    await page.evaluate(() => window.smoke.set(''));
    await editor.evaluate((el, html) => {
      const data = new DataTransfer(); data.setData('text/html', html); data.setData('text/plain', '正文');
      el.dispatchEvent(new ClipboardEvent('paste', {clipboardData:data,bubbles:true,cancelable:true}));
    }, html);
    assert.equal(await page.evaluate(() => window.smoke.state().input), '正文');
    assert.equal(await editor.locator('p').count(), 1);
    const gap = await editor.evaluate(el => {
      const box = el.getBoundingClientRect(), p = el.querySelector('p').getBoundingClientRect();
      return p.top - box.top - parseFloat(getComputedStyle(el).paddingTop);
    });
    assert.ok(gap < 1, 'no blank lines above the first paragraph');
  }
  // Intentional empty paragraphs, hard breaks, code indentation and literal newlines survive.
  for (const [html, expected, selector, count] of [
    ['<p>第一段</p>\n<p></p>\n<p>第二段</p>\n', /第一段[\s\S]*第二段/, 'p', 3],
    ['<p>第一行<br>第二行</p>\n', /^第一行  \n第二行$/, 'br', 1],
    ['<pre><code>first\n  second\n\nlast</code></pre>\n', /first\n  second\n\nlast/, 'pre', 1],
    ['literal\n\n  next', /literal\n\n  next/, 'p', 1],
  ]) {
    await page.evaluate(() => window.smoke.set(''));
    await editor.evaluate((el, html) => {
      const data = new DataTransfer(); data.setData('text/html', html); data.setData('text/plain', 'fallback');
      el.dispatchEvent(new ClipboardEvent('paste', {clipboardData:data,bubbles:true,cancelable:true}));
    }, html);
    assert.equal(await editor.locator(selector).count(), count);
    assert.match(await page.evaluate(() => window.smoke.state().input), expected);
  }
  // Repeated editor copy/paste must not accumulate boundary whitespace.
  await page.evaluate(() => window.smoke.set('正文'));
  for (let cycle = 0; cycle < 4; cycle++) {
    await editor.click(); await editor.press('Control+a'); await editor.press('Control+c');
    await editor.press('Control+v');
    assert.equal(await page.evaluate(() => window.smoke.state().input), '正文');
    assert.equal(await editor.locator('p').count(), 1);
  }
  assert.deepEqual(errors, []);
  console.log('PASS: message native copy/paste, HTML boundary whitespace, intentional paragraphs/breaks/code/literal newlines and repeated editor roundtrip');
} finally { await browser.close(); }
