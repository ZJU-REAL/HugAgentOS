import assert from 'node:assert/strict';
import { acknowledgeUserMessage } from '../src/utils/messageIdentity';
import { mergeHistoryPage } from '../src/utils/historyMerge';
import type { ChatMessage } from '../src/types';
const local: ChatMessage = { uid: 'optimistic-1', role: 'user', content: 'same text', ts: 1 };
const server: ChatMessage = { ...local, uid: 'server-1', messageId: 'server-1', ts: 2 };
const answer: ChatMessage = { uid: 'answer-1', role: 'assistant', content: '', ts: 3, messageId: 'answer-1', isStreaming: true };
// Admission binds the exact optimistic message, preserving its render identity.
const acknowledged = acknowledgeUserMessage([local, answer], local.uid, server.messageId!);
assert.equal(acknowledged[0].uid, local.uid);
assert.equal(acknowledged[0].messageId, server.messageId);
assert.equal(acknowledged[1], answer);
assert.equal(local.messageId, undefined, 'must not mutate the previous store snapshot');
const refreshed = mergeHistoryPage(acknowledged, [server, answer], { localIsWriter: true });
assert.equal(refreshed.filter(m => m.role === 'user').length, 1);
assert.equal(refreshed[0].uid, local.uid, 'history must preserve the acknowledged render identity');
assert.equal(refreshed[1], answer, 'history must preserve the live answer');
// A history response can race admission acknowledgment; collapse only that exact ID.
const raced = acknowledgeUserMessage([local, server, answer], local.uid, server.messageId!);
assert.equal(raced.length, 2);
assert.equal(raced[0].uid, local.uid);
// Same text represents distinct turns; never deduplicate by content or timestamp.
const second: ChatMessage = { ...local, uid: 'optimistic-2', ts: 4 };
const two = acknowledgeUserMessage([...acknowledged, second], second.uid, 'server-2');
assert.equal(two.filter(m => m.role === 'user').length, 2);
assert.deepEqual(two.filter(m => m.role === 'user').map(m => m.messageId), ['server-1', 'server-2']);
assert.deepEqual(acknowledgeUserMessage(raced, local.uid, server.messageId!), raced);
const otherAccount = [server];
assert.equal(acknowledgeUserMessage(otherAccount, local.uid, server.messageId!), otherAccount);
assert.equal(acknowledgeUserMessage([local], local.uid, '')[0], local);
console.log('PASS admission identity, raced history, repeated text, immutable state and account switch');
