import assert from 'node:assert/strict';

import type { ChatMessage } from '../src/types';
import type { ChatState } from '../src/stores/chatState';
import { createChatSelectionActions } from '../src/stores/chatSelectionActions';
import { pickEvictableChats, RETAINED_LOADED_CHATS, touchRecentChat, trimEvictedMessages } from '../src/utils/chatMemoryEviction';

const msg = (i: number, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  role: 'assistant', content: `m${i}`, uid: `m${i}`, ts: i, messageId: `m${i}`, ...extra,
});

// 最近访问顺序：重复访问挪到队尾，不重复记。
{
  let recent: string[] = [];
  for (const id of ['a', 'b', 'c', 'a']) recent = touchRecentChat(recent, id);
  assert.deepEqual(recent, ['b', 'c', 'a']);
}

// 只淘汰超出保留数的最旧会话，且受保护的（正在输出等）不淘汰。
{
  const recent = ['a', 'b', 'c', 'd', 'e'];
  assert.deepEqual(pickEvictableChats(recent, () => true, 3), ['a', 'b']);
  assert.deepEqual(pickEvictableChats(recent, (id) => id !== 'a', 3), ['b']);
  assert.deepEqual(pickEvictableChats(recent, () => true, 5), []);
}

// 裁成最近一页；按需取回的工具全文丢掉并恢复成"截断、可重取"，历史自带的梗概原样保留。
{
  const messages = Array.from({ length: 40 }, (_, i) => msg(i));
  messages[39] = msg(39, { toolCalls: [
    { id: 't', name: 'x', output: 'big', outputLoaded: true } as never,
    { id: 's', name: 'y', output: 'summary', outputTruncated: true } as never,
  ] });
  const trimmed = trimEvictedMessages(messages, 30)!;
  assert.equal(trimmed.length, 30);
  assert.equal(trimmed[0].messageId, 'm10');
  const [fetched, summary] = trimmed[29].toolCalls!;
  assert.equal(fetched.output, undefined);
  assert.equal(fetched.outputLoaded, undefined);
  assert.equal(fetched.outputTruncated, true, 'the card must stay expandable and refetch');
  assert.equal(summary.output, 'summary');
  assert.equal(messages[39].toolCalls![0].output, 'big', 'source array is not mutated');
  assert.equal(trimEvictedMessages([msg(1), msg(2)], 30), null);
}

// 一段没有消息 id 的本地消息里，取回的全文没法再取：原样保留。
{
  const local = { ...msg(1, { toolCalls: [{ id: 't', name: 'x', output: 'big', outputLoaded: true } as never] }), messageId: undefined };
  assert.equal(trimEvictedMessages([local], 30), null);
}

// Store 层：最近 RETAINED_LOADED_CHATS 个之外的会话被淘汰；正在写、正在续拉的会话不淘汰。
{
  let state = {
    store: { chats: {}, order: [] }, storeRef: { chats: {}, order: [] }, currentChatId: '',
    loadedMsgIds: new Set<string>(), recentLoadedChats: [], messagePaging: {},
    backendSessionIds: new Set<string>(), sendingChatIds: new Set<string>(), remoteRunningChatIds: new Set<string>(),
    activeRuns: {}, queuedMessages: {},
  } as unknown as ChatState;
  const set = (patch: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>)) => {
    state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
  };
  const actions = createChatSelectionActions({
    set, get: () => state, initializeDraftRunTarget: () => {}, syncChatUrl: () => {}, publishCurrentChatUrl: () => {},
  } as never);
  const ids = Array.from({ length: RETAINED_LOADED_CHATS + 4 }, (_, i) => `c${i}`);
  for (const id of ids) {
    state.store.chats[id] = { id, title: id, createdAt: 0, updatedAt: 0, messages: Array.from({ length: 40 }, (_, i) => msg(i)) } as never;
    state.backendSessionIds.add(id);
  }
  state = { ...state, sendingChatIds: new Set(['c0']), messagePaging: { c1: { nextPage: 3, hasOlder: true, loading: true } } };
  for (const id of ids) actions.addLoadedMsgId(id);
  assert.ok(state.loadedMsgIds.has('c0'), 'a sending chat is never evicted');
  assert.ok(state.loadedMsgIds.has('c1'), 'a chat loading older pages is never evicted');
  assert.ok(!state.loadedMsgIds.has('c2') && !state.loadedMsgIds.has('c3'));
  assert.equal(state.store.chats.c2.messages.length, 30);
  assert.equal(state.store.chats.c0.messages.length, 40);
  assert.deepEqual(state.recentLoadedChats.slice(-RETAINED_LOADED_CHATS), ids.slice(-RETAINED_LOADED_CHATS));
  actions.clearLoadedMsgIds();
  assert.deepEqual(state.recentLoadedChats, []);
}

console.log('chat memory eviction tests passed');
