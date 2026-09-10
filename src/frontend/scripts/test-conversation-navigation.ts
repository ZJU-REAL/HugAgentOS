import assert from 'node:assert/strict';
import { buildConversationTurns } from '../src/utils/conversationNavigation';
import type { ChatMessage } from '../src/types';

const messages: ChatMessage[] = [
  { role: 'assistant', uid: 'm1', content: 'Older partial reply', ts: 1 },
  { role: 'user', uid: 'm2', content: 'First question', ts: 2 },
  { role: 'assistant', uid: 'm3', content: 'First answer', ts: 3 },
  { role: 'user', uid: 'm4', content: '', attachments: [{ name: 'report.pdf' }], ts: 4 },
  { role: 'assistant', uid: 'm5', content: '', segments: [
    { type: 'text', content: 'Visible answer' },
  ], ts: 5 },
];
assert.deepEqual(buildConversationTurns(messages), [
  { uid: 'm2', title: 'First question', preview: 'First answer' },
  { uid: 'm4', title: 'report.pdf', preview: 'Visible answer' },
]);
assert.deepEqual(buildConversationTurns([]), []);
assert.deepEqual(buildConversationTurns([
  { role: 'user', uid: 'm10', content: 'Pending question', ts: 10 },
  { role: 'assistant', uid: 'm11', content: '<think>Private reasoning', ts: 11, isStreaming: true },
]), [{ uid: 'm10', title: 'Pending question', preview: '' }]);
assert.deepEqual(buildConversationTurns([
  { role: 'user', uid: 'm10', content: 'Pending question', ts: 10 },
  { role: 'assistant', uid: 'm11', content: '<think>Private reasoning</think>**Visible** answer', ts: 11 },
]), [{ uid: 'm10', title: 'Pending question', preview: 'Visible answer' }]);
const prepended = buildConversationTurns([
  { role: 'user', uid: 'm0', content: 'Earlier question', ts: 0 }, ...messages,
]);
assert.equal(prepended[1].uid, 'm2');
assert.equal(prepended[2].uid, 'm4');

// 后端把同一轮的提问与回答建在同一时刻，两条 ts 完全相同。导航锚点必须落在身份上，
// 拿时间当锚点时相邻两轮会退化成同一个锚。
const sameInstantTurns = buildConversationTurns([
  { role: 'user', uid: 'msg_a', content: 'Question', ts: 1_700_000_000_000 },
  { role: 'assistant', uid: 'msg_b', content: 'Answer', ts: 1_700_000_000_000 },
  { role: 'user', uid: 'msg_c', content: 'Follow-up', ts: 1_700_000_001_000 },
  { role: 'assistant', uid: 'msg_d', content: 'Second answer', ts: 1_700_000_001_000 },
]);
assert.deepEqual(sameInstantTurns.map((turn) => turn.uid), ['msg_a', 'msg_c']);

console.log('conversation navigation checks passed');
