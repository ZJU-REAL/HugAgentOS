import assert from 'node:assert/strict';
import { applyContextStateResponse, captureContextState } from '../src/utils/contextStateResponse';
import { createStore } from 'zustand/vanilla';
import { createChatRunActions } from '../src/stores/chatRunActions';
import { createChatInitialState } from '../src/stores/chatInitialState';
import type { ChatState } from '../src/stores/chatState';

import type { ChatMessage } from '../src/types';
import {
  combineContextUsage,
  computeContextBreakdown,
  estimateTokens,
  isCompactionCheckpointForRun,
  parseContextCompactionState,
  parseContextUsageSnapshot,
} from '../src/utils/contextUsage';

{
  const base: ChatMessage = {
    role: 'assistant',
    content: 'answer',
    uid: 'm1',
    ts: 1,
    thinking: [{ content: 'display-only reasoning', timestamp: 1 }],
    toolCalls: [{
      name: 'call_subagent',
      input: { task: 'work' },
      output: 'result',
      subSteps: [
        { kind: 'thinking', text: 'display-only child trace' },
        { kind: 'tool', name: 'inner', input: 'large input', output: 'large output' },
      ],
    }],
  };
  const withoutDisplayOnly: ChatMessage = {
    ...base,
    thinking: undefined,
    toolCalls: base.toolCalls?.map((tool) => ({ ...tool, subSteps: undefined })),
  };

  assert.deepEqual(
    computeContextBreakdown([base]),
    computeContextBreakdown([withoutDisplayOnly]),
  );
  assert.equal(computeContextBreakdown([base]).system, 0);
}

const provider = parseContextUsageSnapshot({
  schema_version: 'context-usage.v2',
  source: 'provider',
  exact: true,
  used_tokens: 150,
  prompt_tokens: 120,
  completion_tokens: 30,
  context_window: 1000,
  model_name: 'demo',
  model_provider_id: 'provider-1',
  model_call_index: 2,
  breakdown: {
    messages: 60,
    tools: 40,
    thinking: 0,
    files: 10,
    system: 40,
    input: 0,
  },
});
assert.ok(provider);

assert.equal(parseContextUsageSnapshot({
  schema_version: 'context-usage.v1',
}), null);

assert.equal(parseContextUsageSnapshot({
  schema_version: 'context-usage.v2',
  source: 'provider',
  exact: true,
  used_tokens: 150,
  prompt_tokens: 119,
  completion_tokens: 30,
  context_window: 1000,
  breakdown: {
    messages: 60,
    tools: 40,
    thinking: 0,
    files: 10,
    system: 40,
    input: 0,
  },
}), null);

{
  const combined = combineContextUsage(provider, {
    draft: 'draft',
    stagedFiles: [],
  });
  assert.equal(combined.total, 150 + estimateTokens('draft'));
  assert.equal(combined.input, estimateTokens('draft'));
  assert.equal(combined.thinking, 0);
}

{
  const compaction = parseContextCompactionState({
    checkpoint_id: 'checkpoint-1',
    checkpoint_created_at: '2026-08-26T00:00:00Z',
    covered_message_count: 8,
    replacement_tokens: 15,
    context_usage: {
      schema_version: 'context-usage.v2',
      source: 'compaction_estimate',
      exact: false,
      used_tokens: 82,
      prompt_tokens: 82,
      completion_tokens: 0,
      context_window: 1000,
      breakdown: {
        messages: 15,
        tools: 0,
        thinking: 0,
        files: 0,
        system: 67,
        input: 0,
      },
    },
  });
  assert.equal(compaction?.contextUsage?.usedTokens, 82);
  assert.equal(compaction?.contextUsage?.source, 'compaction_estimate');
  assert.equal(compaction?.contextUsage?.breakdown.tools, 0);
  assert.equal(compaction?.contextUsage?.breakdown.system, 67);
  assert.equal(isCompactionCheckpointForRun(compaction, '', Date.parse('2026-08-27')), false);
  assert.equal(
    isCompactionCheckpointForRun(
      { ...compaction!, coveredThroughMessageId: 'message-1' },
      '',
      Date.parse('2026-08-27'),
      'message-1',
    ),
    true,
  );
}



// A delayed history request must not roll a live measurement back to an older checkpoint.
{
  const store = createStore<ChatState>((set, get) => ({
    ...createChatInitialState(),
    ...createChatRunActions({ set, get }),
  } as ChatState));
  const newer = { ...provider!, capturedAtMs: 3000 };
  store.getState().setContextUsage('audit', newer);
  store.getState().setContextCompaction('audit', {
    checkpointId: 'older', checkpointTs: 2000, coveredMessageCount: 2,
    replacementTokens: 10, contextUsage: { ...provider!, source: 'compaction_estimate', exact: false, capturedAtMs: 2000 },
  });
  assert.equal(store.getState().contextUsages.audit.capturedAtMs, 3000);
  store.getState().setContextUsage('audit', { ...provider!, capturedAtMs: 1000 });
  assert.equal(store.getState().contextUsages.audit.capturedAtMs, 3000);
}

// SQLite's naive timestamp is UTC, regardless of the browser timezone.
assert.equal(parseContextCompactionState({
  checkpoint_id: 'utc', checkpoint_created_at: '2026-01-01T00:00:00',
  covered_message_count: 1, replacement_tokens: 1,
})?.checkpointTs, 1767225600000);

// Empty history may finish after the live run has already ended.
{
  const store = createStore<ChatState>((set, get) => ({
    ...createChatInitialState(), ...createChatRunActions({ set, get }),
  } as ChatState));
  const request = captureContextState(store.getState(), 'audit');
  store.getState().setContextUsage('audit', { ...provider!, capturedAtMs: 3000 });
  store.getState().setContextCompaction('audit', {
    checkpointId: 'new', checkpointTs: 4000, coveredMessageCount: 2, replacementTokens: 10,
  });
  applyContextStateResponse(store.getState(), 'audit', request, {
    context_usage: null, context_compaction: null,
  });
  assert.equal(store.getState().contextUsages.audit.capturedAtMs, 3000);
  assert.equal(store.getState().contextCompactions.audit.checkpointId, 'new');
}

// Editing invalidates every context request accepted before the truncation.
{
  const store = createStore<ChatState>((set, get) => ({
    ...createChatInitialState(), ...createChatRunActions({ set, get }),
  } as ChatState));
  const request = captureContextState(store.getState(), 'audit');
  store.setState({ contextUsageEpochs: { audit: 1 } });
  assert.equal(applyContextStateResponse(store.getState(), 'audit', request, {
    context_usage: null, context_compaction: {
      checkpoint_id: 'deleted', checkpoint_created_at: '2026-01-01T00:00:00Z',
      covered_message_count: 2, replacement_tokens: 100,
    },
  }), false);
  assert.equal(store.getState().contextCompactions.audit, undefined);
}

// Checkpoint counters are discrete: reject fractions, overflow and invalid values.
for (const count of [-1, 1.5, Number.MAX_SAFE_INTEGER + 1, Infinity]) {
  assert.equal(parseContextCompactionState({
    checkpoint_id: 'invalid', covered_message_count: count, replacement_tokens: 1,
  }), null);
  assert.equal(parseContextCompactionState({
    checkpoint_id: 'invalid', covered_message_count: 1, replacement_tokens: count,
  }), null);
}
console.log('context usage tests passed');
