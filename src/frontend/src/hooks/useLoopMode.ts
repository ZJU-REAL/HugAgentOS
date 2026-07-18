import { message } from 'antd';
import { t } from '../i18n';
import { createLoop, startLoop, resumeLoop } from '../api';
import { useChatStore } from '../stores';
import { isThinkingMode } from '../stores/chatStore';
import { useLoopStore } from '../stores/loopStore';
import type { LoopPlanReq } from '../stores/loopStore';
import { processChatStream } from './chatStream';
import type { ChatItem, ChatMessage } from '../types';

/**
 * SSE stream processing for the autonomous loop: the worker's body/thinking/tools
 * are rendered into the assistant bubble by the **same** unified stream processor
 * (processChatStream) as normal chat — there is no loop-specific reducer copy anymore.
 * The loop-only structural events (loop_started/loop_plan/iteration_started/
 * requirement_passed/loop_completed/loop_error) are intercepted via the onEvent hook,
 * which only drives the "plan bar" above the input box. Shared by both the send path
 * and the refresh-resume path (resumeRunIfAny).
 *
 * `enableThinking`: the thinking mode of this run (the send path takes the current
 * chatMode, the resume path takes the active-run meta info); it decides the initial
 * phase of the <think> stripper.
 */
export async function processLoopStream(
  resp: Response,
  chatId: string,
  enableThinking?: boolean,
) {
  const et = enableThinking ?? isThinkingMode(useChatStore.getState().chatMode);
  return processChatStream(resp, {
    chatId,
    enableThinking: et,
    onEvent: (ev, api) => {
      const store = useLoopStore.getState();
      switch (ev.type) {
        // ── Loop structure: drives the "plan bar" above the input box ──
        case 'loop_started': {
          // On resume (after refresh) use it to rebuild the plan bar; the send path
          // already called startLivePlan earlier, so guard here to avoid resetting.
          const lpNow = store.livePlan;
          if (!lpNow || lpNow.chatId !== chatId) store.startLivePlan(chatId, String(ev.objective || ''));
          // Record loop_id — the "Continue" button uses it to resume the same loop
          // from the breakpoint (including the refresh-resume scenario).
          if (ev.loop_id) store.setLiveLoopId(String(ev.loop_id));
          return true;
        }
        case 'loop_resumed':
          return true;
        case 'loop_plan':
          store.setLivePlanReqs((ev.requirements as LoopPlanReq[]) || [], ev.objective as string | undefined);
          return true;
        case 'iteration_started':
          store.setLiveCurrent((ev.requirement_id as string) ?? null, ev.progress as string | undefined);
          return true;
        // Verification/re-review signals from the read-only review sub-agent (driver-triggered,
        // maker≠checker) — drives the "reviewing" hint on the plan bar; pure observation
        // events, just consume them, they don't go into the assistant bubble.
        case 'loop_review_started':
          store.setLiveReviewing?.(true, (ev.requirement_id as string) ?? null, !!ev.second_pass);
          return true;
        case 'loop_review_result':
          store.setLiveReviewing?.(false, (ev.requirement_id as string) ?? null, !!ev.second_pass);
          return true;
        case 'requirement_passed':
          if (ev.requirement_id) store.markLivePassed(String(ev.requirement_id), ev.progress as string | undefined);
          return true;
        case 'loop_completed':
          store.finishLivePlan(String(ev.status || 'completed'));
          return true;
        case 'loop_error':
          if (api.hasText()) api.appendText('\n\n');
          api.appendText(`❌ ${String(ev.error || t('循环出错'))}`);
          api.refresh();
          return true;
        default:
          return false;
      }
    },
  });
}

/**
 * Autonomous-loop send in chat mode: treat the user message as the objective, create the
 * loop and follow it streaming (fully bound to the project selected in the input box).
 * The worker's real work each iteration (body + tool cards) flows into the assistant bubble
 * **the same way as normal chat**; the plan and progress are shown in the plan bar above the
 * input box (persisted, recoverable and resumable after refresh).
 */
