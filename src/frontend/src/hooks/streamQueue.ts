import { chatDraftKey, readComposer } from '../stores/composerStore';
import { message } from 'antd';
import { t } from '../i18n';
import { steerChatRun, getChatRunSteers, withdrawChatRunSteer, getActiveChatRun } from '../api';
import { resolveBatchModeActive } from '../utils/chatMode';
import { useChatStore, useAuthStore } from '../stores';
import { reloadChatHistory } from './useChatInit';
import { chatInvocationMessageProps, createQueuedChatTurn, hasChatInvocation, normalizeChatInvocation, queuedChatInvocation, type ChatInvocationContext } from '../utils/chatInvocation';
import { newMessageUid } from '../utils/messageIdentity';
import type { ChatMessage } from '../types';
import type { QueuedChatMessage } from '../stores/chatStore';
import type { StreamingContext } from './streamingContext';

export function createStreamQueue(ctx: Pick<StreamingContext, 'abortControllersRef' | 'smartSend'>) {


  function queueDuringRun(
    directMessage?: string,
    invocationOverride?: ChatInvocationContext,
  ) {
    const state = useChatStore.getState();
    const draft = readComposer(chatDraftKey(state.currentChatId));
    const content = (directMessage ?? draft.input).trim();
    if (!content) return;
    const chat = state.currentChat();
    if (state.planMode || state.loopMode || resolveBatchModeActive(chat)) {
      message.info(t('当前运行模式暂不支持追加消息'));
      return;
    }
    if (state.queuedMessages[state.currentChatId]) {
      message.info(t('已有一条待发送消息，请先编辑或删除'));
      return;
    }
    const queued: QueuedChatMessage = createQueuedChatTurn({
      id: `steer_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
      content,
      createdAt: Date.now(),
      source: draft,
      invocationOverride,
    });
    state.setQueuedMessage(state.currentChatId, queued);
    if (directMessage === undefined) draft.consume(draft, ['text', 'invocation']);
  }


  function sendQueuedAsNextTurn(chatId: string, queued: QueuedChatMessage) {
    const store = useChatStore.getState();
    const staleController = ctx.abortControllersRef.current.get(chatId);
    if (staleController) {
      staleController.abort();
      ctx.abortControllersRef.current.delete(chatId);
    }
    store.removeSendingChatId(chatId);
    store.clearActiveRun(chatId);
    store.setQueuedMessage(chatId, null);

    if (store.currentChatId !== chatId) {
      store.setQueuedMessage(chatId, { ...queued, status: 'queued' });
      return;
    }

    window.setTimeout(() => {
      const latest = useChatStore.getState();
      if (latest.currentChatId !== chatId || latest.sendingChatIds.has(chatId)) {
        latest.setQueuedMessage(chatId, { ...queued, status: 'queued' });
        return;
      }
      void ctx.smartSend(queued.content, queuedChatInvocation(queued));
    }, 0);
  }


  function settleQueuedMessageAfterRun(
    chatId: string,
    assistantUid?: string,
    autoSend = true,
  ) {
    const store = useChatStore.getState();
    const queued = store.queuedMessages[chatId];
    if (!queued) return;

    if (queued.status === 'applied') {
      if (queued.appliedMessageId) {
        commitAppliedQueuedMessage(chatId, queued, assistantUid);
      } else {
        // A restored durable card has no local SSE message id. Its user turn
        // is already committed in the DB, so reload history instead of
        // inventing a duplicate local message.
        void reloadChatHistory(chatId);
      }
      store.setQueuedMessage(chatId, null);
      return;
    }

    // The backend still owns an accepted/claimed durable instruction. Do not
    // turn it into a new local send merely because the source SSE ended.
    if (queued.status === 'steering' && queued.targetRunId) return;

    if (autoSend && store.currentChatId === chatId) {
      sendQueuedAsNextTurn(chatId, queued);
      return;
    }

    if (queued.status === 'steering') {
      store.updateQueuedMessage(chatId, (current) => ({
        ...current,
        status: 'queued',
      }));
    }
  }


  async function activateQueuedMessage(chatId?: string) {
    const state = useChatStore.getState();
    const targetId = chatId || state.currentChatId;
    const queued = state.queuedMessages[targetId];
    if (!queued || queued.status === 'applied') return;

    if (!state.sendingChatIds.has(targetId)) {
      state.setQueuedMessage(targetId, null);
      if (state.currentChatId === targetId) {
        await ctx.smartSend(queued.content, queuedChatInvocation(queued));
      }
      return;
    }

    const activeRun = state.activeRuns[targetId];
    if (!activeRun?.runId) {
      message.info(t('任务正在启动，请稍后再试'));
      return;
    }

    // The visible answer may have finished a moment before the local SSE
    // finally block clears `sendingChatIds`. Confirm the run is still live so
    // an instruction is not queued against a run that has no next tool call.
    let liveRun: Awaited<ReturnType<typeof getActiveChatRun>> | undefined;
    try {
      liveRun = await getActiveChatRun(
        targetId,
        useAuthStore.getState().authUser?.user_id,
      );
    } catch {
      // A transient probe failure should not turn a live steer into a duplicate
      // ordinary message; continue with the locally tracked run.
    }
    const currentQueued = useChatStore.getState().queuedMessages[targetId];
    if (!currentQueued || currentQueued.id !== queued.id) {
      // The old stream may have settled and started this queued message while
      // the active-run probe was in flight. Do not cancel or send it twice.
      return;
    }
    if (liveRun === null) {
      sendQueuedAsNextTurn(targetId, currentQueued);
      return;
    }

    // The current AgentScope executor has already assembled its skills/tools.
    // Referenced turns must start through the ordinary chat endpoint after this
    // run completes so the backend can validate and assemble the requested capability.
    if (hasChatInvocation(currentQueued.invocation)) {
      message.info(t('带引用的消息将在当前任务结束后发送'));
      return;
    }

    const runId = liveRun?.run_id || activeRun.runId;
    if (liveRun?.run_id && liveRun.run_id !== activeRun.runId) {
      state.setActiveRun(targetId, {
        runId: liveRun.run_id,
        messageId: liveRun.message_id,
        lastOffset: liveRun.last_event_offset || 0,
      });
    }

    state.updateQueuedMessage(targetId, (current) => ({
      ...current,
      status: 'steering',
      targetRunId: runId,
    }));
    try {
      const accepted = await steerChatRun(runId, currentQueued.id, currentQueued.content, targetId);
      useChatStore.getState().updateQueuedMessage(targetId, (current) => ({
        ...current,
        status: accepted.status === 'applied' ? 'applied' : 'steering',
        targetRunId: runId,
        durableStatus: accepted.status,
      }));
      // The POST response can be lost after durable acceptance. Query the
      // authoritative queue so the card reflects server state instead of
      // guessing from transport success alone.
      await reconcileDurableSteerQueue(targetId, runId);
    } catch (error) {
      // A transport error can happen after the database accepted the request.
      // Reconcile the stable steer id before making the card retryable.
      await reconcileDurableSteerQueue(targetId, runId);
      const reconciled = useChatStore.getState().queuedMessages[targetId];
      if (reconciled?.durableStatus) return;
      let stillLive: Awaited<ReturnType<typeof getActiveChatRun>> | undefined;
      try {
        stillLive = await getActiveChatRun(
          targetId,
          useAuthStore.getState().authUser?.user_id,
        );
      } catch {
        // Keep the queued card actionable when the status probe also fails.
      }
      if (stillLive === null) {
        const latestQueued = useChatStore.getState().queuedMessages[targetId];
        if (latestQueued) sendQueuedAsNextTurn(targetId, latestQueued);
        return;
      }
      useChatStore.getState().updateQueuedMessage(targetId, (current) => ({
        ...current,
        status: 'queued',
        targetRunId: undefined,
        durableStatus: undefined,
      }));
      message.error(t('立即开始失败：{msg}', { msg: (error as Error).message || String(error) }));
    }
  }


  async function discardQueuedMessage(chatId?: string) {
    const state = useChatStore.getState();
    const targetId = chatId || state.currentChatId;
    const queued = state.queuedMessages[targetId];
    if (!queued || queued.status === 'applied') return;
    const activeRun = state.activeRuns[targetId];
    const durableRunId = queued.targetRunId || activeRun?.runId;
    if (queued.status === 'steering' && durableRunId) {
      try {
        const removed = await withdrawChatRunSteer(durableRunId, queued.id, targetId);
        if (!removed) {
          message.info(t('指令已经生效，无法撤回'));
          return;
        }
      } catch (error) {
        message.error(t('撤回失败：{msg}', { msg: (error as Error).message || String(error) }));
        return;
      }
    }
    useChatStore.getState().setQueuedMessage(targetId, null);
  }


  /** Reconcile a restored queue card with the database-backed five-state queue. */
  async function reconcileDurableSteerQueue(chatId: string, runId: string) {
    const queued = useChatStore.getState().queuedMessages[chatId];
    if (!queued) return;
    try {
      const items = await getChatRunSteers(runId, chatId);
      const durable = items.find((item) => item.steer_id === queued.id);
      if (!durable) return;
      if (durable.status === 'applied') {
        useChatStore.getState().updateQueuedMessage(chatId, (current) => ({
          ...current,
          status: 'applied',
          targetRunId: runId,
          durableStatus: 'applied',
        }));
      } else if (durable.status === 'accepted' || durable.status === 'claimed') {
        useChatStore.getState().updateQueuedMessage(chatId, (current) => ({
          ...current,
          status: 'steering',
          targetRunId: runId,
          durableStatus: durable.status,
        }));
      } else {
        // cancelled / superseded are no longer owned by the backend worker;
        // restore an editable local card instead of silently dropping text.
        useChatStore.getState().updateQueuedMessage(chatId, (current) => ({
          ...current,
          id: `steer_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
          status: 'queued',
          targetRunId: undefined,
          durableStatus: undefined,
        }));
      }
    } catch {
      // A status-read outage does not change whether the instruction was
      // accepted. Keep the restored card untouched and retry on next resume.
    }
  }


  function commitAppliedQueuedMessage(
    chatId: string,
    queued: QueuedChatMessage,
    assistantUid?: string,
  ) {
    useChatStore.getState().updateStore((prev) => {
      const chat = prev.chats[chatId];
      if (!chat) return prev;
      const messages = [...chat.messages];
      if (queued.appliedMessageId && messages.some((item) => item.messageId === queued.appliedMessageId)) {
        return prev;
      }
      const userMessage: ChatMessage = {
        role: 'user',
        content: queued.content,
        isMarkdown: false,
        uid: newMessageUid(),
        ts: Date.now(),
        messageId: queued.appliedMessageId,
        ...chatInvocationMessageProps(normalizeChatInvocation(queued.invocation)),
      };
      let assistantIndex = assistantUid === undefined
        ? -1
        : messages.findIndex((item) => item.uid === assistantUid);
      if (assistantIndex < 0) {
        for (let index = messages.length - 1; index >= 0; index -= 1) {
          if (messages[index].role === 'assistant') {
            assistantIndex = index;
            break;
          }
        }
      }
      messages.splice(assistantIndex >= 0 ? assistantIndex : messages.length, 0, userMessage);
      return {
        ...prev,
        chats: {
          ...prev.chats,
          [chatId]: { ...chat, messages, updatedAt: Date.now() },
        },
      };
    });
  }
return { queueDuringRun, sendQueuedAsNextTurn, settleQueuedMessageAfterRun, activateQueuedMessage, discardQueuedMessage, reconcileDurableSteerQueue, commitAppliedQueuedMessage };
}
