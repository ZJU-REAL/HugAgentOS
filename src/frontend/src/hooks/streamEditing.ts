import { Modal, message } from 'antd';
import { requestChatRerun } from '../api/chatReruns';
import { readComposer, chatDraftKey } from '../stores/composerStore';
import { composeCommandMessage } from '../utils/projectCommands';
import { t } from '../i18n';
import { cancelChatRun, getActiveChatRun, cancelPlanApi } from '../api';
import { useChatStore, useAuthStore } from '../stores';
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

      const r = await requestChatRerun(streamChatId, messageIndex, abortController.signal);
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
        role: 'user', content: newContent.trim(), isMarkdown: false, uid: newMessageUid(true), ts: Date.now(),
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

      const r = await requestChatRerun(streamChatId, messageIndex, abortController.signal, newContent.trim());
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
    const draft = readComposer(chatDraftKey(useChatStore.getState().currentChatId));
    const compactCommand = (directMessage?.trim() || composeCommandMessage(draft.input, draft.activeCommand)) === '/compact';
    if (sending && compactCommand) {
      void message.info(t('请等待当前回复结束后再压缩上下文'));
      return;
    }
    if (sending) {
      ctx.queueDuringRun(directMessage, invocationOverride);
      return;
    }
    if (compactCommand) {
      return ctx.send(directMessage, invocationOverride);
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
   *  the local SSE connection.
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

  }


  function continueLoop(chatId?: string) {
    return continueLoopImpl(ctx.abortControllersRef, chatId);
  }
return { regenerate, editAndResend, smartSend, abort, continueLoop };
}