export async function sendLoopMode(
  abortControllersRef: React.MutableRefObject<Map<string, AbortController>>,
  directMessage?: string,
) {
  const {
    input, setInput, sending, addSendingChatId, removeSendingChatId,
    currentChatId, updateStore,
  } = useChatStore.getState();
  const msg = directMessage?.trim() || input.trim();
  if (!msg || sending) return;
  if (!currentChatId) {
    message.error(t('请先新建或选择一个对话。'));
    return;
  }

  const streamChatId = currentChatId;
  addSendingChatId(streamChatId);
  if (!directMessage) setInput('');

  // 1) Optimistically render the user objective (the assistant placeholder bubble is created by the unified stream processor)
  const userMsg: ChatMessage = { role: 'user', content: msg, isMarkdown: false, ts: Date.now() };
  updateStore((prev) => {
    const c = prev.chats[currentChatId];
    const nextChat: ChatItem = {
      ...(c || { id: currentChatId, title: '新对话', createdAt: Date.now(), updatedAt: Date.now(), messages: [], favorite: false, pinned: false, businessTopic: '综合咨询' }),
      messages: [...(c?.messages || []), userMsg],
      updatedAt: Date.now(),
      title: c?.title && c.title !== '新对话' ? c.title : msg.slice(0, 18) || '新对话',
    };
    return { chats: { ...prev.chats, [currentChatId]: nextChat }, order: [currentChatId, ...(prev.order || []).filter((x) => x !== currentChatId)] };
  });

  // 2) Start the plan bar immediately (before the backend responds), then fill in requirements from the loop_plan event
  useLoopStore.getState().startLivePlan(currentChatId, msg);

  const ac = new AbortController();
  abortControllersRef.current.set(streamChatId, ac);

  try {
    // The loop is fully bound to the project the user selected in the input box: pass the
    // project_id bound to the current chat (chat.projectId is the single source of truth);
    // the worker/reviewer work inside the project folder (where the site source lives) and publish via publish_site.
    const currentProjectId = useChatStore.getState().store.chats[currentChatId]?.projectId;
    const loop = await createLoop({
      title: msg.slice(0, 40),
      goal_spec: { objective: msg },
      chat_id: currentChatId,
      ...(currentProjectId ? { project_id: currentProjectId } : {}),
    });
    useLoopStore.getState().setLiveLoopId(loop.loop_id);
    // The worker's thinking mode **fully** follows the mode the user confirmed in the chat:
    // chat_mode is passed through verbatim (fast/medium/high/max), no longer collapsed to a
    // boolean — the backend sets reasoning_effort accordingly.
    const chatMode = useChatStore.getState().chatMode;
    const enableThinking = isThinkingMode(chatMode);
    const resp = await startLoop(loop.loop_id, { enable_thinking: enableThinking, chat_mode: chatMode }, ac.signal);
    if (!resp.ok) throw new Error(t('循环启动失败: {status}', { status: resp.status }));
    const outcome = await processLoopStream(resp, currentChatId, enableThinking);
    // The unified stream processor digests AbortError into a normal wrap-up (the bubble is
    // finalized) — the plan bar's "cancelled" state is set here based on outcome (abort()
    // has another fallback for it).
    if (outcome.aborted) useLoopStore.getState().finishLivePlan('cancelled');
  } catch (e: unknown) {
    const err = e as { name?: string; message?: string };
    if (err?.name !== 'AbortError') {
      message.error(t('自主循环失败：{msg}', { msg: err?.message || String(e) }));
    } else {
      useLoopStore.getState().finishLivePlan('cancelled');
    }
  } finally {
    abortControllersRef.current.delete(streamChatId);
    removeSendingChatId(streamChatId);
    useChatStore.getState().clearActiveRun(streamChatId);
  }
}

/**
 * "Continue" a disconnected/stopped autonomous loop: call resume on the same loop_id, the
 * driver reads feature_list.json in the persistent sandbox and resumes from the breakpoint
 * (does not start over). Reuses the shared abortControllersRef so the stop button also works
 * on the resumed stream. Bound to the chat and loop_id recorded in the plan bar.
 */
export async function continueLoop(
  abortControllersRef: React.MutableRefObject<Map<string, AbortController>>,
  chatId?: string,
) {
  const { currentChatId, sending, addSendingChatId, removeSendingChatId } = useChatStore.getState();
  const targetId = chatId || currentChatId;
  if (!targetId || sending) return;
  const lp = useLoopStore.getState().livePlan;
  if (!lp || lp.chatId !== targetId || !lp.loopId) {
    message.error(t('找不到可继续的循环'));
    return;
  }
  const loopId = lp.loopId;

  addSendingChatId(targetId);
  useLoopStore.getState().reviveLivePlan();  // Revive the plan bar to "in progress"
  const ac = new AbortController();
  abortControllersRef.current.set(targetId, ac);

  try {
    const chatMode = useChatStore.getState().chatMode;
    const enableThinking = isThinkingMode(chatMode);
    const resp = await resumeLoop(loopId, { enable_thinking: enableThinking, chat_mode: chatMode }, ac.signal);
    if (!resp.ok) throw new Error(t('循环启动失败: {status}', { status: resp.status }));
    const outcome = await processLoopStream(resp, targetId, enableThinking);
    if (outcome.aborted) useLoopStore.getState().finishLivePlan('cancelled');
  } catch (e: unknown) {
    const err = e as { name?: string; message?: string };
    if (err?.name === 'AbortError') {
      useLoopStore.getState().finishLivePlan('cancelled');
    } else {
      useLoopStore.getState().finishLivePlan('failed');
      message.error(t('继续循环失败：{msg}', { msg: err?.message || String(e) }));
    }
  } finally {
    abortControllersRef.current.delete(targetId);
    removeSendingChatId(targetId);
    useChatStore.getState().clearActiveRun(targetId);
  }
}
