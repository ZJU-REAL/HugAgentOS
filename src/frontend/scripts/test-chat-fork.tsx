/* eslint-disable react-refresh/only-export-components -- SSR fixture captures the actual hook action without exporting a production component. */
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import React from 'react';
import { renderToString } from 'react-dom/server';
import { message } from 'antd';

const memory = new Map<string, string>();
const storage = { getItem: (key: string) => memory.get(key) ?? null, setItem: (key: string, value: string) => { memory.set(key, value); }, removeItem: (key: string) => { memory.delete(key); } };
if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', { value: webcrypto });
Object.assign(globalThis, { localStorage: storage, window: { location: { pathname: '/', origin: 'http://localhost' }, localStorage: storage, sessionStorage: storage, setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {} } });
Object.assign(globalThis, { document: { documentElement: {}, addEventListener() {}, removeEventListener() {} } });
for (const name of ['info', 'success', 'warning', 'error'] as const) message[name] = (() => () => {}) as typeof message[typeof name];
const { useChatStore } = await import('../src/stores/chatStore');
const { setHybridDual, isLocalChat } = await import('../src/api');
const { useChatFork, forkConversation } = await import('../src/hooks/useChatFork');
const { ForkChatButton } = await import('../src/components/chat/ForkChatButton');
const { classifyForkCommand } = await import('../src/utils/chatForkCommands');
setHybridDual(true);
let actions!: ReturnType<typeof useChatFork>;
function Composer() { actions = useChatFork(useChatStore.getState().currentChatId); return null; }
function source(id: string, local = false) {
  window.location.pathname = '/';
  useChatStore.setState({ currentUserId: 'owner', currentChatId: id, input: '/fork',
    backendSessionIds: new Set([id]), loadedMsgIds: new Set([id]),
    store: { order: [id], chats: { [id]: { id, title: 'Original', createdAt: 1, updatedAt: 2, messages: [], runTarget: local ? 'local' : 'cloud' } } },
  });
  renderToString(<Composer />);
}
const requests: { url: string; method?: string; headers: Headers; body?: Record<string, unknown> }[] = [];
let serial = 0;
function session() {
  return { chat_id: `fork-${++serial}`, user_id: 'owner', title: 'Original · 分支', project_id: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), message_count: 2,
    metadata: { fork: { source_chat_id: useChatStore.getState().currentChatId, source_message_id: 'reply-2', source_chat_seq: 2, source_title: 'Original', created_at: new Date().toISOString(), message_count: 2 } } };
}
function successfulFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  requests.push({ url, method: init?.method, headers: new Headers(init?.headers), ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}) });
  if (url.endsWith('/fork')) return Promise.resolve(Response.json({ code: 200, data: session() }));
  assert.match(url, /messages\?page=1&page_size=30&order=desc$/);
  return Promise.resolve(Response.json({ code: 200, data: { items: [
    { message_id: 'copied-user', role: 'user', content: 'before', created_at: new Date().toISOString(), metadata: { forked_history: true } },
    { message_id: 'copied-assistant', role: 'assistant', content: 'answer', created_at: new Date().toISOString(), metadata: { forked_history: true } },
  ], pagination: { has_next: true } } }));
}
globalThis.fetch = successfulFetch;
for (const local of [false, true]) {
  source(local ? 'local-source' : 'cloud-source', local);
  const count = requests.length;
  const first = actions.forkChat('reply-2', { clearInput: '/fork' });
  const duplicate = actions.forkChat('reply-2');
  assert.equal(await first, true);
  assert.equal(await duplicate, true);
  const calls = requests.slice(count);
  assert.equal(calls.length, 2, 'two simultaneous entry points create and load one branch');
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].body?.through_message_id, 'reply-2');
  assert.match(String(calls[0].body?.request_id), /^[0-9a-f-]{36}$/);
  for (const call of calls) assert.equal(call.headers.get('x-hugagent-target'), local ? 'local' : null);
  const state = useChatStore.getState();
  assert.equal(state.currentChatId, `fork-${serial}`);
  assert.equal(state.input, '');
  assert.equal(state.store.chats[state.currentChatId].messages.length, 2);
  assert.equal(state.messagePaging[state.currentChatId].hasOlder, true);
  assert.equal(state.store.chats[state.currentChatId].runTarget, local ? 'local' : 'cloud');
  assert.equal(isLocalChat(state.currentChatId), local);
}
source('button-source');
useChatStore.getState().setInput('Keep this unrelated draft');
assert.match(renderToString(<ForkChatButton chatId="button-source" messageId="reply-2" />), /aria-label="创建聊天分支"/);
await forkConversation('button-source', 'reply-2');
assert.equal(useChatStore.getState().input, 'Keep this unrelated draft');

