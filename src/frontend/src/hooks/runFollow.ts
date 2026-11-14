import type { ActiveChatRun } from '../api';
import { openRunSubscription } from '../runSubscriptionClient';
import { useChatStore } from '../stores';
import { streamResumeSeed } from '../utils/streamResume';
import { hasStreamedRun, isRunCancelledByUser, processChatStream, type ChatStreamOutcome } from './chatStream';
import { reloadChatHistory } from './chatHistoryLoader';
import type { FollowStreamOptions } from './streamingContext';

/**
 * Following a chat's run from outside the turn that started it: the chat was opened while a run was going, the
 * page reloaded mid-run, or the server started the run (a background wake-up, a Personal Agent task). Ordinary
 * chats and read-only views of other chats share these steps, so a live run looks the same wherever it is shown.
 */

/** A run worth following: still going, not one the user stopped, and not one this tab has just streamed itself. */
export function isFollowableRun(active: ActiveChatRun | null | undefined): active is ActiveChatRun {
  return !!active?.run_id && (active.status === 'running' || active.status === 'pending')
    && !isRunCancelledByUser(active.run_id) && !hasStreamedRun(active.run_id);
}

/**
 * Run `follow` only if no other tab of this browser follows the same run. Two tabs following one run would each
 * draw their own bubble and overwrite each other's saved copy. Resolves to `busy` when another tab has it.
 */
export async function withRunFollowLock<T>(runId: string, follow: () => Promise<T>, busy: T): Promise<T> {
  const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined;
  if (!locks?.request) return follow();
  return locks.request(`hugagent_run_follow_${runId}`, { ifAvailable: true },
    async (lock: unknown) => (lock ? follow() : busy));
}

export interface FollowRunOptions {
  signal?: AbortSignal;
  /** Bring the chat's history up to date first, so the stream resumes on the stored row. */
  reload?: (chatId: string) => Promise<unknown>;
  /** Consume the stream; ordinary chats add title and queue handling around the shared reducer. */
  consume?: (response: Response, chatId: string, opts: FollowStreamOptions) => Promise<ChatStreamOutcome>;
}

/**
 * Follow `active` into the chat store: the stored row gives the bubble its identity, the run's subscription
 * replays its aligned state and then streams. `opened` is false when the subscription could not be opened.
 */
export async function followRunStream(chatId: string, active: ActiveChatRun, {
  signal, reload = reloadChatHistory,
  consume = (response, id, opts) => processChatStream(response, { chatId: id, enableThinking: !!opts.enableThinking, signal: opts.signal, seedFrom: opts.seedFrom }),
}: FollowRunOptions = {}): Promise<{ opened: boolean; outcome?: ChatStreamOutcome }> {
  const store = useChatStore.getState();
  store.setActiveRun(chatId, { runId: active.run_id, messageId: active.message_id, lastOffset: active.last_event_offset || 0 });
  await reload(chatId);
  const base = useChatStore.getState().store.chats[chatId]?.messages
    .find((m) => m.role === 'assistant' && m.messageId === active.message_id);
  store.addSendingChatId(chatId);
  try {
    const response = await openRunSubscription(active.run_id, signal, chatId);
    if (!response.ok || !response.body) return { opened: false };
    const outcome = await consume(response, chatId, { enableThinking: !!active.enable_thinking, signal, seedFrom: streamResumeSeed(base) });
    return { opened: true, outcome };
  } finally {
    useChatStore.getState().removeSendingChatId(chatId);
    useChatStore.getState().clearActiveRun(chatId);
  }
}
