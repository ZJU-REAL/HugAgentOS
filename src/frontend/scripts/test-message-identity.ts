/**
 * 消息身份自测。
 *
 * 钉住的事实：后端接纳一轮时，把这一轮的提问行与助手行用**同一个** `created_at`
 * 建出来（chat_sequencer 取一次 now 给两条用）。前端一旦拿时间戳当消息身份，同一轮
 * 的两条就共用一个 React key —— 切换会话时 React 认错节点、把上一个会话的提问留在
 * 页面顶部。所以身份必须来自 message_id / 本地新发的 uid，绝不能来自时间。
 */
import assert from 'node:assert/strict';

import { newMessageUid } from '../src/utils/messageIdentity';
import { mergeHistoryPage } from '../src/utils/historyMerge';
import type { ChatItem, ChatMessage } from '../src/types';

// useChatInit / storage 都是浏览器里跑的，先把 localStorage/window 垫出来再动态 import。
const bag = new Map<string, string>();
const fakeStorage = {
  getItem: (k: string) => (bag.has(k) ? bag.get(k)! : null),
  setItem: (k: string, v: string) => { bag.set(k, v); },
  removeItem: (k: string) => { bag.delete(k); },
};
const g = globalThis as unknown as Record<string, unknown>;
g.localStorage = fakeStorage;
g.window = { localStorage: fakeStorage, addEventListener() {}, removeEventListener() {} };
g.document = {
  documentElement: {},
  addEventListener() {},
  removeEventListener() {},
};

const { parseHistoryMessage } = await import('../src/hooks/useChatInit');
const { loadChatStore } = await import('../src/storage');

const SAME_INSTANT = '2026-09-08T15:20:30.123456+00:00';
const LATER = '2026-09-08T15:21:00.000000+00:00';

// ── 同一时刻建出来的一轮问答：时间相同，身份必须不同 ──
{
  const question = parseHistoryMessage({
    message_id: 'msg_q', role: 'user', content: '你好', created_at: SAME_INSTANT,
  });
  const answer = parseHistoryMessage({
    message_id: 'msg_a', role: 'assistant', content: '你好，有什么可以帮你', created_at: SAME_INSTANT,
  });

  assert.equal(question.ts, answer.ts, '前提：后端把同一轮的两条建在同一时刻');
  assert.notEqual(question.uid, answer.uid, '同一轮的提问与回答不能共用身份');
  assert.equal(question.uid, 'msg_q');
  assert.equal(answer.uid, 'msg_a');
}

// ── 整段历史渲染出来的 key 必须两两不同 ──
{
  const history = [
    { message_id: 'msg_1', role: 'user', content: 'Q1', created_at: SAME_INSTANT },
    { message_id: 'msg_2', role: 'assistant', content: 'A1', created_at: SAME_INSTANT },
    { message_id: 'msg_3', role: 'user', content: 'Q2', created_at: LATER },
    { message_id: 'msg_4', role: 'assistant', content: 'A2', created_at: LATER },
  ].map(parseHistoryMessage);

  assert.equal(new Set(history.map((m) => m.uid)).size, history.length, '渲染 key 不允许重复');
  assert.equal(new Set(history.map((m) => m.ts)).size, 2, '时间戳本来就会重复——正是不能拿它当 key 的原因');
}

// ── 本地还没落库的消息也必须各拿各的身份 ──
{
  const uids = Array.from({ length: 500 }, () => newMessageUid());
  assert.equal(new Set(uids).size, uids.length, '本地身份不允许撞号');
}

// ── 历史并页后，列表里的身份仍然唯一 ──
{
  const local: ChatMessage[] = [
    parseHistoryMessage({ message_id: 'msg_1', role: 'user', content: 'Q1', created_at: SAME_INSTANT }),
    parseHistoryMessage({ message_id: 'msg_2', role: 'assistant', content: 'A1', created_at: SAME_INSTANT }),
    // 本轮刚发出去、还没落库的两条
    { role: 'user', uid: newMessageUid(), content: 'Q2', ts: Date.now() },
    { role: 'assistant', uid: newMessageUid(), content: '', ts: Date.now(), isStreaming: true },
  ];
  const page: ChatMessage[] = [
    parseHistoryMessage({ message_id: 'msg_1', role: 'user', content: 'Q1', created_at: SAME_INSTANT }),
    parseHistoryMessage({ message_id: 'msg_2', role: 'assistant', content: 'A1', created_at: SAME_INSTANT }),
  ];
  const merged = mergeHistoryPage(local, page);
  assert.equal(new Set(merged.map((m) => m.uid)).size, merged.length, '并页后 key 仍不允许重复');
}

// ── 老版本缓存里的消息没有 uid，读盘时必须补齐 ──
{
  const legacy = {
    chats: {
      c1: {
        id: 'c1', title: '旧对话', createdAt: 0, updatedAt: 0,
        favorite: false, pinned: false, businessTopic: '综合咨询',
        messages: [
          { role: 'user', content: 'Q', ts: 1_700_000_000_000, messageId: 'msg_old_q' },
          { role: 'assistant', content: 'A', ts: 1_700_000_000_000, messageId: 'msg_old_a' },
          // 刷新前还没落库的那条：没有 message_id，也得拿到身份
          { role: 'user', content: '未落库', ts: 1_700_000_000_000 },
        ],
      } as unknown as ChatItem,
    },
    order: ['c1'],
  };
  bag.set('hugagent_ui_chat_history_v2:u1', JSON.stringify(legacy));

  const restored = loadChatStore('u1');
  const messages = restored.chats.c1.messages;
  assert.equal(messages.length, 3);
  assert.ok(messages.every((m) => !!m.uid), '老缓存里的每条消息都要补上身份');
  assert.equal(new Set(messages.map((m) => m.uid)).size, 3, '补齐后的身份仍不允许重复');
  // 已落库的沿用后端主键，这样刷新后与重新拉取的历史对得上、气泡不会重挂。
  assert.equal(messages[0].uid, 'msg_old_q');
  assert.equal(messages[1].uid, 'msg_old_a');
}

console.log('message identity tests passed');
