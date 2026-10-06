import type { ChatStreamOutcome } from './chatStream';
import type { ChatMessage } from '../types';
import { type ChatInvocationContext } from '../utils/chatInvocation';

export interface FollowStreamOptions {
  enableThinking?: boolean;
  pendingNotice?: string;
  signal?: AbortSignal;
  seedFrom?: ChatMessage;
}

export interface StreamingContext {
  effectiveApiUrl: string;
  generateSummary: (chatId: string) => Promise<void>;
  generateClassification: (chatId: string) => Promise<void>;
  abortControllersRef: React.RefObject<Map<string, AbortController>>;
  followUpAbortRef: React.RefObject<Map<string, AbortController>>;
  interruptedNoticeShownRef: React.RefObject<Set<string>>;
  reconcilingRef: React.RefObject<Set<string>>;
  queueDuringRun: (directMessage?: string, invocationOverride?: ChatInvocationContext) => void;
  settleQueuedMessageAfterRun: (chatId: string, assistantUid?: string, autoSend?: boolean) => void;
  reconcileDurableSteerQueue: (chatId: string, runId: string) => Promise<void>;
  syncManualTitleToBackend: (chatId: string) => void;
  send: (directMessage?: string, invocationOverride?: ChatInvocationContext) => Promise<void>;
  processRegenerateStream: (response: Response, chatId: string, opts?: FollowStreamOptions) => Promise<ChatStreamOutcome>;
  processChatStreamWithHandoffRecovery: (response: Response, chatId: string, { enableThinking, pendingNotice, signal, seedFrom }?: FollowStreamOptions) => Promise<ChatStreamOutcome>;
  smartSend: (directMessage?: string, invocationOverride?: ChatInvocationContext) => Promise<void>;
}
