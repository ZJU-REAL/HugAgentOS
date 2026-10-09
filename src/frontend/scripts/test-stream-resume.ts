import assert from 'node:assert/strict';
import { streamResumeSeed } from '../src/utils/streamResume';
const partial = { uid: 'answer', messageId: 'answer', role: 'assistant' as const, ts: 1,
  content: '<think>partial', inFlight: { eventOffset: 12 } };
assert.deepEqual(streamResumeSeed(partial), {
  uid: 'answer', messageId: 'answer', role: 'assistant', ts: 1, content: '',
});
assert.equal(streamResumeSeed(undefined), undefined);
console.log('Shared stream resume checks passed');
