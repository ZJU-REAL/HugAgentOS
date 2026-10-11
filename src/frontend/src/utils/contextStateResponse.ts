import type { ChatState } from '../stores/chatState';
import { parseContextCompactionState, parseContextUsageSnapshot } from './contextUsageSnapshot';

export function captureContextState(state: ChatState, chatId: string) {
  return {
    epoch: state.contextUsageEpochs[chatId] || 0,
    usage: state.contextUsages[chatId],
    compaction: state.contextCompactions[chatId],
  };
}

/** Accept new measurements, but never erase newer state with a delayed empty response. */
export function applyContextStateResponse(
  state: ChatState,
  chatId: string,
  request: ReturnType<typeof captureContextState>,
  payload: { context_usage?: unknown; context_compaction?: unknown },
): boolean {
  if ((state.contextUsageEpochs[chatId] || 0) !== request.epoch) return false;
  if (Object.prototype.hasOwnProperty.call(payload, 'context_usage')) {
    const usage = parseContextUsageSnapshot(payload.context_usage);
    if (usage || state.contextUsages[chatId] === request.usage) state.setContextUsage(chatId, usage);
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'context_compaction')) {
    const compaction = parseContextCompactionState(payload.context_compaction);
    if (compaction || state.contextCompactions[chatId] === request.compaction) {
      state.setContextCompaction(chatId, compaction);
    }
  }
  return true;
}
