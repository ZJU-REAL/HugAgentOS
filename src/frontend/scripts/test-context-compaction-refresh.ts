import assert from 'node:assert/strict';
import { COMPACTION_REFRESH_DELAYS_MS, refreshContextAfterCompaction } from '../src/hooks/chatStream/runtime';
import { useChatStore } from '../src/stores';

const chatId = 'poll-audit';
const runStarted = 1000;
const usage = (tokens: number, captured: number) => ({
  schema_version: 'context-usage.v2', source: 'compaction_estimate', exact: false,
  used_tokens: tokens, prompt_tokens: tokens, completion_tokens: 0,
  context_window: 1000, captured_at_ms: captured,
  breakdown: { messages: tokens, system: 0, tools: 0, files: 0, thinking: 0, input: 0 },
});
const checkpoint = (id: string, captured: number) => ({
  checkpoint_id: id, checkpoint_created_at: new Date(captured).toISOString(),
  covered_message_count: 2, replacement_tokens: 10,
});
const progress = (status: string) => [{
  id: 'post', chat_id: chatId, run_id: 'run', message_id: 'answer',
  phase: 'post_turn', started_at: 2500, status,
}];
const responses = [
  { context_usage: usage(700, 2000), context_compaction: checkpoint('mid', 2000), compaction_progress: [] },
  { context_usage: usage(700, 2000), context_compaction: checkpoint('mid', 2000), compaction_progress: progress('running') },
  { context_usage: usage(100, 3000), context_compaction: checkpoint('post', 3000), compaction_progress: progress('completed') },
];
useChatStore.setState({ currentUserId: null, contextUsages: {}, contextCompactions: {}, contextUsageEpochs: {} });
const originalFetch = globalThis.fetch;
const originalDelays = [...COMPACTION_REFRESH_DELAYS_MS];
let requests = 0;
globalThis.fetch = async () => {
  const data = responses[requests++];
  assert.ok(data, 'polling must stop after the target phase completes');
  return new Response(JSON.stringify({ code: 0, data }), { status: 200 });
};
COMPACTION_REFRESH_DELAYS_MS.fill(0);
try {
  await refreshContextAfterCompaction(chatId, '', runStarted, 'answer', 'post_turn');
  assert.equal(requests, 3, 'mid-turn checkpoint cannot end post-turn polling');
  assert.equal(useChatStore.getState().contextUsages[chatId].usedTokens, 100);
  assert.equal(useChatStore.getState().contextCompactions[chatId].checkpointId, 'post');
  console.log('compaction refresh phase race test passed');
} finally {
  globalThis.fetch = originalFetch;
  COMPACTION_REFRESH_DELAYS_MS.splice(0, COMPACTION_REFRESH_DELAYS_MS.length, ...originalDelays);
}