source('uncertain-retry');
const retryRequests: string[] = [];
let failed = false;
globalThis.fetch = async (input, init) => {
  if (String(input).endsWith('/fork')) {
    retryRequests.push(JSON.parse(String(init?.body)).request_id);
    if (!failed) { failed = true; throw new Error('connection lost'); }
  }
  return successfulFetch(input, init);
};
assert.equal(await actions.forkChat(), false);
assert.equal(useChatStore.getState().input, '/fork');
assert.equal(await actions.forkChat(), true);
assert.equal(retryRequests[0], retryRequests[1], 'uncertain network retry reuses the operation identity');

source('discovered-before-retry');
const discoveredSession = session();
const discoveredId = discoveredSession.chat_id;
const recoveredIds: string[] = [];
let lostResponse = true;
let duplicateHistoryLoads = 0;
globalThis.fetch = async (input, init) => {
  if (String(input).endsWith('/fork')) {
    recoveredIds.push(JSON.parse(String(init?.body)).request_id);
    if (lostResponse) { lostResponse = false; throw new Error('server committed; response lost'); }
    return Response.json({ code: 200, data: discoveredSession });
  }
  duplicateHistoryLoads += 1;
  return successfulFetch(input, init);
};
assert.equal(await actions.forkChat(), false);
const discoveredMessages = [{ role: 'assistant' as const, uid: 'continued-reply', messageId: 'continued-reply', content: 'Work after sidebar discovery', ts: Date.now() }];
const discovered = useChatStore.getState();
discovered.updateStore((store) => ({ ...store, order: [discoveredId, ...store.order], chats: { ...store.chats,
  [discoveredId]: { id: discoveredId, title: 'Renamed branch', titleManuallySet: true, createdAt: 1, updatedAt: 2,
    messages: discoveredMessages, planModeActive: true, batchModeActive: true, workflowModeActive: true,
    modeSlug: 'custom-mode', thinkingEffort: 'max', pinned: true, favorite: true, pendingQuote: { text: 'Keep branch draft' },
  },
} }));
discovered.addBackendSessionId(discoveredId);
discovered.addLoadedMsgId(discoveredId);
discovered.setMessagePaging(discoveredId, { nextPage: 5, hasOlder: false, loading: true });
assert.equal(await actions.forkChat(), true);
assert.equal(recoveredIds[0], recoveredIds[1]);
const recovered = useChatStore.getState().store.chats[discoveredId];
assert.equal(recovered.messages, discoveredMessages, 'idempotent recovery must preserve the independently continued branch');
assert.equal(recovered.title, 'Renamed branch');
assert.equal(recovered.planModeActive, true);
assert.equal(recovered.batchModeActive, true);
assert.equal(recovered.workflowModeActive, true);
assert.equal(recovered.modeSlug, 'custom-mode');
assert.equal(recovered.thinkingEffort, 'max');
assert.equal(recovered.pinned, true);
assert.equal(recovered.favorite, true);
assert.equal(recovered.pendingQuote?.text, 'Keep branch draft');
assert.deepEqual(useChatStore.getState().messagePaging[discoveredId], { nextPage: 5, hasOlder: false, loading: true });
assert.equal(useChatStore.getState().loadedMsgIds.has(discoveredId), true);
assert.equal(duplicateHistoryLoads, 0, 'an already loaded recovered branch needs no initial load');

source('history-failure');
globalThis.fetch = (input, init) => String(input).includes('/messages?')
  ? Promise.resolve(new Response('unavailable', { status: 503 })) : successfulFetch(input, init);
assert.equal(await actions.forkChat(), true);
assert.equal(useChatStore.getState().currentChatId, `fork-${serial}`);
assert.equal(useChatStore.getState().backendSessionIds.has(`fork-${serial}`), true);
assert.equal(useChatStore.getState().loadedMsgIds.has(`fork-${serial}`), false);

