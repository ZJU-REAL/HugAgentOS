/**
 * 评价判定（evaluation gate）前端回归，GET /v1/chats/{id}/feedback 全部由 page.route 模拟：
 *  1. gating：判定未出结果时不显示评价卡，每约 1.5 秒重读一次；判定为需要评价后显示评价卡。
 *  2. not_needed：不显示评价卡，只显示快捷赞 / 踩；点赞后保存为满意，重读后仍保持选中；
 *     服务端不再列出（关闭判定 / 结论变化）后按最新快照恢复评价卡。
 *  3. 已有详细评价（需修改 / 带原因）的回答即使列为 not_needed 也保留评价卡。
 *  4. 旧服务端（没有 not_needed / gating / gate_enabled）：立即显示评价卡，不轮询，行为与原来一致。
 *  5. 切换会话 / 切换账号（reset）后停止轮询；离开期间结束的回答，回到该会话时重新读取。
 *  6. 判定开启（gate_enabled=true）时，回答流式结束的那一帧就不显示评价卡，直到读回判定结果；
 *     未知 / 关闭时与原来一样立即显示。
 *  7. 没有会话评价权限的用户不轮询（界面不受影响）。
 *  8. （假时钟）服务端仍列为 gating 时一直轮询、不显示卡；请求一直失败时每条回答各自在 75 秒后显示卡。
 *
 * 浏览器：PLAYWRIGHT_CHROMIUM_EXECUTABLE 指向本机已有的 Chromium（可选）。
 */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const output = resolve('node_modules/.tmp/evaluation-gate-browser');
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
const CHATS = ['gate', 'skip', 'revised', 'old', 'left', 'other', 'away', 'away2', 'live', 'liveOld', 'plain', 'cap'];
await build({
  stdin: {
    resolveDir: process.cwd(),
    loader: 'tsx',
    contents: String.raw`
import React from 'react';
import './src/styles/index.ts';
import { createRoot } from 'react-dom/client';
import { useAuthStore } from './src/stores/authStore';
import { useChatStore } from './src/stores/chatStore';
import { useEvaluationStore } from './src/stores/evaluationStore';
import { useEvaluationScope } from './src/hooks/useEvaluationScope';
import { MessageActions } from './src/components/chat/MessageActions';

useAuthStore.setState({ authUser: { user_id: 'u1', username: 'u1', can_use_evaluation: true } });
const chats = {};
for (const id of ${JSON.stringify(CHATS)}) {
  chats[id] = { id, title: id, createdAt: 1, updatedAt: 1, messages: [
    { role: 'user', uid: id + '-u', ts: 1, content: '问题' },
    { role: 'assistant', uid: id + '-a', messageId: 'm-' + id, ts: 2, content: '回答' },
  ] };
}
useChatStore.setState({ currentChatId: 'gate', store: { order: Object.keys(chats), chats } });

function Chat({ chatId }) {
  useEvaluationScope(chatId);
  const messages = useChatStore((s) => s.store.chats[chatId]?.messages || []);
  return <div id="chat" data-chat={chatId}>{messages.map((m, i) => (
    <div key={m.uid} className="jx-msgRow" data-uid={m.uid}>
      <MessageActions m={m} messageIndex={i} currentChatId={chatId} exportChatRecord={async () => {}} />
    </div>
  ))}</div>;
}
function App() {
  const [chatId, setChatId] = React.useState(window.startChat || 'gate');
  window.openChat = (id) => setChatId(id);
  return <div style={{ width: 720, padding: 16 }}><Chat key="chat" chatId={chatId} /></div>;
}
createRoot(document.getElementById('root')).render(<App />);
window.evalState = () => {
  const s = useEvaluationStore.getState();
  return { gating: Object.keys(s.gating), notNeeded: Object.keys(s.notNeeded), records: s.records };
};
window.resetEvaluations = () => useEvaluationStore.getState().reset();
window.reloadChat = (id) => useEvaluationStore.getState().loadChat(id, { force: true });
window.setEvaluationPermission = (on) => useAuthStore.setState({
  authUser: { user_id: 'u1', username: 'u1', can_use_evaluation: on } });
/** Stream a new answer into the chat; finish() flips it to done in one store update. */
window.streamAnswer = (chatId, n) => {
  const uid = chatId + '-a' + n;
  const update = (patch) => useChatStore.setState((s) => {
    const chat = s.store.chats[chatId];
    const messages = chat.messages.filter((m) => m.uid !== uid);
    messages.push({ role: 'assistant', uid, ts: 3, content: '新回答', ...patch });
    return { store: { ...s.store, chats: { ...s.store.chats, [chatId]: { ...chat, messages } } } };
  });
  update({ isStreaming: true });
  return window.streaming = {
    finish: () => new Promise((done) => {
      update({ isStreaming: false, messageId: 'm-' + chatId + '-' + n });
      // What the first painted frame after the answer finished shows.
      requestAnimationFrame(() => done(document.querySelectorAll('#chat .jx-evalCard').length));
    }),
  };
};
`,
  },
  bundle: true, splitting: true, jsx: 'automatic', format: 'esm', outdir: output, entryNames: 'fixture',
  loader: { '.png': 'dataurl', '.svg': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.css': 'css' },
  external: ['/home/*', '/icons/*', '/fonts/*'],
  define: { 'import.meta.env': '{"VITE_DEFAULT_LANGUAGE":"zh-CN"}' },
});

const reply = (extra = {}) => ({ items: {}, excluded: [], not_needed: [], gating: [], gate_enabled: true, ...extra });

/** A page on the fixture with scripted feedback replies per chat: each GET takes the next one (the last repeats). */
async function openPage(browser, replies, { clock = false, startChat } = {}) {
  const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
  const state = { replies, gets: Object.fromEntries(CHATS.map((id) => [id, 0])), posts: [], failing: new Set(), errors: [] };
  page.on('pageerror', (error) => { state.errors.push(error.message); console.error('PAGE', error.message); });
  await page.route('https://gate.test/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (/^\/[\w-]+\.(js|css)$/.test(url.pathname)) {
      const css = url.pathname.endsWith('.css');
      return route.fulfill({ contentType: css ? 'text/css' : 'application/javascript', body: await readFile(resolve(output, url.pathname.slice(1))) });
    }
    if (request.resourceType() === 'document') {
      return route.fulfill({ contentType: 'text/html', body: '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>' });
    }
    const feedback = url.pathname.match(/^\/api\/v1\/chats\/([\w-]+)\/feedback$/);
    if (feedback && request.method() === 'GET') {
      const id = feedback[1];
      state.gets[id] += 1;
      if (state.failing.has(id)) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"code":500,"message":"down"}' });
      const list = state.replies[id] ?? [reply()];
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 0, data: list[Math.min(state.gets[id] - 1, list.length - 1)] }) });
    }
    const rate = url.pathname.match(/^\/api\/v1\/chats\/messages\/([\w-]+)\/feedback$/);
    if (rate && request.method() === 'POST') {
      const body = JSON.parse(request.postData() || '{}');
      state.posts.push({ messageId: rate[1], ...body });
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 0, data: { verdict: body.rating, reasons: [], comment: body.comment ?? '' } }) });
    }
    return route.fulfill({ contentType: 'application/json', body: '{"code":0,"data":{}}' });
  });
  if (clock) await page.clock.install({ time: new Date('2026-11-27T00:00:00Z') });
  await page.addInitScript((chat) => {
    localStorage.setItem('jx_lang', 'zh-CN');
    if (chat) window.startChat = chat;
  }, startChat ?? null);
  await page.goto('https://gate.test/');
  await page.waitForFunction(() => typeof window.openChat === 'function');
  return { page, state };
}

