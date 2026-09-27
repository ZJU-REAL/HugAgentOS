import assert from 'node:assert/strict';
import React from 'react';
import { renderToString } from 'react-dom/server';

const memory = new Map<string, string>();
const storage = {
  getItem: (key: string) => memory.get(key) ?? null,
  setItem: (key: string, value: string) => { memory.set(key, value); },
  removeItem: (key: string) => { memory.delete(key); },
};
Object.assign(globalThis, {
  localStorage: storage,
  window: {
    location: { pathname: '/', origin: 'http://localhost' },
    localStorage: storage, sessionStorage: storage, setTimeout, clearTimeout,
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    history: { pushState() {}, replaceState() {} },
  },
  document: { documentElement: {}, addEventListener() {}, removeEventListener() {} },
});

const { parseHistoryMessage } = await import('../src/hooks/chatHistoryMessage');
const { reloadChatHistory } = await import('../src/hooks/chatHistoryLoader');
const { sessionToChatItem } = await import('../src/hooks/chatSessionMapping');
const { isForkedHistory } = await import('../src/utils/forkHistory');
const { ToolMessageContext } = await import('../src/components/tool/ToolMessageContext');
const { ToolCallRow } = await import('../src/components/tool/ToolCallRow');
const { MessageBody } = await import('../src/components/chat/MessageBody');
const { useAuthStore, useChatStore } = await import('../src/stores');

const row = {
  message_id: 'copy-message', role: 'assistant', content: 'Historical answer',
  created_at: '2026-09-26T00:00:00Z', usage: null,
  in_flight: { event_offset: 42 }, thinking: [{ content: 'Earlier reasoning' }],
  metadata: {
    forked_history: true, forked_usage: { total_tokens: 123 },
    plan_id: 'source-plan',
    plan_snapshot: { mode: 'preview', title: 'Source plan', steps: [
      { step_order: 1, title: 'Pending task', status: 'running' },
    ] },
  },
  tool_calls: [{ tool_id: 'source-tool', tool_name: 'call_subagent', result: { request_id: 'source-request' }, status: 'success' }],
};
const inherited = parseHistoryMessage(row);
assert.equal(isForkedHistory(inherited), true);
assert.equal(inherited.messageId, 'copy-message');
assert.equal(inherited.isStreaming, undefined);
assert.deepEqual(inherited.usage, { total_tokens: 123 });
assert.deepEqual(inherited.thinking, row.thinking);
assert.equal(inherited.segments?.[0].planData?.planId, undefined);
assert.equal(inherited.segments?.[0].planData?.mode, 'preview');
assert.equal(inherited.segments?.[0].planData?.steps[0].status, 'running');
const normal = parseHistoryMessage({ ...row, metadata: { ...row.metadata, forked_history: false } });
assert.equal(normal.isStreaming, true);
assert.equal(normal.segments?.[0].planData?.planId, 'source-plan');

const inheritedMarkup = renderToString(
  <ToolMessageContext.Provider value={{ chatId: 'branch', messageId: inherited.messageId, messageUid: inherited.uid, readOnly: true }}>
    <ToolCallRow tool={inherited.toolCalls![0]} />
  </ToolMessageContext.Provider>,
);
assert.match(inheritedMarkup, /<details/);
assert.doesNotMatch(inheritedMarkup, /jx-subagentCall|iframe|确认执行|放弃/);
const planMarkup = renderToString(<MessageBody m={inherited} messageIndex={0} currentChatId="branch" send={() => { throw Error('Inherited render must not send'); }} />);
assert.doesNotMatch(planMarkup, /jx-plan-approveBtn|确认后将按步骤执行|anticon-loading|jx-plan-step--active/);
assert.match(planMarkup, /历史计划（只读）/);
assert.match(planMarkup, /分叉时/);

const session = { chat_id: 'branch', title: 'Branch', metadata: { fork: { source_chat_id: 'source' } } };
const first = sessionToChatItem(session);
assert.equal(first.planModeActive, false);
const prior = { ...first, modeSlug: 'custom-model', thinkingEffort: 'high' as const, planModeActive: true };
const reloaded = sessionToChatItem(session, prior);
assert.equal(reloaded.modeSlug, 'custom-model');
assert.equal(reloaded.thinkingEffort, 'high');
assert.equal(reloaded.planModeActive, true, 'later explicit branch mode selection survives refresh');

type AuthUser = NonNullable<ReturnType<typeof useAuthStore.getState>['authUser']>;
useAuthStore.setState({ authUser: { user_id: 'account-a' } as AuthUser });
useChatStore.setState({ currentChatId: 'branch', planMode: false, currentPlanId: null, store: { chats: { branch: first }, order: ['branch'] } });
let release!: () => void;
const blocked = new Promise<void>(resolve => { release = resolve; });
globalThis.fetch = async () => {
  await blocked;
  return Response.json({ data: { items: [row], pagination: { has_next: false } } });
};
const pending = reloadChatHistory('branch');
useAuthStore.setState({ authUser: { user_id: 'account-b' } as AuthUser });
release();
assert.equal(await pending, false, 'late old-account history cannot hydrate the new account');
assert.equal(useChatStore.getState().loadedMsgIds.has('branch'), false);
assert.equal(useChatStore.getState().store.chats.branch.messages.length, 0);

assert.equal(await reloadChatHistory('branch'), true);
assert.equal(useChatStore.getState().planMode, false, 'inherited snapshots never restore source plan mode');
assert.equal(useChatStore.getState().currentPlanId, null);
console.log('PASS: fork history stays readable and readonly; original plan bindings, live controls, accounting and account state remain isolated');
