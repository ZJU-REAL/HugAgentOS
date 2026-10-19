import { useCommandCompletions } from './useCommandCompletions';
import { useComposerFiles } from './useComposerFiles';
import { useEffect, useRef } from 'react';
import { useChatStore } from '../stores';
import { getStreamActivityTs } from './chatStream';
import { createStreamQueue } from './streamQueue';
import { createStreamSend } from './streamSend';
import { createStreamFollowing } from './streamFollowing';
import { createStreamEditing } from './streamEditing';
import { createStreamRecovery } from './streamRecovery';

export function useStreaming(
effectiveApiUrl: string,
generateSummary: (chatId: string) => Promise<void>,
generateClassification: (chatId: string) => Promise<void>
) {

  const { handleFileSelect, removeFile } = useComposerFiles(effectiveApiUrl);

  /** AbortControllers keyed by chat id — allows multiple chats to stream in parallel
   *  (e.g. user starts chat A, switches to new chat B, sends while A is still running). */
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());

  /** Separate AbortControllers for the post-stream follow-up question polling
   *  loop. The main `abortControllersRef` is cleared in the SSE `finally`
   *  block before polling starts, so we can't reuse it — without independent
   *  tracking the polling fires-and-forgets and survives chat switches /
   *  logouts as a memory-leaking ghost request. */
  const followUpAbortRef = useRef<Map<string, AbortController>>(new Map());

  /** Plan F short-term fix: dedupe which chats have already been shown the "session
   *  interrupted" toast. Each chat gets it once, so users switching back and forth between
   *  chats aren't spammed. The Set lives only in the current hook instance (reset on page
   *  refresh, which exactly matches the "new window should re-notify" semantics). */
  const interruptedNoticeShownRef = useRef<Set<string>>(new Set());


  /* ───────────────────────────────────────────
     断连看门狗 —— 「后台早跑完了，前台还在转圈，只能靠刷新页面才发现」的根治。

     成因：一轮长工具（批量作业 run_job 实测能把一轮撑到 54 分钟）期间，SSE 上只有每
     15 秒一行心跳。中间任何一层（代理、NAT、休眠唤醒）把连接悄悄掐成半开时，fetch 既
     不报错也不结束，reader 就永远 await 下去——气泡于是停在最后一帧转圈，而后端那轮
     早已正常跑完落库。过去唯一的出路是用户自己刷新页面（resumeRunIfAny 只在切会话/
     刷新时跑一次）。

     判据用**传输层活性**而不是「有没有新内容」：长工具期间没有可渲染事件是正常的，
     心跳断了才是真断了。连丢 5 拍（75 秒）才动手，宁可晚一点也不误伤慢链路。
     ─────────────────────────────────────────── */
  const RUN_STALL_MS = 75_000;

  const RUN_WATCH_EVERY_MS = 20_000;

  /** 正在对账的会话，防止两轮定时器叠在同一个会话上互相拆台。 */
  const reconcilingRef = useRef<Set<string>>(new Set());
  const serverRunDiscoveryRef = useRef<Set<string>>(new Set());

  const queue = createStreamQueue({
    abortControllersRef,
    smartSend: (...args) => editing.smartSend(...args)
  });
  const following = createStreamFollowing({
    generateClassification,
    generateSummary,
    syncManualTitleToBackend: (...args) => sending.syncManualTitleToBackend(...args)
  });
  const sending = createStreamSend({
    abortControllersRef,
    effectiveApiUrl,
    followUpAbortRef,
    generateClassification,
    generateSummary,
    processChatStreamWithHandoffRecovery: following.processChatStreamWithHandoffRecovery,
    settleQueuedMessageAfterRun: queue.settleQueuedMessageAfterRun
  });
  const editing = createStreamEditing({
    abortControllersRef,
    effectiveApiUrl,
    followUpAbortRef,
    generateSummary,
    processRegenerateStream: following.processRegenerateStream,
    queueDuringRun: queue.queueDuringRun,
    send: sending.send
  });
  const recovery = createStreamRecovery({
    abortControllersRef,
    interruptedNoticeShownRef,
    processRegenerateStream: following.processRegenerateStream,
    reconcileDurableSteerQueue: queue.reconcileDurableSteerQueue,
    reconcilingRef,
    serverRunDiscoveryRef,
    settleQueuedMessageAfterRun: queue.settleQueuedMessageAfterRun
  });


  useCommandCompletions(recovery.attachServerStartedRun);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const { sendingChatIds, activeRuns, currentChatId } = useChatStore.getState();
      const now = Date.now();
      sendingChatIds.forEach((cid) => {
        // 还没拿到 run_id 的（刚 POST 出去、首帧未到）不归看门狗管
        if (!activeRuns[cid]) return;
        const last = getStreamActivityTs(cid);
        if (!last || now - last < RUN_STALL_MS) return;
        void recovery.reconcileStalledRun(cid);
      });
      // 后台标签页不必占着这一发：切回来时 currentChatId 的 effect 本来就会补跟随
      if (currentChatId && typeof document !== 'undefined' && document.visibilityState !== 'hidden') {
        void recovery.attachServerStartedRun(currentChatId);
      }
    }, RUN_WATCH_EVERY_MS);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);


  return {
    send: editing.smartSend,
    abort: editing.abort,
    activateQueuedMessage: queue.activateQueuedMessage,
    discardQueuedMessage: queue.discardQueuedMessage,
    handleFileSelect,
    removeFile,
    regenerate: editing.regenerate,
    editAndResend: editing.editAndResend,
    resumeRunIfAny: recovery.resumeRunIfAny,
    continueLoop: editing.continueLoop,
  };
}
