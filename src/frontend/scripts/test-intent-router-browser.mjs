/**
 * 智能路由（mode_slug=auto）前端回归：
 *  1. 模式菜单：服务端名单里的「智能路由」出现在最前、和具体模式之间有分隔；选中它不改思考强度，
 *     模型位按用户自己选的强度显示；找不到模式时的兜底不会落到智能路由上。
 *  2. 工具卡：用真实的流解析（processChatStream）喂 intent_recognition 的 tool_call / tool_result，
 *     运行中显示「识别中」，完成后显示结果与耗时；展开后有问题 / 结果（从 X 切换）/ 理由。
 *  3. 换模式重答：点「换成」里的模式，对话的模式位改成它，并以 mode_slug=该模式 走 /regenerate。
 *  4. 历史：parseHistoryMessage 还原的同一张卡（source=fallback）显示「识别未完成，已使用 X」。
 *
 * 浏览器：PLAYWRIGHT_CHROMIUM_EXECUTABLE 指向本机已有的 Chromium（可选）。
 */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const output = resolve('node_modules/.tmp/intent-router-browser');
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await build({
  stdin: {
    resolveDir: process.cwd(),
    loader: 'tsx',
    contents: String.raw`
import React from 'react';
import './src/styles/index.ts';
import { createRoot } from 'react-dom/client';
import { useChatStore } from './src/stores/chatStore';
import { MessageBubble } from './src/components/chat/MessageBubble';
import { processChatStream } from './src/hooks/chatStream';
import { parseHistoryMessage } from './src/hooks/chatHistoryMessage';
import ChatModeSwitch from './src/components/chat/ChatModeSwitch';
import ModelEffortChip from './src/components/chat/ModelEffortChip';
import { requestChatRerun } from './src/api/chatReruns';
import { useChatModeStore } from './src/stores/chatModeStore';
import { useModelCapabilitiesStore } from './src/stores/modelCapabilitiesStore';

const mode = (slug, name, extra = {}) => ({ slug, name, description: name + ' 的说明', is_builtin: true,
  is_private: false, default_effort: 'fast', effort_locked: false, ...extra });
useChatModeStore.setState({ loaded: true, modes: [
  mode('auto', '智能路由', { is_router: true }),
  mode('standard', '标准模式'),
  mode('turbo', '极速模式', { effort_locked: true }),
  mode('ledger', '台账查询', { is_builtin: false }),
] });
const main = { provider_id: 'main', display_name: 'Main', model_name: 'main', provider: 'openai_compatible',
  is_default: true, supports_reasoning_effort: true, reasoning_effort_configured: true,
  reasoning_effort_levels: [{ key: 'medium', value: 'medium' }, { key: 'high', value: 'high' }],
  default_reasoning_effort: 'medium' };
useModelCapabilitiesStore.setState({ loaded: true, selectedModelProviderId: 'main',
  capabilities: { ...main, user_model_switch_enabled: false, user_selectable_models: [main] } });

const chatId = 'c1';
useChatStore.setState({
  currentChatId: chatId, chatMode: 'high', modeSlug: 'standard',
  store: { order: [chatId], chats: { [chatId]: { id: chatId, title: 'Intent', createdAt: 1, updatedAt: 1,
    modeSlug: 'standard', messages: [
      { role: 'user', uid: 'u1', ts: 1, content: '查一下某单位的编制数' },
    ] } } },
});

window.rerunCalls = [];
window.rerunBodies = [];
const realFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const url = String(input);
  if (url.includes('/v1/chats/c1/regenerate')) {
    window.rerunBodies.push(JSON.parse(String(init?.body)));
    return new Response('data: [DONE]\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
  }
  return realFetch(input, init);
};
const regenerate = (index) => {
  window.rerunCalls.push(index);
  void requestChatRerun(chatId, index, new AbortController().signal);
};

function Messages() {
  const messages = useChatStore((s) => s.store.chats[chatId]?.messages || []);
  return <>{messages.map((m, i) => (
    <MessageBubble key={m.uid} m={m} messageIndex={i} currentChatId={chatId} send={() => {}}
      exportChatRecord={async () => {}} regenerate={regenerate} />
  ))}</>;
}
function History({ message }) {
  return <div id="history"><MessageBubble m={message} messageIndex={0} currentChatId="h1" send={() => {}}
    exportChatRecord={async () => {}} /></div>;
}
function App() {
  const [history, setHistory] = React.useState(null);
  window.showHistory = (raw) => setHistory(parseHistoryMessage(raw));
  return <div style={{ width: 720, padding: 16 }}>
    <div id="seat" style={{ paddingTop: 360 }}><ChatModeSwitch /><ModelEffortChip /></div>
    <div id="live"><Messages /></div>
    {history && <History message={history} />}
  </div>;
}
createRoot(document.getElementById('root')).render(<App />);

const frame = (e) => 'data: ' + JSON.stringify(e) + '\n\n';
let push;
const body = new ReadableStream({ start(controller) {
  push = (text) => controller.enqueue(new TextEncoder().encode(text));
  window.endStream = () => controller.close();
} });
window.startStream = () => {
  window.streamDone = processChatStream(new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }),
    { chatId, enableThinking: false }).then(() => true);
  push(frame({ type: 'run_started', run_id: 'run-1' }));
  push(frame({ type: 'tool_call', tool_name: 'intent_recognition', tool_display_name: '用户意图识别',
    tool_args: { question: '查一下某单位的编制数' }, tool_id: 'intent_1' }));
};
window.finishStream = () => {
  push(frame({ type: 'tool_result', tool_id: 'intent_1', tool_name: 'intent_recognition', status: 'success',
    result: { mode_slug: 'turbo', mode_name: '极速模式', reason: '问题是一次简单检索', confidence: 0.92,
      source: 'llm', switched: true, previous_mode_slug: 'standard',
      candidates: [{ slug: 'standard', name: '标准模式' }, { slug: 'turbo', name: '极速模式' }, { slug: 'ledger', name: '台账查询' }],
      latency_ms: 1234, fallback_cause: null } }));
  push(frame({ type: 'content', delta: '该单位核定编制 42 名。' }));
  push('data: [DONE]\n\n');
  window.endStream();
};
window.chatState = () => {
  const s = useChatStore.getState();
  return { chatMode: s.chatMode, modeSlug: s.modeSlug, chatModeSlug: s.store.chats[chatId]?.modeSlug,
    fallback: useChatModeStore.getState().modeOf('gone').slug };
};
`,
  },
  // splitting：动态 import 拆成独立 chunk。不拆的话 esbuild 会把整条店铺链包成惰性 __esm 初始化，
  // chatStore ↔ modelCapabilitiesStore ↔ chatSupport 的顶层 await 循环就互相等死（Vite 的原生 ESM 没这问题）。
  bundle: true, splitting: true, jsx: 'automatic', format: 'esm', outdir: output, entryNames: 'fixture',
  loader: { '.png': 'dataurl', '.svg': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.css': 'css' },
  external: ['/home/*', '/icons/*', '/fonts/*'],
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
});

const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
try {
  const page = await browser.newPage({ viewport: { width: 1000, height: 900 } });
  const errors = [];
  page.on('pageerror', (error) => { errors.push(error.message); console.error('PAGE', error.message); });
  page.on('console', (msg) => { if (msg.type() === 'error') console.error('CONSOLE', msg.text()); });
  await page.route('https://intent.test/**', async (route) => {
    const url = new URL(route.request().url());
    if (/^\/[\w-]+\.(js|css)$/.test(url.pathname)) {
      const css = url.pathname.endsWith('.css');
      return route.fulfill({ contentType: css ? 'text/css' : 'application/javascript', body: await readFile(resolve(output, url.pathname.slice(1))) });
    }
    if (route.request().resourceType() === 'document') {
      return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
    }
    return route.fulfill({ contentType: 'application/json', body: '{"code":0,"data":{}}' });
  });
  await page.addInitScript(() => localStorage.setItem('jx_lang', 'zh-CN'));
  await page.goto('https://intent.test/');
  await page.waitForFunction(() => typeof window.startStream === 'function');

  // ── 1. 模式菜单 ──
  const seatBtn = page.locator('.jx-modeSeatBtn');
  assert.equal(await seatBtn.innerText(), '标准模式');
  await seatBtn.click();
  const titles = await page.locator('.jx-modeSeatMenu .jx-menuOption-title').allTextContents();
  assert.deepEqual(titles, ['智能路由', '标准模式', '极速模式', '台账查询'], 'router listed first');
  assert.equal(await page.locator('.jx-modeSeatMenu .jx-modeSeatDivider').count(), 1);
  await page.getByRole('menuitemradio', { name: /智能路由/ }).click();
  let state = await page.evaluate(() => window.chatState());
  assert.equal(state.modeSlug, 'auto');
  assert.equal(state.chatModeSlug, 'auto', 'mode follows the conversation');
  assert.equal(state.chatMode, 'high', 'picking the router keeps the user effort');
  assert.equal(state.fallback, 'standard', 'unknown slug never falls back to the router');
  assert.match(await seatBtn.getAttribute('class'), /\brouter\b/);
  const effortChip = page.getByRole('button', { name: '切换模型与思考强度' });
  assert.match(await effortChip.innerText(), /思考·高/, 'effort chip shows the user effort under auto');

  // ── 2. 流式工具卡 ──
  await page.evaluate(() => window.startStream());
  const shellHead = page.locator('#live .jx-trs-head').first();
  await shellHead.waitFor();
  assert.match(await shellHead.innerText(), /正在识别意图/);
  await shellHead.click();
  const row = page.locator('#live .jx-tcr').first();
  await row.locator('.jx-tcr-progress', { hasText: '识别中' }).waitFor();
  assert.match(await row.locator('.jx-tcr-label').innerText(), /用户意图识别/);
  assert.equal(await row.locator('.jx-tcr-kind').count(), 1, 'route icon rendered');
  await page.evaluate(() => window.finishStream());
  await page.evaluate(() => window.streamDone);
  await row.locator('.jx-tcr-progress', { hasText: '1.2 秒' }).waitFor();
  assert.match(await row.locator('.jx-tcr-value').innerText(), /极速模式/);
  await row.locator('.jx-tcr-header').click();
  const body = row.locator('.jx-intentRoute');
  await body.waitFor();
  const text = await body.innerText();
  assert.match(text, /问题\s*查一下某单位的编制数/);
  assert.match(text, /进入「极速模式」（从「标准模式」切换）/);
  assert.match(text, /理由\s*问题是一次简单检索/);
  const options = await body.locator('.jx-intentRoute-optionBtn').allInnerTexts();
  assert.deepEqual(options.map((s) => s.replace(/\s+/g, '')), ['标准模式', '台账查询'], 'alternatives exclude the current result');
  await page.waitForTimeout(600); // 等展开过渡走完再截图（截图供人工复核浅色 / 深色）
  await page.screenshot({ path: resolve(output, 'tool-card.png'), fullPage: true });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await page.waitForTimeout(400);
  await page.screenshot({ path: resolve(output, 'tool-card-dark.png'), fullPage: true });
  await page.evaluate(() => document.documentElement.removeAttribute('data-theme'));

  // ── 3. 换模式重答 ──
  await body.getByRole('button', { name: '台账查询' }).click();
  await page.waitForFunction(() => window.rerunBodies.length === 1);
  const calls = await page.evaluate(() => window.rerunCalls);
  assert.deepEqual(calls, [1], 'reruns the assistant turn');
  const [rerun] = await page.evaluate(() => window.rerunBodies);
  assert.equal(rerun.mode_slug, 'ledger');
  assert.equal(rerun.message_index, 1);
  assert.equal(rerun.chat_mode, 'high');
  state = await page.evaluate(() => window.chatState());
  assert.equal(state.modeSlug, 'ledger', 'leaves smart routing');
  assert.equal(state.chatModeSlug, 'ledger');
  assert.equal(await seatBtn.innerText(), '台账查询');

  // ── 4. 刷新后的历史卡（fallback） ──
  await page.evaluate(() => window.showHistory({
    role: 'assistant', message_id: 'm-h', content: '答复', created_at: '2026-11-27T00:00:00Z',
    tool_calls: [{ tool_id: 'intent_h', tool_name: 'intent_recognition', tool_display_name: '用户意图识别',
      tool_args: { question: '帮我写一份通知' }, status: 'success',
      result: { mode_slug: 'standard', mode_name: '标准模式', reason: '', confidence: null, source: 'fallback',
        switched: false, previous_mode_slug: null, candidates: [{ slug: 'standard', name: '标准模式' }, { slug: 'turbo', name: '极速模式' }],
        latency_ms: 3000, fallback_cause: 'timeout' } }],
  }));
  const hShell = page.locator('#history .jx-trs-head').first();
  await hShell.waitFor();
  assert.match(await hShell.textContent(), /用户意图识别/);
  await hShell.click();
  const hRow = page.locator('#history .jx-tcr').first();
  assert.match(await hRow.locator('.jx-tcr-progress').innerText(), /3\.0 秒/);
  await hRow.locator('.jx-tcr-header').click();
  const hBody = hRow.locator('.jx-intentRoute');
  await hBody.waitFor();
  assert.match(await hBody.innerText(), /识别未完成，已使用「标准模式」/);
  assert.equal(await hBody.locator('.jx-intentRoute-optionBtn').count(), 0, 'no rerun without a regenerate handler');

  assert.deepEqual(errors, []);
  await page.screenshot({ path: resolve(output, 'verified.png'), fullPage: true });
  console.log('Browser: intent router menu, streamed + history tool card, switch-mode rerun: OK');
} finally {
  await browser.close();
}
