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
import { messageClipboard } from './src/components/chat/messageClipboard';
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
window.smoke = { clipboard: messageClipboard, message: m => { const s = useChatStore.getState(); useChatStore.setState({ store: { ...s.store, chats: { ...s.store.chats, source: { ...s.store.chats.source, messages: [m] } } } }); }, setUser: id => useChatStore.setState({ currentUserId: id }), state: () => readComposer(), set: text => readComposer().setInput(text), sent: [] };
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
  page.on('pageerror', e => { errors.push(e.message); console.error(e.message); });
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
    '\n正文\n\n',
    '<meta charset="utf-8">\n正文\n\n',
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
  // Captured Codex selection: plain text has no newlines; HTML carries layout LFs.
  await page.evaluate(() => window.smoke.set(''));
  await editor.evaluate(el => {
    const data = new DataTransfer(); data.setData('text/plain', '正文已复制');
    data.setData('text/html', '\n\n<!--StartFragment--><span>正文</span><div><div><div><div><div><div><p><span>已复制</span></p></div></div></div></div></div></div><!--EndFragment-->\n\n');
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  });
  assert.equal(await page.evaluate(() => window.smoke.state().input), '正文\n\n已复制');
  assert.equal(await editor.locator(':scope > p').count(), 2);
  assert.equal(await editor.locator('br:not(.ProseMirror-trailingBreak)').count(), 0);

  // Intentional empty paragraphs, hard breaks, code indentation and literal newlines survive.
  for (const [html, expected, selector, count] of [
    ['<p>第一段</p>\n<p></p>\n<p>第二段</p>\n', /第一段[\s\S]*第二段/, 'p', 3],
    ['<p>第一行<br>第二行</p>\n', /^第一行  \n第二行$/, 'br', 1],
    ['<p><code>a  b</code></p>', /^`a  b`$/, 'code', 1],
    ['<pre><code>first\n  second\n\nlast</code></pre>\n', /first\n  second\n\nlast/, 'pre', 1],
    ['literal\n\n  next', /^literal next$/, 'p', 1],
  ]) {
    await page.evaluate(() => window.smoke.set(''));
    await editor.evaluate((el, html) => {
      const data = new DataTransfer(); data.setData('text/html', html); data.setData('text/plain', 'fallback');
      el.dispatchEvent(new ClipboardEvent('paste', {clipboardData:data,bubbles:true,cancelable:true}));
    }, html);
    assert.equal(await editor.locator(selector).count(), count);
    assert.match(await page.evaluate(() => window.smoke.state().input), expected);
  }
  // The literal text channel retains intentional boundary and internal newlines.
  await page.evaluate(() => window.smoke.set(''));
  await page.waitForFunction(() => document.querySelector('.jx-inputArea [contenteditable="true"]')?.textContent === '');
  await editor.evaluate(el => {
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'v', ctrlKey: true, shiftKey: true, bubbles: true }));
    const data = new DataTransfer(); data.setData('text/plain', '\nliteral\n\n  next\n\n');
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  });
  assert.equal(await editor.textContent(), 'literal  next');
  assert.equal(await editor.locator('br:not(.ProseMirror-trailingBreak)').count(), 5);

  // Browser selection boundaries are clipboard metadata, not authored line breaks.
  for (const [html, text, selector] of [
    ['<br class="Apple-interchange-newline"><span>仓库 lint 也有</span><br class="Apple-interchange-newline">', '仓库 lint 也有', 'p'],
    ['<br class="Apple-interchange-newline"><ul><li>协议标识、消息结构校验。</li></ul><br class="Apple-interchange-newline">', '- 协议标识、消息结构校验。', 'ul'],
  ]) {
    await page.evaluate(() => window.smoke.set(''));
    await editor.evaluate((el, html) => {
      const data = new DataTransfer(); data.setData('text/plain', '仓库 lint 也有'); data.setData('text/html', html);
      el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
    }, html);
    assert.equal(await page.evaluate(() => window.smoke.state().input), text);
    assert.equal(await editor.locator(':scope > ' + selector).count(), 1);
    assert.equal(await editor.locator(':scope > *').count(), 1, 'selection does not introduce boundary paragraphs');
    assert.equal(await editor.locator('br:not(.ProseMirror-trailingBreak)').count(), 0, 'selection metadata is not a hard break');
  }

  const copyMessage = async (message) => {
    await page.evaluate(message => { window.smoke.set(''); window.smoke.message({
      uid: 'copy-case-' + Date.now(), ts: 1, isMarkdown: true, role: 'assistant', ...message,
    }); }, message);
    const bubble = page.locator('.jx-msg').last();
    await bubble.hover();
    await bubble.getByTitle('复制内容', { exact: true }).click();
    await bubble.getByTitle('已复制', { exact: true }).waitFor();
    const plainText = await page.evaluate(async () => navigator.clipboard.readText());
    await editor.click(); await editor.press('Control+v');
    return plainText;
  };
  // Large streamed messages keep fragment boundaries without quadratic accumulation.
  const assembly = await page.evaluate(() => {
    const part = 'a'.repeat(10_000), segments = Array.from({ length: 500 }, () => ({ type: 'text', content: part }));
    const start = performance.now();
    const result = window.smoke.clipboard({ uid: 'large', role: 'assistant', ts: 1, isMarkdown: false, content: '', segments });
    const elapsed = performance.now() - start;
    const expected = Array.from({ length: 500 }, () => part).join('\n\n');
    const boundary = window.smoke.clipboard({ uid: 'boundaries', role: 'assistant', ts: 1, isMarkdown: false,
      content: '', segments: [{ type: 'text', content: '\n'.repeat(100_000) + 'a' }, { type: 'text', content: 'b' }] });
    return { equal: result.plainText === expected, elapsed,
      boundary: boundary.plainText === '\n'.repeat(100_000) + 'a\n\nb' };
  });
  assert.equal(assembly.equal, true);
  assert.equal(assembly.boundary, true);
  console.log('500-segment 5 MB clipboard assembly: ' + assembly.elapsed.toFixed(1) + ' ms');

  // Adversarial fidelity: each copied representation must survive another roundtrip.
  for (const content of ['first\nsecond', 'first\n\nsecond', 'a  b\n  c']) {
    assert.equal(await copyMessage({ content, isMarkdown: false }), content);
    assert.equal(await editor.innerText(), content);
    const draft = await page.evaluate(() => window.smoke.state().input);
    await copyMessage({ role: 'user', content: draft });
    assert.equal(await editor.innerText(), content, 'plain message whitespace survives repeated copying');
  }
  const headingDraft = await copyMessage({ content: 'a\nb\n===' });
  assert.equal(await editor.locator('h1').count(), 1);
  await copyMessage({ role: 'user', content: headingDraft });
  assert.equal(await editor.locator('h1').count(), 1, 'multiline Setext heading stays one heading');
  assert.equal(await editor.locator(':scope > *').count(), 1);

  const htmlExample = '<div>\na\nb\n</div>';
  assert.equal(await copyMessage({ role: 'user', content: htmlExample }), htmlExample);
  assert.equal(await editor.locator('pre code').textContent(), htmlExample);
  const htmlDraft = await page.evaluate(() => window.smoke.state().input);
  await copyMessage({ role: 'user', content: htmlDraft });
  assert.equal(await editor.locator('pre code').textContent(), htmlExample);

  for (const content of ['\\# heading', '\\- item', '\\+ item', '1\\. literal']) {
    const first = await copyMessage({ content });
    assert.equal(await editor.locator(':scope > p').count(), 1);
    const visible = await editor.innerText();
    await copyMessage({ role: 'user', content: first });
    assert.equal(await editor.locator(':scope > p').count(), 1, 'literal block marker stays a paragraph');
    assert.equal(await editor.innerText(), visible);
  }
  const nestedTask = '3. [x] parent\n   - [ ] child\n4. regular';
  const taskDraft = await copyMessage({ content: nestedTask });
  assert.equal(await editor.locator('ol > li ul[data-type=taskList] > li').count(), 1);
  await copyMessage({ role: 'user', content: taskDraft });
  assert.equal(await editor.locator('ol > li ul[data-type=taskList] > li').count(), 1, 'ordered child remains a task');
  const deepTask = '10. [x] root\n    99. [ ] parent\n        100. [x] child\n11. plain';
  const deepDraft = await copyMessage({ content: deepTask });
  await copyMessage({ role: 'user', content: deepDraft });
  assert.equal(await editor.locator('ol ol ol > li[data-checked=true]').count(), 1, JSON.stringify({deepDraft, dom: await editor.innerHTML()}));
  assert.equal(await page.evaluate(() => window.smoke.state().input), deepDraft, 'multidigit task nesting is stable');
  const fenced = '~~~~md\n```js\nexample\n```\n~~~~';
  const fenceDraft = await copyMessage({ content: fenced });
  assert.equal(await editor.locator('pre code').textContent(), '```js\nexample\n```');
  await copyMessage({ role: 'user', content: fenceDraft });
  assert.equal(await editor.locator('pre').count(), 1);
  assert.equal(await editor.locator('pre code').textContent(), '```js\nexample\n```', 'inner fences remain code text');

  // HTML code may carry structural BRs, which textContent alone would discard.
  for (const html of ['<p><code>a<br>b</code></p>', '<pre><code>a<br>b</code></pre>']) {
    await page.evaluate(() => window.smoke.set(''));
    await editor.evaluate((el, html) => {
      const data = new DataTransfer(); data.setData('text/html', html); data.setData('text/plain', 'a\nb');
      el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
    }, html);
    const draft = await page.evaluate(() => window.smoke.state().input);
    if (html.startsWith('<pre>')) assert.equal(await editor.locator('pre code').textContent(), 'a\nb');
    else assert.equal(await editor.locator('br:not(.ProseMirror-trailingBreak)').count(), 1);
    await copyMessage({ role: 'user', content: draft });
    if (html.startsWith('<pre>')) assert.equal(await editor.locator('pre code').textContent(), 'a\nb');
    else assert.equal(await editor.locator('br:not(.ProseMirror-trailingBreak)').count(), 1);
  }

  // Assistant HTML is presentation, while user examples remain literal source.
  assert.equal(await copyMessage({ content: '请执行 ssh root@<span>192.0.2.10</span>' }),
    '请执行 ssh root@192.0.2.10');
  assert.equal(await editor.innerText(), '请执行 ssh root@192.0.2.10');
  assert.equal(await page.evaluate(() => window.smoke.state().input), '请执行 ssh root@192.0.2.10');
  assert.equal(await editor.locator('p').count(), 1);
  assert.equal(await editor.locator('a').count(), 0, 'SSH destination is not a mail link');

  assert.equal(await copyMessage({ content: 'fallback', segments: [
    { type: 'text', content: '' }, { type: 'text', content: '第一段\n\n' },
    { type: 'text', content: '' }, { type: 'text', content: '第二段' },
    { type: 'text', content: '' },
  ] }), '第一段\n\n第二段', 'empty stream segments and existing separators do not add blanks');
  assert.equal(await page.evaluate(() => window.smoke.state().input), '第一段\n\n第二段');
  assert.equal(await editor.locator('p').count(), 2);

  for (const [content, expected, selector, count] of [
    ['<div>\n<p>第一段</p>\n<p>第二段</p>\n</div>\n', '第一段\n\n第二段', 'p', 2],
    ['第一行\n第二行', '第一行  \n第二行', 'br', 1],
    ['**粗体** 与 *斜体*', '**粗体** 与 *斜体*', 'strong', 1],
    ['- 第一项\n- 第二项', '- 第一项\n- 第二项', 'li', 2],
    ['- [x] 已完成\n- [ ] 待处理', '- [x] 已完成\n- [ ] 待处理', 'ul[data-type=taskList] > li', 2],
    ['- [x] 已完成\n- 普通条目', '- [x] 已完成\n- 普通条目', 'ul > li', 2],
    ['0. [x] 已完成\n1. [ ] 待处理', '0. [x] 已完成\n1. [ ] 待处理', 'ol > li', 2],
    ['3. [x] 已完成\n4. [ ] 待处理', '3. [x] 已完成\n4. [ ] 待处理', 'ol > li', 2],
    ['- [x] 主任务\n  - [ ] 子任务', '- [x] 主任务\n  - [ ] 子任务', 'ul input[type=checkbox]', 2],
    ['```html\n<span>代码</span>\n\n  tail\n```', '```html\n<span>代码</span>\n\n  tail\n```', 'pre', 1],
    ['`a  b`', '`a  b`', 'code', 1],
    ['`<span>代码</span>`', '`<span>代码</span>`', 'code', 1],
    ['[邮箱](mailto:person@example.com)', '[邮箱](mailto:person@example.com)', 'a[href^="mailto:"]', 1],
    ['[网站](https://example.com/)', '[网站](https://example.com/)', 'a', 1],
  ]) {
    assert.equal(await copyMessage({ content }), expected);
    assert.equal(await page.evaluate(() => window.smoke.state().input), expected);
    assert.equal(await editor.locator(selector).count(), count);
    // Repeated copying of the resulting user message must remain stable.
    assert.equal(await copyMessage({ role: 'user', content: expected }), expected);
    assert.equal(await page.evaluate(() => window.smoke.state().input), expected);
  }
  const table = '| a   | b   |\n| --- | --- |\n| c   | d   |';
  assert.equal(await copyMessage({ content: '| a | b |\n| --- | --- |\n| c | d |' }), table);
  assert.equal(await page.evaluate(() => window.smoke.state().input), table);
  assert.equal(await editor.locator('table').count(), 1);
  assert.equal(await editor.locator(':scope > p').count(), 0, 'table has no trailing placeholder');
  assert.equal(await copyMessage({ role: 'user', content: table }), table);
  assert.equal(await page.evaluate(() => window.smoke.state().input), table);
  assert.equal(await copyMessage({ content: '<p>第一段</p><p></p><p>第二段</p>' }), '第一段\n\n\n\n第二段');
  assert.equal(await editor.locator('p').count(), 3, 'authored empty paragraph remains');
  await copyMessage({ role: 'user', content: '<span>这是用户输入的 HTML 示例</span>' });
  assert.equal(await editor.innerText(), '<span>这是用户输入的 HTML 示例</span>');

  // Both typed and pasted SSH destinations remain text, including IPs and hostnames.
  for (const command of ['ssh root@192.0.2.10', 'ssh deploy@example.com']) {
    await page.evaluate(() => window.smoke.set(''));
    await editor.pressSequentially(command + ' ');
    assert.equal(await editor.locator('a').count(), 0);
    assert.equal(await page.evaluate(() => window.smoke.state().input), command + ' ');
    await editor.press('Enter');
    assert.equal(await page.evaluate(() => window.smoke.sent.at(-1)), command + ' ');
    assert.equal(await copyMessage({ role: 'user', content: command }), command);
    assert.equal(await page.evaluate(() => window.smoke.state().input), command);
    assert.equal(await editor.locator('a').count(), 0);
    await page.evaluate(() => window.smoke.set(''));
    await page.evaluate(command => navigator.clipboard.writeText(command), command);
    await editor.click(); await editor.press('Control+v');
    assert.equal(await page.evaluate(() => window.smoke.state().input), command);
    assert.equal(await editor.locator('a').count(), 0);
  }

  await page.evaluate(() => window.smoke.set(''));
  await editor.pressSequentially('https://example.com ');
  assert.equal(await editor.locator('a').count(), 1, 'ordinary web URL autolinking remains');

  // Native Ctrl+C from a rendered message, rather than the message copy button.
  for (const [content, selector, expected] of [
    ['仓库 lint 也有', 'p', '仓库 lint 也有'],
    ['- 协议标识、消息结构校验。\n- 校验消息来源窗口。', 'li', '- 协议标识、消息结构校验。'],
  ]) {
    await page.evaluate(content => { window.smoke.set(''); window.smoke.message({
      uid: 'selection-' + Date.now(), role: 'assistant', ts: 1, isMarkdown: true, content,
    }); }, content);
    await page.waitForFunction(() => document.querySelector('.jx-inputArea [contenteditable="true"]')?.textContent === '');
    const source = page.locator('.jx-msgText').last().locator(selector).first();
    await source.evaluate(el => {
      const range = document.createRange(); range.selectNodeContents(el);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    });
    await page.keyboard.press('Control+c');
    await editor.click();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await editor.press('Control+v');
    assert.equal(await page.evaluate(() => window.smoke.state().input), expected);
    assert.equal(await editor.locator(':scope > *').count(), 1, 'native selection paste has one content block');
    assert.equal(await editor.locator(':scope > p:empty').count(), 0);
  }

  // An authored boundary break is distinct from the browser selection marker.
  await page.evaluate(() => window.smoke.set(''));
  await editor.evaluate(el => {
    const data = new DataTransfer(); data.setData('text/html', '<p><br>正文<br></p>');
    data.setData('text/plain', '\n正文\n');
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  });
  assert.equal(await page.evaluate(() => window.smoke.state().input), '  \n正文  \n');
  assert.equal(await editor.locator('br:not(.ProseMirror-trailingBreak)').count(), 2);

  // Repeated editor copy/paste must not accumulate boundary whitespace.
  await page.evaluate(() => window.smoke.set('正文'));
  for (let cycle = 0; cycle < 4; cycle++) {
    await editor.click(); await editor.press('Control+a'); await editor.press('Control+c');
    await editor.press('Control+v');
    assert.equal(await page.evaluate(() => window.smoke.state().input), '正文');
    assert.equal(await editor.locator('p').count(), 1);
  }
  assert.deepEqual(errors, []);
  console.log('PASS: native message copy/paste, HTML semantics, SSH text, code/paragraphs, task/mixed/ordered/nested lists, table boundaries and repeated roundtrip');
} finally { await browser.close(); }