for (const scenario of ['account', 'navigation', 'panel', 'draft'] as const) {
  source(`race-${scenario}`);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  globalThis.fetch = async (input, init) => {
    if (String(input).endsWith('/fork')) { await gate; }
    return successfulFetch(input, init);
  };
  const task = actions.forkChat(undefined, { clearInput: '/fork' });
  await Promise.resolve();
  if (scenario === 'account') useChatStore.setState({ currentUserId: 'other', store: { order: [], chats: {} } });
  if (scenario === 'navigation') useChatStore.setState({ currentChatId: 'other-conversation' });
  if (scenario === 'panel') window.location.pathname = '/settings';
  if (scenario === 'draft') useChatStore.getState().setInput('Edited during request');
  release();
  await task;
  if (scenario === 'account') assert.equal(Object.keys(useChatStore.getState().store.chats).length, 0);
  if (scenario === 'navigation') assert.equal(useChatStore.getState().currentChatId, 'other-conversation');
  if (scenario === 'panel') assert.equal(useChatStore.getState().currentChatId, 'race-panel');
  if (scenario === 'draft') assert.equal(useChatStore.getState().input, 'Edited during request');
}
source('late-history');
let releaseHistory!: () => void;
let historyStarted!: () => void;
const historyGate = new Promise<void>((resolve) => { releaseHistory = resolve; });
const started = new Promise<void>((resolve) => { historyStarted = resolve; });
globalThis.fetch = async (input, init) => {
  if (String(input).includes('/messages?')) { historyStarted(); await historyGate; }
  return successfulFetch(input, init);
};
const lateTask = actions.forkChat();
await started;
const newId = `fork-${serial}`;
const existing = useChatStore.getState();
existing.updateStore((store) => ({ ...store, chats: { ...store.chats, [newId]: {
  ...store.chats[newId], messages: [{ role: 'user', uid: 'new-follow-up', content: 'Already continuing', ts: Date.now() + 1000 }],
} } }));
existing.setMessagePaging(newId, { nextPage: 4, hasOlder: false, loading: false });
existing.addSendingChatId(newId);
releaseHistory();
await lateTask;
assert.ok(useChatStore.getState().store.chats[newId].messages.some((item) => item.uid === 'new-follow-up'), 'late initial history must not overwrite new turns');
assert.equal(useChatStore.getState().messagePaging[newId].nextPage, 4);
assert.equal(useChatStore.getState().messagePaging[newId].hasOlder, false);
existing.removeSendingChatId(newId);

source('replaced-history');
let releaseReplacement!: () => void;
let replacementStarted!: () => void;
const replacementGate = new Promise<void>((resolve) => { releaseReplacement = resolve; });
const replacementReady = new Promise<void>((resolve) => { replacementStarted = resolve; });
globalThis.fetch = async (input, init) => {
  if (String(input).includes('/messages?')) { replacementStarted(); await replacementGate; }
  return successfulFetch(input, init);
};
const replacementTask = actions.forkChat();
await replacementReady;
const replacementId = `fork-${serial}`;
const replacements = [{ role: 'user' as const, uid: 'edited-first', messageId: 'persisted-edited',
  content: 'Changed first question', ts: Date.now() + 1000 }];
const replacementState = useChatStore.getState();
replacementState.updateStore((store) => ({ ...store, chats: { ...store.chats, [replacementId]: {
  ...store.chats[replacementId], messages: replacements,
} } }));
replacementState.addLoadedMsgId(replacementId);
releaseReplacement();
await replacementTask;
assert.equal(useChatStore.getState().store.chats[replacementId].messages, replacements,
  'a late redundant initial snapshot must not resurrect edited-away history');

assert.equal(classifyForkCommand(' /fork\n'), 'fork');
assert.equal(classifyForkCommand('/fork argument'), 'invalid');
assert.equal(classifyForkCommand('Explain /fork'), null);
assert.equal(classifyForkCommand('/forking'), null);
console.log('PASS: shared fork entry points, explicit boundary, local/cloud routing, history loading, idempotent retry, draft preservation, account/navigation races');
