/** Real composer gestures and message presentation; network is a fixture. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const output = resolve('node_modules/.tmp/markdown-composer-browser');
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
  await editor.pressSequentially('* first');
  await editor.press('Shift+Enter');
  await editor.pressSequentially('second');
  assert.equal(await editor.locator('ul > li').count(), 2, 'Enter continues Markdown lists');
  assert.equal(await page.evaluate(() => window.smoke.sent.length), 0);
  await editor.press('Shift+Enter');
  await editor.press('Shift+Enter');
  assert.equal(await editor.locator('ul > li').count(), 2, 'empty item exits list');
  await page.evaluate(() => window.smoke.set(''));
  const reset = async () => {
    await page.evaluate(() => window.smoke.set(''));
    await editor.click();
  };
  await editor.pressSequentially('1. first');
  await editor.press('Shift+Enter');
  await editor.pressSequentially('second');
  await editor.press('Tab');
  assert.equal(await editor.locator('ol ol li').count(), 1, 'Tab nests a list item');
  await editor.press('Shift+Tab');
  assert.equal(await editor.locator('ol > li').count(), 2, 'Shift+Tab lifts a list item');
  await reset();
  await editor.pressSequentially('- [x] done');
  await editor.press('Shift+Enter');
  await editor.pressSequentially('todo');
  assert.equal(await editor.locator('ul[data-type="taskList"] > li').count(), 2);
  assert.equal(await editor.locator('input[type="checkbox"]').nth(1).isChecked(), false, 'new task is unchecked');
  await editor.locator('input[type="checkbox"]').nth(1).click();
  await page.waitForFunction(() => /\[x\] todo/.test(window.smoke.state().input));
  assert.match(await page.evaluate(() => window.smoke.state().input), /\[x\] todo/);
  await reset();
  for (let level = 1; level <= 6; level++) {
    await editor.pressSequentially('#'.repeat(level) + ' Heading');
    assert.equal(await editor.locator('h' + level).innerText(), 'Heading');
    await editor.press('Shift+Enter');
    await editor.pressSequentially('paragraph');
    assert.equal(await editor.locator('h' + level).innerText(), 'Heading', 'heading Enter starts a paragraph');
    await reset();
  }
  await editor.pressSequentially('> quote');
  await editor.press('Shift+Enter');
  await editor.pressSequentially('continued');
  assert.match(await editor.locator('blockquote').innerText(), /continued/);
  await editor.press('Shift+Enter');
  await editor.press('Shift+Enter');
  await editor.pressSequentially('outside');
  assert.doesNotMatch(await editor.locator('blockquote').innerText(), /outside/, 'empty quote exits');
  await reset();
  await editor.pressSequentially('~~strike~~');
  assert.equal(await editor.locator('s').innerText(), 'strike');
  await reset();
  await editor.pressSequentially(String.fromCharCode(96) + 'code' + String.fromCharCode(96));
  assert.equal(await editor.locator('code').innerText(), 'code');
  await reset();
  await editor.pressSequentially('[site](https://example.com)');
  assert.equal(await editor.locator('a').getAttribute('href'), 'https://example.com', 'Markdown links render while typing');
  await reset();
  await editor.pressSequentially('---');
  assert.equal(await editor.locator('hr').count(), 1);
  await reset();
  await editor.evaluate(el => {
    const data = new DataTransfer();
    data.setData('text/plain', '| A | B |\n| --- | --- |\n| one | two |');
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  });
  assert.equal(await editor.locator('table').count(), 1);
  await editor.locator('td').last().click();
  await editor.press('Tab');
  assert.equal(await editor.locator('tr').count(), 3, 'Tab at final table cell adds a row');
  assert.equal(await page.evaluate(() => window.smoke.sent.length), 0, 'structural gestures never send');
  await reset();
  await editor.pressSequentially('| **Header** | [Value](https://example.com/value) ![alt](https://example.com/table.png) |');
  await editor.press('Shift+Enter');
  await editor.pressSequentially('| --- | --- |');
  await editor.press('Shift+Enter');
  assert.equal(await editor.locator('table').count(), 1, 'typed pipe table converts after delimiter row');
  assert.equal(await editor.locator('table strong').innerText(), 'Header', 'table conversion preserves marks');
  assert.equal(await editor.locator('table a').first().getAttribute('href'), 'https://example.com/value');
  await page.waitForFunction(() => /https:\/\/example.com\/table.png/.test(window.smoke.state().input));
  assert.match(await page.evaluate(() => window.smoke.state().input), /https:\/\/example.com\/table.png/, 'table conversion keeps literal image destination');

  assert.equal(await page.evaluate(() => window.smoke.sent.length), 0);
  await reset();
  await editor.pressSequentially('**加粗**');
  await editor.locator('strong').waitFor({ timeout: 2000 });
  assert.equal(await editor.locator('strong').innerText(), '加粗');
  await page.waitForFunction(() => window.smoke.state().input === '**加粗**');
  assert.equal(await page.evaluate(() => window.smoke.state().input), '**加粗**');
  await editor.press('Enter');
  await page.locator('.jx-bubble.user strong').waitFor();
  assert.deepEqual(await page.evaluate(() => window.smoke.sent), ['**加粗**']);
  await editor.pressSequentially('normal');
  await editor.press('Control+a');
  await editor.press('Control+b');
  assert.equal(await editor.locator('strong').innerText(), 'normal');
  await editor.press('Control+i');
  await editor.locator('em').waitFor();
  await editor.press('Control+z');
  assert.equal(await editor.locator('em').count(), 0);
  await editor.press('Control+Shift+z');
  await editor.locator('em').waitFor();
  await editor.press('Control+a');
  await editor.press('Backspace');
  await editor.pressSequentially('```');
  await editor.press('Space');
  await editor.locator('pre').waitFor();
  await editor.pressSequentially('first');
  await editor.press('Shift+Enter');
  await editor.pressSequentially('  second');
  assert.equal(await page.evaluate(() => window.smoke.sent.length), 1, 'code Shift+Enter must not send');
  assert.match(await editor.locator('pre').innerText(), /first\n  second/);

  await editor.press('Enter');
  assert.equal(await page.evaluate(() => window.smoke.sent.length), 2);
  await editor.evaluate(el => {
    const data = new DataTransfer();
    data.setData('text/html', '<p><strong>网页粗体</strong> <em>斜体</em></p><pre><code>line1\n  line2</code></pre>');
    data.setData('text/plain', '网页粗体 斜体\nline1\n  line2');
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  });
  assert.equal(await editor.locator('strong').innerText(), '网页粗体');
  await page.waitForFunction(() => /\*\*网页粗体\*\*/.test(window.smoke.state().input));
  assert.match(await page.evaluate(() => window.smoke.state().input), /\*\*网页粗体\*\*/);
  assert.match(await editor.locator('pre').innerText(), /line1\n  line2/);
  await editor.press('Control+End');
  await editor.press('Enter');
  await page.reload();
  await page.locator('.jx-bubble.user strong').first().waitFor();
  assert.equal(await page.locator('.jx-bubble.user strong').last().innerText(), '网页粗体');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.locator('.jx-msg.user').first().hover();
  await page.getByTitle('复制内容', { exact: true }).first().click();
  await page.getByTitle('已复制', { exact: true }).first().waitFor();
  const copied = await page.evaluate(async () => {
    const item = (await navigator.clipboard.read())[0];
    return { text: await (await item.getType('text/plain')).text(), html: await (await item.getType('text/html')).text() };
  });
  assert.equal(copied.text, '**加粗**');
  assert.match(copied.html, /<strong>加粗<\/strong>/);
  await page.locator('.jx-msg.user').first().hover();
  await page.getByTitle('编辑消息', { exact: true }).first().click();
  const edit = page.locator('.jx-editMessage [contenteditable="true"]');
  await edit.locator('strong').waitFor();
  await edit.press('Control+a');
  await edit.press('Control+i');
  await edit.press('Enter');
  assert.match(await page.evaluate(() => window.smoke.edited), /加粗/);
  await page.locator('.jx-msg.user').first().hover();
  await page.getByTitle('编辑消息', { exact: true }).first().click();
  await edit.locator('strong').waitFor();
  await edit.press('Control+a');
  await edit.press('Backspace');
  await edit.pressSequentially('* edit first');
  await edit.press('Shift+Enter');
  await edit.pressSequentially('edit second');
  assert.equal(await edit.locator('ul > li').count(), 2, 'saved-message editor continues lists too');
  await edit.press('Enter');
  assert.match(await page.evaluate(() => window.smoke.edited), /edit first[\s\S]*edit second/);

  await page.evaluate(() => window.smoke.set('[ref:e9] <script>alert(1)</script>'));
  await editor.press('Enter');
  assert.match(await page.locator('.jx-bubble.user').last().innerText(), /\[ref:e9\]/);
  assert.equal(await page.locator('.jx-bubble.user script').count(), 0);

  await page.evaluate(() => window.smoke.set(''));
  await editor.pressSequentially('@测试');
  await page.getByRole('option').filter({ hasText: '测试智能体' }).click();
  await editor.locator('[data-chip="mention"]').waitFor();
  assert.equal(await page.evaluate(() => window.smoke.state().activeMention.id), 'test-agent');
  assert.equal(await page.evaluate(() => window.smoke.state().input.trim()), '', 'chip is not prompt text');
  await editor.press('Backspace');
  await page.waitForFunction(() => window.smoke.state().activeMention === null);
  assert.equal(await page.evaluate(() => window.smoke.state().activeMention), null);
  await editor.press('Control+z');
  await editor.locator('[data-chip="mention"]').waitFor();
  await page.waitForFunction(() => window.smoke.state().activeMention?.id === 'test-agent');
  assert.equal(await page.evaluate(() => window.smoke.state().activeMention.id), 'test-agent', 'undo restores authoritative chip state');
  await editor.press('Home');
  await editor.pressSequentially('| Header |');
  await editor.press('End');
  await editor.press('Shift+Enter');
  await editor.pressSequentially('| --- |');
  await editor.press('Shift+Enter');
  assert.equal(await editor.locator('[data-chip="mention"]').count(), 1, 'table conversion cannot remove capability chips');

  await page.evaluate(() => window.smoke.set(''));
  await editor.pressSequentially('input');
  const sentBeforeIme = await page.evaluate(() => window.smoke.sent.length);
  await editor.evaluate(el => el.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' })));
  await editor.press('Enter');
  assert.equal(await page.evaluate(() => window.smoke.sent.length), sentBeforeIme, 'IME confirmation does not send');
  await editor.evaluate(el => el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '中文' })));
  await page.evaluate(() => window.smoke.set(''));
  await page.evaluate(() => navigator.clipboard.writeText('**literal**'));
  await editor.press('Control+Shift+v');
  await page.waitForFunction(() => [...document.querySelectorAll('.jx-inputArea [contenteditable="true"]')].at(-1)?.innerText === '**literal**');
  assert.equal(await editor.locator('strong').count(), 0, 'plain paste bypasses Markdown and HTML conversion');
  assert.equal(await editor.innerText(), '**literal**');

  await page.evaluate(() => window.smoke.set('before ![alt](https://example.com/a.png) after'));
  await editor.press('Control+End');
  await editor.pressSequentially('!');
  await page.waitForFunction(() => /!\[alt\]\(https:\/\/example.com\/a.png\)/.test(window.smoke.state().input));
  assert.match(await page.evaluate(() => window.smoke.state().input), /!\[alt\]\(https:\/\/example.com\/a.png\)/, 'unsupported image keeps URL and Markdown');
  await page.evaluate(() => window.smoke.set('![alt][img]\n\n[img]: https://example.com/ref.png'));
  await editor.press('Control+End');
  await editor.pressSequentially('!');
  await page.waitForFunction(() => /https:\/\/example.com\/ref.png/.test(window.smoke.state().input));
  assert.match(await page.evaluate(() => window.smoke.state().input), /https:\/\/example.com\/ref.png/, 'reference image keeps resolved destination');
  await page.evaluate(() => window.smoke.set('<strong>literal HTML</strong>'));
  assert.equal(await editor.locator('strong').count(), 0);
  assert.match(await editor.innerText(), /<strong>literal HTML<\/strong>/);
  await editor.press('Control+End');
  await editor.pressSequentially('!');
  await page.waitForFunction(() => /<strong>literal HTML<\/strong>/.test(window.smoke.state().input));
  assert.match(await page.evaluate(() => window.smoke.state().input), /<strong>literal HTML<\/strong>/);
  await page.evaluate(() => window.smoke.set('```mermaid\ngraph TD; A-->B;\n```'));
  await editor.press('Control+End');
  await editor.press('Enter');
  await page.waitForFunction(() => [...document.querySelectorAll('.jx-bubble.user')].at(-1)?.innerText.includes('graph TD; A-->B;'), {timeout: 2000});
  assert.match(await page.locator('.jx-bubble.user').last().innerText(), /graph TD; A-->B;/);
  await page.evaluate(() => window.smoke.set('Secret draft'));
  await editor.press('Control+End');
  await editor.pressSequentially(' previous-user');
  await page.evaluate(() => window.smoke.setUser('other-user'));
  await page.evaluate(() => window.smoke.set('New user draft'));
  await editor.press('Control+z');
  assert.equal(await editor.innerText(), 'New user draft', 'undo cannot restore previous account content');
  await page.evaluate(() => window.smoke.set('* send list'));
  await editor.press('Control+End');
  await editor.press('Enter');
  assert.match(await page.evaluate(() => window.smoke.sent.at(-1)), /send list/, 'Enter sends even inside a list');
  for (const markdown of ['## send heading', '> send quote', '- [ ] send task', '| A | B |\n| --- | --- |\n| send | table |']) {
    await page.evaluate(text => window.smoke.set(text), markdown);
    await editor.press('Control+End');
    await editor.press('Enter');
    assert.match(await page.evaluate(() => window.smoke.sent.at(-1)), /send/, 'Enter sends every Markdown structure');
  }
  await editor.pressSequentially('button send');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  assert.equal(await page.evaluate(() => window.smoke.sent.at(-1)), 'button send', 'send button is unchanged');
  assert.deepEqual(errors, []);
  console.log('PASS: lists/indentation/tasks/headings/quotes/links/tables, Markdown typing/send/history, shortcuts, code Enter/indentation, HTML paste, Markdown+HTML copy, rich editing, citation text, chip delete/undo, IME confirmation and plain paste');
} finally { await browser.close(); }
