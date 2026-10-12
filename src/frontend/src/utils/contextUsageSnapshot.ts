import type { ContextCompactionState, ContextUsageSnapshot, ContextUsageSource } from '../types';
import type { ContextBreakdown } from './contextUsage';

const CONTEXT_USAGE_SOURCES: ContextUsageSource[] = [
  'provider',
  'backend_estimate',
  'compaction_estimate',
];

/** Parse and validate the snake_case snapshot returned by API/SSE. */
export function parseContextUsageSnapshot(value: unknown): ContextUsageSnapshot | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  if (raw.schema_version !== 'context-usage.v2') return null;
  const source = typeof raw.source === 'string' && CONTEXT_USAGE_SOURCES.includes(raw.source as ContextUsageSource)
    ? raw.source as ContextUsageSource
    : null;
  const usedTokens = Number(raw.used_tokens);
  const promptTokens = Number(raw.prompt_tokens);
  const completionTokens = Number(raw.completion_tokens);
  const contextWindow = Number(raw.context_window);
  const modelCallIndex = Number(raw.model_call_index ?? 0);
  const rawBreakdown = raw.breakdown;
  if (!source || !rawBreakdown || typeof rawBreakdown !== 'object') return null;

  const b = rawBreakdown as Record<string, unknown>;
  const breakdown: ContextBreakdown = {
    messages: Number(b.messages),
    tools: Number(b.tools),
    thinking: Number(b.thinking),
    files: Number(b.files),
    system: Number(b.system),
    input: Number(b.input),
    total: 0,
  };
  const values = [
    usedTokens,
    promptTokens,
    completionTokens,
    contextWindow,
    modelCallIndex,
    breakdown.messages,
    breakdown.tools,
    breakdown.thinking,
    breakdown.files,
    breakdown.system,
    breakdown.input,
  ];
  if (values.some((n) => !Number.isSafeInteger(n) || n < 0)) return null;
  const categoryValues = values.slice(5);
  breakdown.total = categoryValues.reduce((sum, n) => sum + n, 0);
  if (breakdown.total !== usedTokens || promptTokens + completionTokens !== usedTokens) return null;

  const capturedAtMs = Number(raw.captured_at_ms ?? 0);
  const chatSeq = Number(raw.chat_seq ?? 0);
  if (![capturedAtMs, chatSeq].every((n) => Number.isSafeInteger(n) && n >= 0)) return null;
  return {
    capturedAtMs,
    chatSeq,
    source,
    exact: raw.exact === true && source === 'provider',
    usedTokens,
    promptTokens,
    completionTokens,
    contextWindow,
    modelName: typeof raw.model_name === 'string' ? raw.model_name : undefined,
    modelProviderId: typeof raw.model_provider_id === 'string' ? raw.model_provider_id : undefined,
    modelCallIndex,
    breakdown,
  };
}

/** Parse the snake_case checkpoint projection returned by the API/SSE. */
export function parseContextCompactionState(value: unknown): ContextCompactionState | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  const checkpointId = typeof raw.checkpoint_id === 'string' ? raw.checkpoint_id : '';
  const checkpointCreatedAt = typeof raw.checkpoint_created_at === 'string'
    ? Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/i.test(raw.checkpoint_created_at)
      ? raw.checkpoint_created_at : raw.checkpoint_created_at + 'Z')
    : Number.NaN;
  const coveredMessageCount = Number(raw.covered_message_count);
  const replacementTokens = Number(raw.replacement_tokens);
  const contextUsage = parseContextUsageSnapshot(raw.context_usage);
  if (
    !checkpointId
    || !Number.isSafeInteger(coveredMessageCount)
    || coveredMessageCount < 0
    || !Number.isSafeInteger(replacementTokens)
    || replacementTokens < 0
  ) {
    return null;
  }
  return {
    checkpointId,
    checkpointTs: Number.isNaN(checkpointCreatedAt) ? 0 : checkpointCreatedAt,
    coveredThroughMessageId: typeof raw.covered_through_message_id === 'string'
      ? raw.covered_through_message_id
      : undefined,
    coveredMessageCount,
    replacementTokens,
    ...(contextUsage ? { contextUsage } : {}),
  };
}


/** Server capture time orders checkpoints and same-row model calls across transports. */
export function isOlderContextUsage(
  incoming: ContextUsageSnapshot,
  current?: ContextUsageSnapshot,
): boolean {
  if (!current) return false;
  if (incoming.capturedAtMs && current.capturedAtMs) {
    return incoming.capturedAtMs < current.capturedAtMs;
  }
  // Pre-upgrade snapshots have no clock. Do not let legacy history replace a
  // fresh live measurement. A new legacy checkpoint is handled by its boundary.
  if (current.capturedAtMs && !incoming.capturedAtMs) return true;
  return !!incoming.chatSeq && !!current.chatSeq && incoming.chatSeq < current.chatSeq;
}
