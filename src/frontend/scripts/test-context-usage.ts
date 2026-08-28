import assert from 'node:assert/strict';

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
  schema_version: 'context-usage.v1',
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
      schema_version: 'context-usage.v1',
      source: 'compaction_estimate',
      exact: false,
      used_tokens: 82,
      prompt_tokens: 82,
      completion_tokens: 0,
      context_window: 1000,
      breakdown: {
        messages: 15,
        tools: 20,
        thinking: 0,
        files: 0,
        system: 47,
        input: 0,
      },
    },
  });
  assert.equal(compaction?.contextUsage?.usedTokens, 82);
  assert.equal(compaction?.contextUsage?.source, 'compaction_estimate');
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

console.log('context usage tests passed');
