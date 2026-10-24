// Selecting text in the canvas file preview → toolbar → quote / quick prompt lands in the chat composer.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/canvas-quote-browser');
await mkdir(output, { recursive: true });
await build({ plugins:[{name:'asset-url',setup(build){build.onResolve({filter:new RegExp('[?]url$')},args=>({path:args.path,namespace:'asset-url'}));build.onLoad({filter:/.*/,namespace:'asset-url'},()=>({contents:'export default "/mock-worklet.js"',loader:'js'}));}}], stdin: { contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { InputArea } from './src/components/chat/InputArea';
import { MessageBubble } from './src/components/chat/MessageBubble';
import { RightSidebarPanel } from './src/components/canvas/RightSidebarPanel';
import { useCanvasStore } from './src/stores/canvasStore';
import { useChatStore } from './src/stores/chatStore';
import { readComposer, chatDraftKey } from './src/stores/composerStore';
import { quoteToWire } from './src/utils/quotedFollowUp';
import './src/styles/variables.css';
import './src/styles/common.css';
import './src/styles/chat.css';
import './src/styles/canvas.css';

useChatStore.setState({ currentUserId: 'quote-owner', currentChatId: 'a',
  store: { order: ['a'], chats: { a: { id: 'a', title: 'a', createdAt: 1, updatedAt: 1, messages: [] } } } });
function Shell() {
  const inputRef = React.useRef(null), fileRef = React.useRef(null);
  return <main className="jx-appMainLayout" style={{display:'flex',height:'100vh'}}>
    <div className="jx-primaryPane is-chatSurface" style={{flex:1}}>
    <MessageBubble m={{ role: 'assistant', uid: 'm1', ts: 4242, content: '助手回答里的一句结论，可以被追问。', isMarkdown: true }}
      messageIndex={0} currentChatId="a" send={() => {}} exportChatRecord={async () => {}} />
    <div className="jx-chatFooter">
      <InputArea inputRef={inputRef} fileInputRef={fileRef} send={() => {}} handleFileSelect={() => {}} removeFile={() => {}} />
    </div></div>
    <div className="jx-canvasPanelSlot" style={{display:'flex',flex:'none',height:'100%'}}><RightSidebarPanel /></div>
  </main>;
}
const router = createMemoryRouter([{ path: '*', element: <Shell /> }], { initialEntries: ['/'] });
window.fixture = {
  canvas: useCanvasStore, router,
  draft: () => { const d = readComposer(chatDraftKey('a')); return { input: d.input, quote: d.quotedFollowUp && quoteToWire(d.quotedFollowUp) }; },
  clearQuote: () => readComposer(chatDraftKey('a')).setQuotedFollowUp(null),
};
createRoot(document.getElementById('root')).render(<RouterProvider router={router} />);
`, resolveDir:process.cwd(),loader:'tsx'},outfile:resolve(output,'fixture.js'),bundle:true,format:'esm',jsx:'automatic',define:{'import.meta.env':'{}'},external:['/loader.gif','/loader-done.png'],loader:{'.woff':'dataurl','.woff2':'dataurl','.ttf':'dataurl'}});

const MARKDOWN = '# 第一章 总览\n\n## 1.1 背景\n\n这是一段需要被引用的原文内容，用来验证选区工具条。\n';
const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}) });
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('https://quote.test/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/fixture.js' || path === '/fixture.css') return route.fulfill({ contentType: path.endsWith('.js') ? 'text/javascript' : 'text/css', body: await readFile(resolve(output, path.slice(1))) });
    const text = path.match(/\/v1\/file\/([^/]+)\/text$/);
    if (text) return route.fulfill({ json: { code: 0, data: { file_id: text[1], name: 'notes.md', content: MARKDOWN, revision: 'r1', size: MARKDOWN.length, can_edit: false } } });
    if (/^(\/api)?\/files\//.test(path)) return route.fulfill({ contentType: 'text/markdown', body: MARKDOWN });
    if (path.startsWith('/api')) return route.fulfill({ json: { code: 0, data: [] } });
    return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body style="margin:0"><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto('https://quote.test/');
  await page.evaluate(() => window.fixture.canvas.getState().openCanvas({ file_id: 'notes', name: 'notes.md', url: '/files/notes', mime_type: 'text/markdown' }));
  const paragraph = page.locator('.jx-canvas-body p', { hasText: '需要被引用的原文' });
  await paragraph.waitFor();

  const select = async (target = paragraph) => {
    const box = await target.boundingBox();
    await page.mouse.move(box.x + 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + Math.min(box.width - 2, 260), box.y + box.height / 2, { steps: 8 });
    await page.mouse.up();
    return page.getByRole('toolbar', { name: '选中内容操作' });
  };

  // 1. Quick prompt: quotes the selection with its file + heading, prefills (does not send).
  let toolbar = await select();
  await toolbar.waitFor();
  const labels = await toolbar.getByRole('button').allInnerTexts();
  assert.deepEqual(labels.map(s => s.trim()), ['复制', '引用到对话', '解释', '改写', '翻译']);
  await toolbar.getByRole('button', { name: '解释' }).dispatchEvent('mousedown', { button: 0 });
  await page.waitForFunction(() => !!window.fixture.draft().quote);
  let draft = await page.evaluate(() => window.fixture.draft());
  assert.equal(draft.input, '解释这段内容');
  assert.equal(draft.quote.source.kind, 'file');
  assert.equal(draft.quote.source.file_id, 'notes');
  assert.equal(draft.quote.source.file_name, 'notes.md');
  assert.equal(draft.quote.source.locator, '1.1 背景');
  assert.ok(draft.quote.text.startsWith('这是一段'), draft.quote.text);
  await page.getByRole('toolbar', { name: '选中内容操作' }).waitFor({ state: 'detached' });
  const chip = page.locator('.jx-inputQuote');
  await chip.waitFor();
  assert.equal((await chip.locator('.jx-inputQuoteBadge').innerText()).trim(), '文件引用');
  assert.equal((await chip.locator('.jx-inputQuoteSource').innerText()).trim(), 'notes.md · 1.1 背景');
  assert.equal((await page.locator('.jx-composerEditor').innerText()).trim(), '解释这段内容');
  assert.ok(await page.evaluate(() => document.activeElement?.classList.contains('jx-composerEditor')), '引用后光标应在输入框');
  await page.screenshot({ path: resolve(output, 'quoted.png') });

  // 2. Plain quote replaces the previous one.
  await page.evaluate(() => window.fixture.clearQuote());
  toolbar = await select();
  await toolbar.waitFor();
  await toolbar.getByRole('button', { name: '引用到对话' }).dispatchEvent('mousedown', { button: 0 });
  await page.waitForFunction(() => !!window.fixture.draft().quote);
  draft = await page.evaluate(() => window.fixture.draft());
  assert.equal(draft.quote.source.file_name, 'notes.md');

  // 3. Chat messages share the same toolbar: copy + follow-up quote with a message source.
  await page.evaluate(() => window.fixture.clearQuote());
  toolbar = await select(page.locator('.jx-msgContent p', { hasText: '助手回答' }));
  await toolbar.waitFor();
  assert.deepEqual((await toolbar.getByRole('button').allInnerTexts()).map(s => s.trim()), ['复制', '追问']);
  await toolbar.getByRole('button', { name: '追问' }).dispatchEvent('mousedown', { button: 0 });
  await page.waitForFunction(() => !!window.fixture.draft().quote);
  draft = await page.evaluate(() => window.fixture.draft());
  assert.deepEqual(draft.quote.source, { kind: 'message', ts: 4242 });
  assert.equal((await page.locator('.jx-inputQuoteBadge').innerText()).trim(), '追问引用');

  // 4. Canvas opened outside a chat (an app page): copy only.
  await page.evaluate(() => window.fixture.router.navigate('/apps/demo'));
  toolbar = await select();
  await toolbar.waitFor();
  assert.deepEqual((await toolbar.getByRole('button').allInnerTexts()).map(s => s.trim()), ['复制']);

  assert.deepEqual(errors, []);
  console.log('PASS: canvas selection → toolbar → file quote + prompt in composer; copy-only outside a chat');
} finally {
  await browser.close();
}
