import assert from 'node:assert/strict';
import { streamResumeSeed } from '../src/utils/streamResume';

{
  // Scheduler and main chat use this same resume decision. An untagged
  // reasoning prefix has not crossed the model's closing </think> yet.
  const partial = {
    uid: 'scheduled-reasoning', role: 'assistant' as const, ts: 1,
    content: 'Reasoning before a missing opening tag',
    inFlight: { runId: 'running-task', state: 'streaming', eventOffset: 12 },
  };
  assert.equal(streamResumeSeed(partial, true), undefined);
  assert.equal(streamResumeSeed(partial, false), partial);
  assert.equal(streamResumeSeed({ ...partial, content: '<think>partial' }, false), undefined);
  assert.equal(streamResumeSeed(undefined, true), undefined);
}

console.log('Shared stream resume checks passed');
