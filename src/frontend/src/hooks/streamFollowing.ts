import { openRunSubscription } from '../runSubscriptionClient';
import { getChatRunSteers } from '../api';
import { useChatStore } from '../stores';
import { processChatStream } from './chatStream';
import { reloadChatHistory } from './useChatInit';
import { parseAppliedQueueHandoff, type QueuedRunHandoff } from '../utils/streamHandoff';
import { newMessageUid } from '../utils/messageIdentity';
import type { ChatMessage } from '../types';
import type { StreamingContext, FollowStreamOptions } from './streamingContext';

export function createStreamFollowing(ctx: Pick<StreamingContext, 'generateClassification' | 'generateSummary' | 'syncManualTitleToBackend'>) {


  async function processRegenerateStream(
    response: Response,
    chatId: string,
    opts: FollowStreamOptions = {},
  ) {
    const outcome = await processChatStreamWithHandoffRecovery(response, chatId, opts);
    useChatStore.getState().addBackendSessionId(chatId);
    useChatStore.getState().addLoadedMsgId(chatId);
    ctx.syncManualTitleToBackend(chatId);
    if (outcome.settled) {
      setTimeout(() => ctx.generateSummary(chatId), 500);
      setTimeout(() => ctx.generateClassification(chatId), 800);
    }
    return outcome;
  }


  async function discoverQueuedRun(
    sourceRunId: string,
    chatId: string,
  ): Promise<QueuedRunHandoff | undefined> {
    try {
      const items = await getChatRunSteers(sourceRunId, chatId);
      for (const item of items) {
        const handoff = parseAppliedQueueHandoff(
          item as unknown as Record<string, unknown>,
          sourceRunId,
        );
        if (handoff) return handoff;
      }
      return undefined;
    } catch {
      return undefined;
    }
  }


  /** Consume a stream and recover a DB-committed handoff even if its Redis event was lost. */
  async function processChatStreamWithHandoffRecovery(
    response: Response,
    chatId: string,
    { enableThinking = false, pendingNotice, signal, seedFrom }: FollowStreamOptions = {},
  ) {
    let outcome: Awaited<ReturnType<typeof processChatStream>> | undefined;
    try {
      outcome = await processChatStream(response, { chatId, enableThinking, pendingNotice, seedFrom, signal });
    } catch (error) {
      const sourceRunId = useChatStore.getState().activeRuns[chatId]?.runId;
      const recovered = await followQueuedRunChain(
        undefined,
        chatId,
        enableThinking,
        signal,
        sourceRunId,
      );
      if (!recovered) throw error;
      return recovered;
    }
    const sourceRunId = useChatStore.getState().activeRuns[chatId]?.runId;
    return (
      await followQueuedRunChain(outcome, chatId, enableThinking, signal, sourceRunId)
    ) ?? outcome;
  }


  /**
   * A followUp/nextRun, or a steer that missed the final safe boundary, is
   * committed together with the source run's completion.
   * Follow the committed child immediately so a fast child cannot finish in the
   * background before the 20-second active-run poll notices it.
   */
  async function followQueuedRunChain(
    initial: Awaited<ReturnType<typeof processChatStream>> | undefined,
    chatId: string,
    enableThinking: boolean,
    signal?: AbortSignal,
    initialSourceRunId?: string,
  ) {
    let outcome = initial;
    const seen = new Set<string>();
    let sourceRunId = initialSourceRunId;
    let usedDurableBackfill = false;
    while (!outcome?.aborted) {
      let queued = outcome?.queuedRun;
      if (!queued && sourceRunId) {
        queued = await discoverQueuedRun(sourceRunId, chatId);
        if (queued) usedDurableBackfill = true;
      }
      if (!queued) break;
      if (seen.has(queued.runId)) break;
      seen.add(queued.runId);

      useChatStore.getState().updateStore((prev) => {
        const chat = prev.chats[chatId];
        if (!chat || chat.messages.some((item) => item.messageId === queued.userMessageId)) return prev;
        const lastTs = chat.messages.length > 0 ? chat.messages[chat.messages.length - 1].ts : 0;
        const userMessage: ChatMessage = {
          role: 'user',
          content: queued.message,
          isMarkdown: false,
          uid: newMessageUid(),
          ts: Math.max(Date.now(), lastTs + 1),
          messageId: queued.userMessageId,
        };
        return {
          ...prev,
          chats: {
            ...prev.chats,
            [chatId]: {
              ...chat,
              messages: [...chat.messages, userMessage],
              updatedAt: Date.now(),
            },
          },
        };
      });
      const localQueued = useChatStore.getState().queuedMessages[chatId];
      if (localQueued?.id === queued.steerId) {
        useChatStore.getState().setQueuedMessage(chatId, null);
      }
      useChatStore.getState().setActiveRun(chatId, {
        runId: queued.runId,
        messageId: queued.messageId,
        lastOffset: 0,
      });

      const response = await openRunSubscription(queued.runId, signal, chatId);
      if (!response.ok || !response.body) throw new Error(await response.text());
      outcome = await processChatStream(response, { chatId, enableThinking, signal });
      sourceRunId = queued.runId;
    }
    if (usedDurableBackfill) {
      // The projection was incomplete, so DB history is the final authority
      // for any child that finished before its replay was attached.
      await reloadChatHistory(chatId);
    }
    return outcome;
  }
return { processRegenerateStream, discoverQueuedRun, processChatStreamWithHandoffRecovery, followQueuedRunChain };
}