const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
try {
  const { page, state } = await openPage(browser, {
    gate: [reply({ gating: ['m-gate'] }), reply({ gating: ['m-gate'] }), reply()],
    skip: [reply({ not_needed: ['m-skip'] })],
    revised: [reply({ not_needed: ['m-revised'],
      items: { 'm-revised': { verdict: 'revise', reasons: ['accuracy'], comment: '数据口径不对' } } })],
    old: [{ items: {}, excluded: [] }],
    left: [reply({ gating: ['m-left'] })],
    other: [reply({ gating: ['m-other'] })],
    away: [reply()],
    live: [reply()],
    liveOld: [{ items: {}, excluded: [] }],
    plain: [reply({ gating: ['m-plain'] })],
  });
  const { gets, posts } = state;
  const card = page.locator('#chat .jx-evalCard');
  const likeBtn = page.locator('#chat').getByRole('button', { name: '有帮助', exact: true });
  const dislikeBtn = page.locator('#chat').getByRole('button', { name: '没有帮助' });
  const open = async (id) => {
    await page.evaluate((chat) => window.openChat(chat), id);
    await page.locator(`#chat[data-chat="${id}"]`).waitFor();
  };

  // ── 1. gating → needed ──
  await page.waitForFunction(() => window.evalState().gating.includes('m-gate'));
  assert.equal(await card.count(), 0, 'no card while the gate decides');
  assert.equal(await likeBtn.count(), 0, 'no quick rating for an evaluation user while gating');
  await card.waitFor({ timeout: 6000 });
  assert.equal(gets.gate, 3, 'polled until the decision landed');
  await page.waitForTimeout(2000);
  assert.equal(gets.gate, 3, 'polling stops once nothing is gating');
  await page.screenshot({ path: resolve(output, 'gate-needed.png'), fullPage: true });

  // ── 2. not_needed → quick rating only, the rating sticks, and the latest snapshot rules ──
  await open('skip');
  await likeBtn.waitFor();
  assert.equal(await card.count(), 0, 'no evaluation card for a not_needed answer');
  assert.equal(await dislikeBtn.count(), 1);
  assert.equal(await likeBtn.getAttribute('aria-pressed'), 'false');
  await page.locator('#chat .jx-msgRow').last().hover();
  await likeBtn.dispatchEvent('click');
  await page.waitForFunction(() => window.evalState().records['m-skip']?.verdict === 'adopt');
  assert.deepEqual(posts.map((p) => [p.messageId, p.rating]), [['m-skip', 'adopt']]);
  assert.equal(await likeBtn.getAttribute('aria-pressed'), 'true');
  state.replies.skip = [reply({ not_needed: ['m-skip'], items: { 'm-skip': { verdict: 'adopt', reasons: [], comment: '' } } })];
  await page.evaluate(() => window.reloadChat('skip'));
  assert.equal(await likeBtn.getAttribute('aria-pressed'), 'true', 'stored verdict shown after re-reading');
  assert.equal(await card.count(), 0);
  await page.screenshot({ path: resolve(output, 'not-needed.png'), fullPage: true });
  // The gate is switched off: the next snapshot no longer lists the answer → the card is back.
  state.replies.skip = [reply({ gate_enabled: false, items: { 'm-skip': { verdict: 'adopt', reasons: [], comment: '' } } })];
  await page.evaluate(() => window.reloadChat('skip'));
  await card.waitFor({ timeout: 2000 });
  assert.ok(!(await page.evaluate(() => window.evalState())).notNeeded.includes('m-skip'), 'not_needed replaced per chat');
  assert.equal(await likeBtn.count(), 0);

  // ── 3. a detailed evaluation keeps its card ──
  await open('revised');
  await card.waitFor({ timeout: 2000 });
  assert.equal(await likeBtn.count(), 0, 'quick rating would overwrite the reasons');

  // ── 4. old server without the new fields → unchanged ──
  await open('old');
  await card.waitFor({ timeout: 2000 });
  assert.equal(await likeBtn.count(), 0, 'evaluation users keep the card, not the quick rating');
  await page.waitForTimeout(2000);
  assert.equal(gets.old, 1, 'no polling against an old server');

  // ── 5a. an answer that finished while the user was elsewhere is read on return ──
  await open('away');
  await card.waitFor();
  const awayReads = gets.away;
  await open('old');
  state.replies.away = [reply({ not_needed: ['m-away'] })];
  await open('away');
  await likeBtn.waitFor({ timeout: 2000 });
  assert.equal(gets.away, awayReads + 1, 're-read on return');
  assert.equal(await card.count(), 0);

  // ── 5b. leaving a chat stops its polling; so does an account reset ──
  await open('left');
  await page.waitForFunction(() => window.evalState().gating.includes('m-left'));
  await page.waitForTimeout(1700);
  const leftReads = gets.left;
  assert.ok(leftReads >= 2, 'polls while gating');
  await open('other');
  await page.waitForFunction(() => window.evalState().gating.includes('m-other'));
  assert.ok(!(await page.evaluate(() => window.evalState())).gating.includes('m-left'), 'left chat no longer gating');
  await page.evaluate(() => window.resetEvaluations());
  const otherReads = gets.other;
  await page.waitForTimeout(3500);
  assert.equal(gets.left, leftReads, 'no more reads for the chat that was left');
  assert.equal(gets.other, otherReads, 'reset stops polling');

  // ── 6. gate on: no card in the first frame after the answer finishes ──
  await open('live');
  await card.waitFor(); // the previous answer: gate decided "needed"
  state.replies.live = [reply({ gating: ['m-live-1'] }), reply({ not_needed: ['m-live-1'] })];
  const liveStart = gets.live;
  await page.evaluate(() => { window.streamAnswer('live', 1); });
  await page.locator('#chat .jx-msgRow[data-uid="live-a1"]').waitFor({ state: 'attached' });
  const firstFrameCards = await page.evaluate(() => window.streaming.finish());
  assert.equal(firstFrameCards, 1, 'only the older answer has a card in the first frame');
  assert.ok((await page.evaluate(() => window.evalState())).gating.includes('m-live-1'), 'held locally until the gate answers');
  await page.waitForFunction(() => window.evalState().notNeeded.includes('m-live-1'), null, { timeout: 6000 });
  assert.ok(gets.live > liveStart, 're-read after the answer finished');
  assert.equal(await page.locator('#chat .jx-msgRow[data-uid="live-a1"] .jx-evalCard').count(), 0);
  // …gate state unknown (old server): the card shows at once, as before.
  await page.evaluate(() => window.resetEvaluations()); // forgets gate_enabled
  await open('liveOld');
  await card.waitFor();
  await page.evaluate(() => { window.streamAnswer('liveOld', 1); });
  await page.locator('#chat .jx-msgRow[data-uid="liveOld-a1"]').waitFor({ state: 'attached' });
  const oldFrameCards = await page.evaluate(() => window.streaming.finish());
  assert.equal(oldFrameCards, 2, 'card shows right away when the gate is not known to be on');

  // ── 7. users without the evaluation permission do not poll ──
  await page.evaluate(() => window.setEvaluationPermission(false));
  await open('plain');
  await likeBtn.waitFor();
  await page.waitForTimeout(3500);
  assert.equal(gets.plain, 1, 'no gating poll without the evaluation card');
  assert.deepEqual((await page.evaluate(() => window.evalState())).gating, []);
  assert.deepEqual(state.errors, []);
  await page.close();

  // ── 8. fake clock: the server governs; the 75 s local cap is per answer and only for failing reads ──
  const fake = await openPage(browser, { cap: [reply({ gating: ['m-cap'] })] }, { clock: true, startChat: 'cap' });
  const fp = fake.page;
  const fcard = fp.locator('#chat .jx-evalCard');
  const tick = async (ms) => {
    for (let left = ms; left > 0; left -= 500) {
      await fp.clock.runFor(Math.min(500, left));
      await fp.waitForTimeout(15); // let mocked responses arrive (real time)
    }
  };
  await fp.waitForFunction(() => window.evalState().gating.includes('m-cap'));
  await tick(30_000);
  assert.equal(await fcard.count(), 0, 'still undecided per the server after 30 s: no card');
  assert.ok(fake.state.gets.cap >= 15, `kept polling while the server lists it (${fake.state.gets.cap} reads)`);
  // A second answer starts gating 30 s later; then every read fails.
  fake.state.replies.cap = [reply({ gating: ['m-cap', 'm-cap-1'] })];
  await fp.evaluate(() => { window.streamAnswer('cap', 1); });
  await fp.locator('#chat .jx-msgRow[data-uid="cap-a1"]').waitFor({ state: 'attached' });
  await fp.evaluate(() => { void window.streaming.finish(); });
  await tick(2000);
  assert.deepEqual((await fp.evaluate(() => window.evalState())).gating.sort(), ['m-cap', 'm-cap-1']);
  fake.state.failing.add('cap');
  await tick(45_000); // t ≈ 77 s: the first answer's cap ran out, the second's (≈ 107 s) not yet
  let gating = (await fp.evaluate(() => window.evalState())).gating;
  assert.deepEqual(gating, ['m-cap-1'], 'first answer gave up on its own deadline');
  assert.equal(await fp.locator('#chat .jx-msgRow[data-uid="cap-a"] .jx-evalCard').count(), 1);
  assert.equal(await fp.locator('#chat .jx-msgRow[data-uid="cap-a1"] .jx-evalCard').count(), 0);
  await tick(32_000);
  gating = (await fp.evaluate(() => window.evalState())).gating;
  assert.deepEqual(gating, [], 'second answer gave up too');
  assert.equal(await fcard.count(), 2);
  const capReads = fake.state.gets.cap;
  await tick(5000);
  assert.equal(fake.state.gets.cap, capReads, 'no polling after giving up');
  assert.deepEqual(fake.state.errors, []);
  await fp.screenshot({ path: resolve(output, 'local-cap.png'), fullPage: true });

  console.log('Browser: evaluation gate (gating poll, not_needed + snapshot replace, detailed record keeps card, old server, '
    + 're-read on return, stop on switch/reset, no first-frame flash, no poll without permission, per-answer 75 s cap): OK');
} finally {
  await browser.close();
}
