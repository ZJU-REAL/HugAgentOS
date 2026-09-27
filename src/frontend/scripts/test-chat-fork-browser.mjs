/** Real browser gestures against production composer/message components; HTTP is a fixture. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const playwright = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const chromium = playwright.chromium ?? playwright.default?.chromium;
const output = resolve('node_modules/.tmp/chat-fork-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: { contents: "\nimport React, { useRef } from 'react';\nimport { createRoot } from 'react-dom/client';\nimport { InputArea } from './src/components/chat/InputArea';\nimport { MessageBubble } from './src/components/chat/MessageBubble';\nimport { ChatForkBanner } from './src/components/chat/ChatForkBanner';\nimport { useChatStore } from './src/stores/chatStore';\nimport { useCatalogStore } from './src/stores/catalogStore';\nimport './src/styles/variables.css';\nimport './src/styles/chat.css';\n\nconst reply = { uid: 'reply-1', messageId: 'a1', role: 'assistant', content: 'Violet remembered.', ts: 1, isMarkdown: false };\nfunction reset() {\n  history.replaceState(null, '', '/c/source');\n  useChatStore.setState({ currentUserId: 'owner', currentChatId: 'source', input: '',\n    backendSessionIds: new Set(['source']), loadedMsgIds: new Set(['source']),\n    activeRuns: {}, sendingChatIds: new Set(), sending: false,\n    store: { order: ['source'], chats: { source: { id: 'source', title: 'Original',\n      createdAt: 1, updatedAt: 1, messages: [reply], runTarget: 'cloud' } } },\n  });\n  useCatalogStore.getState().setPanel('chat');\n}\nwindow.__forkSmoke = { reset, state: () => useChatStore.getState(), sends: 0 };\nreset();\nfunction Fixture() {\n  const inputRef = useRef(null), fileRef = useRef(null);\n  const id = useChatStore(s => s.currentChatId);\n  const messages = useChatStore(s => s.store.chats[id]?.messages ?? []);\n  return <main style={{padding: 32, maxWidth: 900}}>\n    <ChatForkBanner chatId={id} />\n    {messages.filter(m => m.role === 'assistant').map((m, index) =>\n      <MessageBubble key={m.uid} m={m} messageIndex={index} currentChatId={id}\n        send={() => window.__forkSmoke.sends++} exportChatRecord={async () => {}} />)}\n    <InputArea inputRef={inputRef} fileInputRef={fileRef}\n      send={() => window.__forkSmoke.sends++} handleFileSelect={() => {}} removeFile={() => {}} />\n  </main>;\n}\ncreateRoot(document.getElementById('root')).render(<Fixture />);\n", resolveDir: process.cwd(), loader: 'tsx' },
  outfile: resolve(output, 'fixture.js'), bundle: true, format: 'esm', jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  loader: { '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.css': 'empty', '.svg': 'dataurl' },
});
const origin = 'http://127.0.0.1:5198';
const browser = await chromium.launch({
  headless: true,
  ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}),
});
const calls = [], errors = [];
let serial = 0;
const sessions = new Map();
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  page.on('pageerror', e => errors.push(e.message));
  await page.route(origin + '/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/fixture.js') return route.fulfill({ contentType: 'text/javascript', body: await readFile(resolve(output, 'fixture.js')) });
    if (url.pathname.endsWith('/fork')) {
      const body = route.request().postDataJSON();
      calls.push(body);
      const id = 'branch-' + ++serial;
      const session = { chat_id: id, user_id: 'owner', title: 'Original · 分支',
        project_id: null, message_count: 2, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        metadata: { fork: { source_chat_id: 'source', source_message_id: body.through_message_id || 'a1',
          source_chat_seq: 2, source_title: 'Original', message_count: 2, created_at: new Date().toISOString() } } };
      sessions.set(id, session);
      return route.fulfill({ status: 201, json: { code: 20100, data: session } });
    }
    if (/\/chats\/[^/]+\/messages$/.test(url.pathname)) {
      return route.fulfill({ json: { code: 20000, data: { items: [
        { message_id: 'copy-u', role: 'user', content: 'Remember violet', created_at: new Date().toISOString(), metadata: { forked_history: true } },
        { message_id: 'copy-a', role: 'assistant', content: 'Violet remembered.', created_at: new Date().toISOString(), metadata: { forked_history: true } },
      ], pagination: { has_next: false } } } });
    }
    if (/\/chats\/[^/]+$/.test(url.pathname)) {
      const id = url.pathname.split('/').at(-1);
      return route.fulfill({ json: { code: 20000, data: sessions.get(id) || { chat_id: id, title: 'Original', metadata: {} } } });
    }
    if (url.pathname.startsWith('/api')) return route.fulfill({ json: { code: 20000, data: { items: [] } } });
    return route.fulfill({ contentType: 'text/html', body: '<html><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
  });
  await page.goto(origin + '/c/source');
  await page.waitForFunction(() => !!window.__forkSmoke);
  const editor = page.locator('[contenteditable="true"]');
  const reset = async () => {
    await page.evaluate(() => window.__forkSmoke.reset());
    await page.waitForFunction(() => window.__forkSmoke.state().currentChatId === 'source');
    await editor.fill('');
  };
  const branched = async expected => {
    await page.waitForFunction(id => window.__forkSmoke.state().currentChatId === id, 'branch-' + expected);
    assert.equal(calls.length, expected);
    assert.equal(await page.evaluate(() => window.__forkSmoke.sends), 0, 'commands must not call model generation');
    await page.getByRole('button', { name: 'Original', exact: true }).waitFor();
  };

  // The visible slash menu carries both the requested label and description.
  await editor.fill('/fork');
  const option = page.getByRole('option').filter({ hasText: '创建聊天分支' });
  await option.waitFor();
  assert.match(await option.innerText(), /为此聊天创建分支/);
  await option.click();
  await branched(1);
  assert.equal(calls[0].through_message_id, undefined);

  // Keyboard selection uses the same endpoint.
  await reset();
  await editor.fill('/fork');
  await editor.press('Enter');
  await branched(2);

  // With suggestions dismissed, Enter still intercepts the exact command.
  await reset();
  await editor.fill('/fork');
  await editor.press('Escape');
  await editor.press('Enter');
  await branched(3);

  // The button is mounted in the actual MessageBubble action toolbar.
  await reset();
  await editor.fill('keep this draft');
  await page.getByRole('button', { name: '创建聊天分支', exact: true }).click();
  await branched(4);
  assert.equal(calls[3].through_message_id, 'a1');
  assert.equal(await page.evaluate(() => window.__forkSmoke.state().input), 'keep this draft');

  assert.deepEqual(errors, []);
  console.log('PASS: browser slash menu/click, keyboard selection, hidden-menu Enter, real message toolbar icon, source banner and draft preservation');
} finally {
  await browser.close();
}
