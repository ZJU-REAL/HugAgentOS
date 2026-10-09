import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = resolve('node_modules/.tmp/thinking-replay-browser');
await mkdir(output, { recursive: true });
await build({
  stdin: {
    resolveDir: process.cwd(),
    contents: String.raw`
      import { processChatStream } from './src/hooks/chatStream';
      import { useChatStore } from './src/stores/chatStore';
      import { streamResumeSeed } from './src/utils/streamResume';
      window.runReplay = async (automationRun) => {
        const chatId = automationRun ? 'scheduled-chat' : 'ordinary-chat';
        const base = {
          uid: 'persisted-assistant', messageId: 'assistant-id',
          role: 'assistant', ts: 1, content: 'Private reasoning prefix',
          inFlight: { eventOffset: 2 },
        };
        useChatStore.setState({
          currentChatId: chatId,
          store: {
            chats: { [chatId]: {
              id: chatId, title: 'Replay', createdAt: 1, updatedAt: 1,
              automationRun, messages: [base],
            } },
            order: [chatId],
          },
        });
        const events = [
          { type: 'run_started', run_id: 'test-run', message_id: 'assistant-id' },
          { type: 'content', delta: 'Private reasoning prefix' },
          { type: 'content', delta: ' continued</thi' },
          { type: 'content', delta: 'nk>Public answer' },
          { type: 'meta', message_id: 'assistant-id', usage: {} },
        ];
        const data = events.map(e => 'data: ' + JSON.stringify(e) + '\n\n').join('')
          + 'data: [DONE]\n\n';
        const response = new Response(data, {
          headers: { 'Content-Type': 'text/event-stream' },
        });
        const seedFrom = streamResumeSeed(base);
        const result = await processChatStream(response, {
          chatId, enableThinking: true, seedFrom,
        });
        return {
          result,
          messages: useChatStore.getState().store.chats[chatId].messages,
        };
      };
    `,
  },
  outfile: resolve(output, 'fixture.js'),
  bundle: true, jsx: "automatic", format: "esm", loader: { ".css": "empty" },
  define: { 'import.meta.env': '{}' },
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE });
try {
  const page = await browser.newPage();
  page.on("pageerror", error => console.error(error.message));
  await page.route('https://thinking.test/**', async route => {
    if (new URL(route.request().url()).pathname === '/fixture.js') {
      await route.fulfill({
        contentType: 'text/javascript',
        body: await readFile(resolve(output, 'fixture.js'), 'utf8'),
      });
    } else if (route.request().resourceType() === 'document') {
      await route.fulfill({
        contentType: 'text/html',
        body: '<script type="module" src="/fixture.js"></script>',
      });
    } else {
      await route.fulfill({
        contentType: 'application/json', body: '{"code":0,"data":{}}',
      });
    }
  });
  await page.goto('https://thinking.test/');
  await page.waitForFunction(() => typeof window.runReplay === 'function');
  for (const scheduled of [false, true]) {
    const outcome = await page.evaluate(value => window.runReplay(value), scheduled);
    assert.equal(outcome.result.full, 'Public answer');
    const messages = outcome.messages.filter(m => m.role === 'assistant');
    assert.equal(messages.length, 1, 'Replay must adopt the durable assistant row');
    assert.equal(messages[0].content, 'Public answer');
    assert.equal(
      messages[0].thinking.map(b => b.content).join(''),
      'Private reasoning prefix continued',
    );
  }
  console.log('Ordinary and scheduled thinking replay passed');
} finally {
  await browser.close();
}
