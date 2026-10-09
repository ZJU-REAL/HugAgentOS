import { useChatStore } from '../../stores';
import type { CitationItem,ToolCall } from '../../types';
import { reduceControls } from './controls';
import { activateStructuredReasoning,appendLateStructuredThinkContent,appendThinkContent,claimPersistedBubble,finalizeRunningTools,processTextChunk,rearmInlineReasoning } from './messageReducer';
import { appendOrUpdate,cancelStreamingUpdate } from './messageStore';
import { installRunControls } from './snapshotControls';
import type { ChatStreamState } from './state';

interface RunProjection {
  run_id: string;
  message_id: string;
  started_at: number | null;
  event_offset: number;
  last_event_ts?: number;
  blocks: Array<{ kind: 'text' | 'thinking' | 'tool' | 'protocol' | 'phase' | 'answer'; content?: string; structured?: boolean; index?: number }>;
  tools: ToolCall[];
  signals: Record<string, Record<string, unknown>>;
  citations?: CitationItem[];
  terminal: boolean;
  history?: Array<{ role: 'assistant'; state: RunProjection } |
    { role: 'user'; message_id: string; content: string; timestamp: number }>;

}

/** Replace the reducer's base with a state captured at one committed offset. */
export function installRunSnapshot(state: ChatStreamState, value: unknown) {
  const projection = value as RunProjection;
  if (!projection?.message_id || !Array.isArray(projection.blocks) || !Array.isArray(projection.tools)) {
    throw new Error('Invalid run snapshot');
  }
  for (const entry of projection.history ?? []) {
    if (entry.role === 'assistant') installRunSnapshot(state, entry.state);
    else useChatStore.getState().updateStore((prev) => {
      const chat = prev.chats[state.chatId];
      if (!chat || chat.messages.some((m) => m.messageId === entry.message_id)) return prev;
      return { ...prev, chats: { ...prev.chats, [state.chatId]: { ...chat,
        messages: [...chat.messages, { uid: entry.message_id, messageId: entry.message_id,
          role: 'user', content: entry.content, ts: entry.timestamp }] } } };
    });
  }
  cancelStreamingUpdate(state);
  if (state.metaMessageId && state.metaMessageId !== projection.message_id) {
    state.bubbleUid = projection.message_id;
  }
  state.runId = projection.run_id;
  state.eventOffset = projection.event_offset;
  state.metaMessageId = projection.message_id;
  state.bubbleStartedAt = projection.started_at ?? state.bubbleStartedAt;
  state.lastEventTs = projection.last_event_ts ?? state.lastEventTs;
  state.full = '';
  state.parseBuffer = '';
  state.thinking.length = 0;
  state.segments.length = 0;
  state.toolCalls = projection.tools;
  state.thinkingPhaseActive = state.enableThinking;
  state.structuredReasoning = false;
  state.sawThinkCloseTag = false;
  state.implicitThinkSegIdxs.clear();
  state.deferredThinkingText = undefined;
  state.toolPending = false;
  claimPersistedBubble(state);
  for (const block of projection.blocks) {
    if (block.kind === 'text') processTextChunk(state, block.content ?? '');
    else if (block.kind === 'phase') rearmInlineReasoning(state);
    else if (block.kind === 'answer') reduceControls(state, { content: block.content }, { content: block.content }, 'content_replace');
    else if (block.kind === 'protocol') activateStructuredReasoning(state);
    else if (block.kind === 'thinking') {
      if (block.structured) {
        if (!state.structuredReasoning) activateStructuredReasoning(state);
        appendLateStructuredThinkContent(state, block.content ?? '');
      } else appendThinkContent(state, block.content ?? '', true);
    } else if (block.index != null) {
      state.segments.push({ type: 'tool', toolIndex: block.index });

    }
  }
  state.allCitations = projection.citations ?? [];
  installRunControls(state, projection.signals);
  state.streamEnded = projection.terminal;
  if (state.streamEnded) finalizeRunningTools(state);
  useChatStore.getState().setActiveRun(state.chatId, {
    runId: state.runId, messageId: state.metaMessageId, lastOffset: state.eventOffset,
  });
  appendOrUpdate(state, !state.streamEnded, undefined, state.metaMessageId);
}
