import { streamResumeSeed } from '../utils/streamResume';
import { message } from 'antd';
import { t } from '../i18n';
import { cancelChatRun, followChatRun, getActiveChatRun, getLoop } from '../api';
import { processPlanExecuteStream, processPlanGenerateStream } from './usePlanMode';
import { useChatStore, useAuthStore } from '../stores';
import { processChatStream, hasStreamedRun, isRunCancelledByUser } from './chatStream';
import { reloadChatHistory } from './useChatInit';
import { processLoopStream } from './useLoopMode';
import { useLoopStore } from '../stores/loopStore';
import type { StreamingContext } from './streamingContext';

export function createStreamRecovery(ctx: Pick<StreamingContext, 'abortControllersRef' | 'interruptedNoticeShownRef' | 'processRegenerateStream' | 'reconcileDurableSteerQueue' | 'reconcilingRef' | 'settleQueuedMessageAfterRun'>) {


  /** Plan F short-term fix: when resume / an SSE error discovers the chat's run is already
   *  failed/cancelled, explicitly wind down the zombie streaming state in the UI — clear
   *  sendingChatIds and flag the last ``isStreaming=true`` assistant message false — and for
   *  the genuinely-interrupted case (failed) show the "previous session didn't finish due to a
   *  server restart, please resend" toast once.
   *
   *  The backend startup hook ``recover_orphan_runs`` already marks zombie runs failed and
   *  writes a terminal SSE event; but the legacy frontend ``resumeRunIfAny`` code
   *  early-returned on non-running/pending without winding down, leaving the last assistant
   *  bubble's streaming cursor spinning forever. */
  function cleanupZombieRunState(chatId: string, runStatus: string) {
    const store = useChatStore.getState();
    store.updateStore((prev) => {
      const c = prev.chats[chatId];
      if (!c) return { chats: prev.chats, order: prev.order };
      const msgs = [...(c.messages || [])];
      const last = msgs[msgs.length - 1];
      if (last?.role === 'assistant' && last.isStreaming) {
        msgs[msgs.length - 1] = { ...last, isStreaming: false };
      }
      return {
        chats: { ...prev.chats, [chatId]: { ...c, messages: msgs } },
        order: prev.order,
      };
    });
    store.removeSendingChatId(chatId);
    store.clearActiveRun(chatId);
    if (runStatus === 'failed' && !ctx.interruptedNoticeShownRef.current.has(chatId)) {
      ctx.interruptedNoticeShownRef.current.add(chatId);
      message.warning(t('上次会话因服务端重启未完成，请重新发起'));
    }
  }


  /**
   * Resume an in-flight backend run after page refresh / chat switch.
   * Looks up the active run for this chat and pipes the SSE replay through
   * the same handler used for regenerate/edit. No-op if no active run.
   */
  async function reconcileLoopBar(
    chatId: string,
    active: Awaited<ReturnType<typeof getActiveChatRun>>,
  ) {
    const lp = useLoopStore.getState().livePlan;
    // Only handle a plan bar that belongs to this chat and still shows in-progress
    if (!lp || lp.chatId !== chatId || (lp.status && lp.status !== 'running')) return;
    // An active loop run will be followed by resumeRunIfAny's autonomous_loop branch → keep running
    if (active && active.run_id && active.kind === 'autonomous_loop'
      && (active.status === 'running' || active.status === 'pending')) return;
    // Otherwise the loop is no longer running — look up the real terminal state to wind down (treat as "cancelled/stopped" if unfindable)
    const TERMINAL = ['completed', 'cancelled', 'budget_exhausted', 'failed', 'awaiting_human'];
    let finalStatus = 'cancelled';
    if (lp.loopId) {
      try {
        const loop = await getLoop(lp.loopId, chatId);
        if (loop?.status && TERMINAL.includes(loop.status)) finalStatus = loop.status;
      } catch { /* if unfindable, wind down as cancelled */ }
    }
    // The user may have restarted the run during reconciliation; re-check to avoid a wrongful wind-down
    const cur = useLoopStore.getState().livePlan;
    if (cur && cur.chatId === chatId && (cur.status === 'running' || !cur.status)) {
      useLoopStore.getState().finishLivePlan(finalStatus);
    }
  }


  async function reconcileStalledRun(chatId: string) {
    if (ctx.reconcilingRef.current.has(chatId)) return;
    ctx.reconcilingRef.current.add(chatId);
    try {
      const uid = useAuthStore.getState().authUser?.user_id;
      let active: Awaited<ReturnType<typeof getActiveChatRun>> = null;
      try {
        active = await getActiveChatRun(chatId, uid);
      } catch {
        return; // 查不到就等下一轮：宁可继续转圈，也不要凭一次网络抖动拆掉正在跑的轮次
      }
      const live = !!active && (active.status === 'running' || active.status === 'pending');
      // 本地这条连接已经确定是死的（75 秒没有任何字节），先把它断掉，让气泡收尾
      ctx.abortControllersRef.current.get(chatId)?.abort();

      if (!live) {
        // 后端那轮已经终态：结束僵尸 UI，并把库里的最终消息拉回来——用户不用再手动刷新。
        cleanupZombieRunState(chatId, active?.status || 'completed');
        await reloadChatHistory(chatId);
        return;
      }

      // run 还活着 → 重挂。等 abort 的 finally 把 sendingChatIds 清掉，否则 resumeRunIfAny
      // 会被它自己的「正在发送就别插手」守卫挡回来。
      for (let i = 0; i < 12 && useChatStore.getState().sendingChatIds.has(chatId); i++) {
        await new Promise((r) => setTimeout(r, 250));
      }
      if (useChatStore.getState().sendingChatIds.has(chatId)) return; // 没让开就等下一轮
      await resumeRunIfAny(chatId);
    } finally {
      ctx.reconcilingRef.current.delete(chatId);
    }
  }


  /** 后端自己发起的那一轮 —— 批量作业跑完/中途播报会在**同一个会话里入队一条新 run**
   *  （job_wakeup），用户没点任何东西，前端也就没有任何本地流。过去只有切会话或刷新
   *  才会去看一眼有没有在跑的 run，于是这轮播报全程隐身：作业早交付完了，页面上还是
   *  用户离开时的样子。这里在当前会话空闲时补一眼，发现有活的 run 就照常跟随。 */
  async function attachServerStartedRun(chatId: string) {
    const store = useChatStore.getState();
    if (store.sendingChatIds.has(chatId) || store.activeRuns[chatId]) return;
    let active: Awaited<ReturnType<typeof getActiveChatRun>> = null;
    try {
      active = await getActiveChatRun(chatId, useAuthStore.getState().authUser?.user_id);
    } catch {
      return;
    }
    if (!active?.run_id || (active.status !== 'running' && active.status !== 'pending')) return;
    // 用户按过停止的那一轮：后端可能还没落终态，但用户的意图是终局的，不许重挂
    if (isRunCancelledByUser(active.run_id)) return;
    // 自己刚跑完那一轮的残影（本地流已收尾、后端还没落终态）——认错了会把同一轮重放成两个气泡
    if (hasStreamedRun(active.run_id)) return;
    // 这轮的用户侧消息（唤醒指令）和助手行都是后端落的库；resumeRunIfAny 会先把历史
    // 拉齐、再以库里那行为基态接上流。
    await resumeRunIfAny(chatId);
  }


  async function resumeRunIfAny(chatId: string) {
    const uid = useAuthStore.getState().authUser?.user_id;
    let active: Awaited<ReturnType<typeof getActiveChatRun>> = null;
    try {
      active = await getActiveChatRun(chatId, uid);
    } catch {
      return;
    }
    const restoredQueued = useChatStore.getState().queuedMessages[chatId];
    const durableRunId = restoredQueued?.targetRunId || active?.run_id;
    if (durableRunId) await ctx.reconcileDurableSteerQueue(chatId, durableRunId);
    // Autonomous-loop plan-bar reconciliation: a plan bar restored from localStorage may still
    // read running while the backend run has already ended (stopped / finished / crashed). As
    // long as there's no "active loop run" that would be followed below, wind the plan bar down
    // to the real loop state, so it doesn't stay stuck on "in progress" forever after a refresh.
    await reconcileLoopBar(chatId, active);

    if (!active || !active.run_id) {
      ctx.settleQueuedMessageAfterRun(chatId, undefined, false);
      return;
    }
    // 用户已经按过停止：即便后端这一轮还挂着 running（协作式取消尚未落终态、
    // 或取消请求失败），也不许再挂上去重放——那正是"中断的任务又开始执行了"。
    // 顺手补一刀取消，让后端那轮真的停下来。
    if (isRunCancelledByUser(active.run_id)) {
      cancelChatRun(active.run_id, uid, chatId).catch(() => { /* noop */ });
      cleanupZombieRunState(chatId, 'cancelled');
      ctx.settleQueuedMessageAfterRun(chatId, undefined, false);
      return;
    }
    if (active.status !== 'running' && active.status !== 'pending') {
      // Run already terminal (failed / cancelled / completed) — the backend's
      // recover_orphan_runs marks zombie running runs failed on restart and writes a terminal
      // event. But the frontend may still have leftover sendingChatIds + a last assistant
      // message with isStreaming=true. Explicitly clean up this zombie UI state, and for the
      // failed path show a toast once so the user resends.
      cleanupZombieRunState(chatId, active.status);
      // 终态的行已经在库里定稿（正文、工具卡、错误或计划快照都在），拉一次就是最终样子。
      await reloadChatHistory(chatId);
      return;
    }

    // Re-read state at the latest moment — user may have started a fresh send
    // during the active-run round-trip.
    if (useChatStore.getState().sendingChatIds.has(chatId)) return;

    // activeRun 在锁外登记：即使本标签页没拿到跟随权，停止按钮也能取消该 run
    useChatStore.getState().setActiveRun(chatId, {
      runId: active.run_id,
      messageId: active.message_id,
      lastOffset: active.last_event_offset || 0,
    });

    // ── 跨标签页互斥：同一 run 只允许一个标签页跟随 SSE ──
    // 过去复制标签页/多开时两个标签页同时 follow 同一 run，各自用不同的
    // 身份建气泡，互相覆盖 localStorage，产生重复/半截气泡与
    // "回答无对应问题"（问题17）。Web Locks 随标签页关闭自动释放。
    const runLockName = `hugagent_run_follow_${active.run_id}`;
    const activeRun = active;
    const doFollowRun = () => followActiveRun(chatId, activeRun, uid);
    const locksApi = typeof navigator !== 'undefined' ? navigator.locks : undefined;
    if (locksApi?.request) {
      await locksApi.request(runLockName, { ifAvailable: true }, async (lock: unknown) => {
        if (!lock) return; // 另一个标签页正在跟随该 run
        if (useChatStore.getState().sendingChatIds.has(chatId)) return;
        await doFollowRun();
      });
    } else {
      await doFollowRun();
    }
  }


  /** 实际跟随一个后台 run 的 SSE（plan / loop / 普通对话三种分支）。 */
  async function followActiveRun(
    chatId: string,
    active: NonNullable<Awaited<ReturnType<typeof getActiveChatRun>>>,
    uid: string | undefined,
  ) {
    const { addSendingChatId, removeSendingChatId } = useChatStore.getState();

    // Plan mode: live-replay the plan event stream (plan_step_* / tool_call / tool_result /
    // plan_complete), fully continuous with the pre-refresh progress.
    if (active.kind === 'plan_execute' || active.kind === 'plan_generate') {
      addSendingChatId(chatId);
      const ac = new AbortController();
      ctx.abortControllersRef.current.set(chatId, ac);
      try {
        const resp = await followChatRun(active.run_id, 0, ac.signal, uid, chatId);
        if (!resp.ok || !resp.body) return;
        if (active.kind === 'plan_execute' && active.plan_id) {
          await processPlanExecuteStream(resp, chatId, active.plan_id, {
            onSetCurrentPlanId: useChatStore.getState().setCurrentPlanId,
            onAfterComplete: (cid) => {
              // After replay completes, refresh the message list, replacing client-built state
              // with the final message in the DB, ensuring the stop button, isStreaming flag, etc. wind down correctly.
              void reloadChatHistory(cid);
            },
          });
        } else if (active.kind === 'plan_generate') {
          await processPlanGenerateStream(resp, chatId, {
            onSetCurrentPlanId: useChatStore.getState().setCurrentPlanId,
          });
          // Also refresh after generate completes: pick up the DB-persisted assistant message + plan_snapshot
          await reloadChatHistory(chatId);
        }
      } catch (e) {
        if (!(e instanceof Error && e.name === 'AbortError')) {
          // The task may have already finished in the DB; history is the final authority.
          await reloadChatHistory(chatId);
        }
      } finally {
        ctx.abortControllersRef.current.delete(chatId);
        removeSendingChatId(chatId);
        useChatStore.getState().clearActiveRun(chatId);
      }
      return;
    }

    // Autonomous-loop replay: full replay from offset 0, both restoring the worker's body/tool
    // bubbles and rebuilding the "plan bar" above the input box
    // (loop_plan/iteration_started/requirement_passed).
    if (active.kind === 'autonomous_loop') {
      addSendingChatId(chatId);
      // The replay's run_started frame carries the message_id, so it takes over the bubble
      // history already rendered for this run instead of drawing a second one.
      const ac = new AbortController();
      ctx.abortControllersRef.current.set(chatId, ac);
      try {
        const resp = await followChatRun(active.run_id, 0, ac.signal, uid, chatId);
        if (resp.ok && resp.body) await processLoopStream(resp, chatId, !!active.enable_thinking);
      } catch (e) {
        if (!(e instanceof Error && e.name === 'AbortError')) { /* replay failure is silent — the final message arrives with the next refresh */ }
      } finally {
        ctx.abortControllersRef.current.delete(chatId);
        removeSendingChatId(chatId);
        useChatStore.getState().clearActiveRun(chatId);
      }
      return;
    }

    // 非思考模式可从已保存的正文和 offset 续播；思考模式必须重放，
    // 因为消息行没有保存内联思考解析器所处的阶段。
    await reloadChatHistory(chatId);
    const base = useChatStore.getState().store.chats[chatId]?.messages
      .find((m) => m.role === 'assistant' && m.messageId === active.message_id);
    const seedFrom = streamResumeSeed(base, !!active.enable_thinking);
    const fromOffset = seedFrom?.inFlight?.eventOffset ?? 0;

    addSendingChatId(chatId);

    const abortController = new AbortController();
    ctx.abortControllersRef.current.set(chatId, abortController);
    let streamOutcome: Awaited<ReturnType<typeof processChatStream>> | undefined;

    try {
      const r = await followChatRun(active.run_id, fromOffset, abortController.signal, uid, chatId);
      if (!r.ok || !r.body) return;
      streamOutcome = await ctx.processRegenerateStream(r, chatId, {
        enableThinking: !!active.enable_thinking,
        signal: abortController.signal,
        seedFrom,
      });
    } catch (e) {
      if (!(e instanceof Error && e.name === 'AbortError')) {
        // Resume failure is handled silently — UX-wise it's equivalent to "the task runs in the background and the final message arrives via the next refresh"
      }
    } finally {
      ctx.abortControllersRef.current.delete(chatId);
      removeSendingChatId(chatId);
      useChatStore.getState().clearActiveRun(chatId);
      ctx.settleQueuedMessageAfterRun(
        chatId,
        streamOutcome?.bubbleUid,
        streamOutcome !== undefined,
      );
    }
  }
return { cleanupZombieRunState, reconcileLoopBar, reconcileStalledRun, attachServerStartedRun, resumeRunIfAny, followActiveRun };
}
