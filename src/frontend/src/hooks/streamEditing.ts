import { Modal, message } from 'antd';
import { t } from '../i18n';
import { authFetch, regenerateMessage, editAndRegenerate, cancelChatRun, getActiveChatRun, cancelBatchPlan, cancelPlanApi } from '../api';
import { useChatStore, useAuthStore, useBatchStore } from '../stores';
import { isThinkingMode } from '../stores/chatStore';
import { markRunCancelledByUser } from './chatStream';
import { type ChatInvocationContext } from '../utils/chatInvocation';
import { sendPlanMode } from './planSend';
import { sendLoopMode, continueLoop as continueLoopImpl } from './useLoopMode';
import { useLoopStore } from '../stores/loopStore';
import { newMessageUid } from '../utils/messageIdentity';
import type { ChatMessage } from '../types';
import type { StreamingContext } from './streamingContext';

export function createStreamEditing(ctx: Pick<StreamingContext, 'abortControllersRef' | 'effectiveApiUrl' | 'followUpAbortRef' | 'generateSummary' | 'processRegenerateStream' | 'queueDuringRun' | 'send'>) {


  /** Regenerate the last assistant response */
  async function regenerate(messageIndex: number) {
    const { sending, addSendingChatId, removeSendingChatId, currentChatId, truncateMessagesFrom } = useChatStore.getState();
    if (sending) return;
    const streamChatId = currentChatId;
    addSendingChatId(streamChatId);

    const abortController = new AbortController();
    ctx.abortControllersRef.current.set(streamChatId, abortController);

    try {
      const chat = useChatStore.getState().store.chats[streamChatId];
      const targetMsg = chat?.messages[messageIndex];
      if (targetMsg) {
        truncateMessagesFrom(streamChatId, targetMsg);
      }

      const r = await regenerateMessage(streamChatId, messageIndex, abortController.signal);
      if (!r.ok || !r.body) throw new Error(await r.text());

      await ctx.processRegenerateStream(r, streamChatId, {
        enableThinking: isThinkingMode(useChatStore.getState().chatMode),
        signal: abortController.signal,
      });
    } catch (e) {
      if (!(e instanceof Error && e.name === 'AbortError')) {
        message.error(t('重新生成失败：{msg}', { msg: e instanceof Error ? e.message : String(e) }));
      }
    } finally {
      ctx.abortControllersRef.current.delete(streamChatId);
      removeSendingChatId(streamChatId);
    }
  }


  /** Edit a user message and regenerate */
  async function editAndResend(messageIndex: number, newContent: string) {
    const { sending, addSendingChatId, removeSendingChatId, currentChatId, truncateMessagesFrom, setEditingMessageUid } = useChatStore.getState();
    if (!newContent.trim()) return;

    // 编辑重发是**破坏性**的：后端 delete_messages_from 会把这条之后的消息全部硬删，
    // 撤不回来。编辑第一轮时，后面几十轮问答会一声不响地消失（问题 24）。
    // 所以在动手之前先把代价说清楚，让用户自己决定。
    {
      const chat = useChatStore.getState().store.chats[currentChatId];
      const msgs = chat?.messages || [];
      const droppedRounds = msgs.slice(messageIndex + 1).filter((m) => m.role === 'user').length;
      if (droppedRounds > 0) {
        const confirmed = await new Promise<boolean>((resolve) => {
          Modal.confirm({
            title: t('编辑后将丢弃后续对话'),
            content: t('这条消息之后还有 {n} 轮问答，编辑重发会把它们一并删除且无法恢复。确定继续吗？', { n: droppedRounds }),
            okText: t('继续编辑'),
            okButtonProps: { danger: true },
            cancelText: t('取消'),
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!confirmed) return;
      }
    }
    if (sending) {
      // 正在流式输出时点「发送」：先停止当前回答再编辑重发（对齐主流产品行为），
      // 而不是静默吞掉点击。abort 触发本地 AbortError → 原流的 finally 清理
      // sendingChatIds；等一拍让清理落地后继续。
      abort(currentChatId);
      await new Promise((res) => setTimeout(res, 250));
    }
    const streamChatId = currentChatId;
    addSendingChatId(streamChatId);
    setEditingMessageUid(null);

    const abortController = new AbortController();
    ctx.abortControllersRef.current.set(streamChatId, abortController);

    try {
      const chat = useChatStore.getState().store.chats[streamChatId];
      const targetMsg = chat?.messages[messageIndex];
      if (targetMsg) {
        truncateMessagesFrom(streamChatId, targetMsg);
      }

      // Add the edited user message to local store. Editing only rewrites the text —
      // the backend replays the original turn's attachments and its skill / plugin /
      // connector / @agent selection, so the local echo has to keep showing them.
      const userMsg: ChatMessage = {
        role: 'user', content: newContent.trim(), isMarkdown: false, uid: newMessageUid(), ts: Date.now(),
        ...(targetMsg?.attachments?.length ? { attachments: targetMsg.attachments } : {}),
        ...(targetMsg?.quotedFollowUp ? { quotedFollowUp: targetMsg.quotedFollowUp } : {}),
        ...(targetMsg?.skillId ? { skillId: targetMsg.skillId } : {}),
        ...(targetMsg?.skillName ? { skillName: targetMsg.skillName } : {}),
        ...(targetMsg?.pluginName ? { pluginName: targetMsg.pluginName } : {}),
        ...(targetMsg?.connectorName ? { connectorName: targetMsg.connectorName } : {}),
        ...(targetMsg?.mentionName ? { mentionName: targetMsg.mentionName } : {}),
      };
      useChatStore.getState().updateStore((prev) => {
        const c = prev.chats[streamChatId];
        const msgs = [...(c?.messages || []), userMsg];
        return {
          chats: { ...prev.chats, [streamChatId]: { ...c, messages: msgs, updatedAt: Date.now() } },
          order: [streamChatId, ...(prev.order || []).filter(x => x !== streamChatId)],
        };
      });

      const r = await editAndRegenerate(streamChatId, messageIndex, newContent.trim(), abortController.signal);
      if (!r.ok || !r.body) throw new Error(await r.text());

      await ctx.processRegenerateStream(r, streamChatId, {
        enableThinking: isThinkingMode(useChatStore.getState().chatMode),
        signal: abortController.signal,
      });
    } catch (e) {
      if (!(e instanceof Error && e.name === 'AbortError')) {
        message.error(t('编辑重发失败：{msg}', { msg: e instanceof Error ? e.message : String(e) }));
      }
    } finally {
      ctx.abortControllersRef.current.delete(streamChatId);
      removeSendingChatId(streamChatId);
    }
  }


  async function smartSend(directMessage?: string, invocationOverride?: ChatInvocationContext) {
    const { planMode, loopMode, sending } = useChatStore.getState();
    if (sending) {
      ctx.queueDuringRun(directMessage, invocationOverride);
      return;
    }
    if (planMode) {
      return sendPlanMode(ctx.effectiveApiUrl, ctx.abortControllersRef, ctx.generateSummary, directMessage);
    }
    if (loopMode) {
      return sendLoopMode(ctx.abortControllersRef, directMessage);
    }
    return ctx.send(directMessage, invocationOverride);
  }


  /** Abort the stream for a specific chat (defaults to the currently viewed chat).
   *  Actually kills the background task: first call /v1/chat-runs/{run_id}/cancel, then abort
   *  the local SSE connection. Also cancels any batch plans still executing on this chat —
   *  batch tasks have their own SSE stream and plan_id, are not in abortControllersRef, and
   *  must be handled separately.
   */
  function abort(chatId?: string) {
    const targetId = chatId || useChatStore.getState().currentChatId;
    const activeRun = useChatStore.getState().activeRuns[targetId];
    const uid = useAuthStore.getState().authUser?.user_id;
    if (activeRun?.runId) {
      // 先登记用户意图，再发取消请求：取消是 fire-and-forget，请求失败或后端
      // 协作式取消慢一拍时，这条登记保证任何重挂路径都不会再把它捡起来重放。
      markRunCancelledByUser(activeRun.runId);
      // fire-and-forget: a failed cancel call must not block the local abort
      cancelChatRun(activeRun.runId, uid, targetId).catch(() => { /* noop — backend orphan recovery is the safety net */ });
    } else {
      // 本窗口没拿到这一轮的 run_id（跟随权在另一个窗口 / 刷新后还没挂上）时，
      // 旧代码直接跳过取消，后端那轮继续跑；切回来 resumeRunIfAny 一挂就表现成
      // "已中断的任务又开始执行了"。这里补一次反查，按会话取活的 run 再取消。
      void getActiveChatRun(targetId, uid)
        .then((run) => {
          if (!run?.run_id) return;
          markRunCancelledByUser(run.run_id);
          return cancelChatRun(run.run_id, uid, targetId);
        })
        .catch(() => { /* noop — 后端孤儿回收兜底 */ });
    }
    const controller = ctx.abortControllersRef.current.get(targetId);
    if (controller) {
      controller.abort();
      ctx.abortControllersRef.current.delete(targetId);
    }

    // 计划模式：把后端的计划和它的 run 一并取消。只 abort 本地 SSE 是不够的——计划在
    // 后端仍是 approved/执行中，切走再切回来（resumeRunIfAny 会重新挂上那个还活着的
    // run）就表现为「已经中断的任务又自己跑起来了」（问题 32）。
    {
      const chat = useChatStore.getState().store.chats[targetId];
      const execPlanId = (chat?.messages || [])
        .flatMap((m) => m.segments || [])
        .filter((seg) => seg.type === 'plan' && seg.planData?.mode === 'executing' && !seg.planData?.cancelled)
        .map((seg) => seg.planData?.planId)
        .filter((id): id is string => !!id)
        .pop();
      if (execPlanId) {
        cancelPlanApi(execPlanId, targetId).catch(() => { /* noop —— 本地已经断流，后端有孤儿回收兜底 */ });
      }
    }

    // Autonomous loop: on stop, wind the chat's "plan bar" down from running to cancelled —
    // otherwise the replay path's AbortError is silently swallowed and the plan bar stays stuck
    // on "in progress" forever (bug fix).
    const _lp = useLoopStore.getState().livePlan;
    if (_lp && _lp.chatId === targetId && (_lp.status === 'running' || !_lp.status)) {
      useLoopStore.getState().finishLivePlan('cancelled');
    }

    // Cancel post-stream follow-up question polling for this chat —
    // the loop is fire-and-forget so without this it survives chat
    // switches as a leaked timer + pending fetch.
    const pollAc = ctx.followUpAbortRef.current.get(targetId);
    if (pollAc) {
      pollAc.abort();
      ctx.followUpAbortRef.current.delete(targetId);
    }

    // Cancel batch plans on this chat that are still running or awaiting confirmation
    const batchState = useBatchStore.getState();
    const activePlans = Object.values(batchState.plans).filter(
      (p) => p.meta.chat_id === targetId
        && (p.status === 'running' || p.status === 'awaiting_confirm'),
    );
    for (const p of activePlans) {
      // Backend: mark cancelled + cancel the in-flight runner task
      cancelBatchPlan(p.meta.plan_id).catch(() => { /* noop */ });
      // Frontend: immediately close the SSE fetch + update store state
      batchState.disconnectStream(p.meta.plan_id);
      batchState.cancel(p.meta.plan_id);
    }
  }


  /** Cancel a pending batch plan and re-stream the original user message
   *  with batch_plan disabled, so the agent answers via ordinary tools.
   *
   *  The backend endpoint (POST /v1/batch/{plan_id}/cancel-and-resume):
   *    1. marks the plan cancelled
   *    2. deletes the assistant turn that triggered batch_plan
   *    3. re-streams the user message with disable_batch_plan=true
   *
   *  Frontend mirrors the assistant-turn deletion in chatStore so the UI
   *  reflects the same state, then consumes the SSE via the regenerate
   *  pipeline (since the response shape is identical).
   */
  async function cancelAndResumeBatch(planId: string, chatId: string) {
    const { addSendingChatId, removeSendingChatId, truncateMessagesFrom } =
      useChatStore.getState();
    addSendingChatId(chatId);

    // Drop the dangling empty assistant turn from the local store. We pick
    // the latest assistant message — the backend does the same lookup
    // server-side so the two stay in sync.
    const chat = useChatStore.getState().store.chats[chatId];
    if (chat?.messages?.length) {
      for (let i = chat.messages.length - 1; i >= 0; i--) {
        const m = chat.messages[i];
        if (m.role === 'assistant') {
          truncateMessagesFrom(chatId, m);
          break;
        }
      }
    }

    const abortController = new AbortController();
    ctx.abortControllersRef.current.set(chatId, abortController);

    try {
      const r = await authFetch(
        `${ctx.effectiveApiUrl}/v1/batch/${encodeURIComponent(planId)}/cancel-and-resume`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: abortController.signal,
        },
      );
      if (!r.ok || !r.body) {
        throw new Error(await r.text() || `cancel-and-resume failed: ${r.status}`);
      }
      // The endpoint streams the same SSE shape as /chats/regenerate, so
      // we can reuse the existing consumer.
      await ctx.processRegenerateStream(r, chatId, {
        enableThinking: isThinkingMode(useChatStore.getState().chatMode),
        signal: abortController.signal,
      });
    } catch (e) {
      if (!(e instanceof Error && e.name === 'AbortError')) {
        message.error(t('取消批量并继续失败：{msg}', { msg: e instanceof Error ? e.message : String(e) }));
        throw e;
      }
    } finally {
      ctx.abortControllersRef.current.delete(chatId);
      removeSendingChatId(chatId);
    }
  }


  function continueLoop(chatId?: string) {
    return continueLoopImpl(ctx.abortControllersRef, chatId);
  }
return { regenerate, editAndResend, smartSend, abort, cancelAndResumeBatch, continueLoop };
}
