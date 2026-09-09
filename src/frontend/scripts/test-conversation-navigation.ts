import assert from 'node:assert/strict';
import { buildConversationTurns } from '../src/utils/conversationNavigation';
import type { ChatMessage } from '../src/types';

const messages: ChatMessage[] = [
  { role: 'assistant', content: 'Older partial reply', ts: 1 },
  { role: 'user', content: 'First question', ts: 2 },
  { role: 'assistant', content: 'First answer', ts: 3 },
  { role: 'user', content: '', attachments: [{ name: 'report.pdf' }], ts: 4 },
  { role: 'assistant', content: '', segments: [
    { type: 'text', content: 'Visible answer' },
  ], ts: 5 },
];
assert.deepEqual(buildConversationTurns(messages), [
  { ts: 2, title: 'First question', preview: 'First answer' },
  { ts: 4, title: 'report.pdf', preview: 'Visible answer' },
]);
assert.deepEqual(buildConversationTurns([]), []);
assert.deepEqual(buildConversationTurns([
  { role: 'user', content: 'Pending question', ts: 10 },
  { role: 'assistant', content: '<think>Private reasoning', ts: 11, isStreaming: true },
]), [{ ts: 10, title: 'Pending question', preview: '' }]);
assert.deepEqual(buildConversationTurns([
  { role: 'user', content: 'Pending question', ts: 10 },
  { role: 'assistant', content: '<think>Private reasoning</think>**Visible** answer', ts: 11 },
]), [{ ts: 10, title: 'Pending question', preview: 'Visible answer' }]);
const prepended = buildConversationTurns([
  { role: 'user', content: 'Earlier question', ts: 0 }, ...messages,
]);
assert.equal(prepended[1].ts, 2);
assert.equal(prepended[2].ts, 4);
console.log('conversation navigation checks passed');
