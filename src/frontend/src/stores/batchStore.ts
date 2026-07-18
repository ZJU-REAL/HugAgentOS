import { create } from 'zustand';
import { openBatchStream } from '../api';
import { useChatStore } from './chatStore';
import type { BatchItemResult, BatchPlanMeta, BatchPlanState } from '../types';
import { t } from '../i18n';

/** Per-plan state, keyed by plan_id. The store also remembers which
 *  plan is currently awaiting user confirmation so the modal knows when
 *  to open.
 */
interface BatchStore {
  pendingConfirmPlanId: string | null;
  plans: Record<string, BatchPlanState>;

  /** Active SSE streams keyed by plan_id. We keep the AbortController so
   *  switching chats / unmounting can cancel cleanly without leaking the
   *  underlying fetch reader. */
  streamControllers: Record<string, AbortController>;

  /** Called when the SSE handler sees a `batch_confirm` event. */
  setPendingConfirm: (meta: BatchPlanMeta) => void;
  clearPendingConfirm: () => void;

  /** Called once the user has submitted the confirm dialog and we begin
   *  streaming execution events. */
  startRun: (planId: string, template: string) => void;

  /** Append one item result (success or skipped). */
  appendResult: (planId: string, result: BatchItemResult) => void;

  /** Optional: mark item N as currently running (for UI loading state). */
  markItemRunning: (planId: string, index: number, total: number) => void;

  /** Stream finished. */
  finish: (
    planId: string,
    summary: { total: number; success: number; failed: number; status: string },
  ) => void;

  /** Stream errored mid-flight. */
  fail: (planId: string, errorMsg: string) => void;

  /** User cancelled or closed the modal. */
  cancel: (planId: string) => void;

  /** Read helper used by Modal/Panel components. */
  getPlan: (planId: string) => BatchPlanState | undefined;

  /** Open the batch SSE stream and dispatch events into this store.
   *  Idempotent — if a controller for *planId* already exists we don't
   *  open a second stream. Use to:
   *   • follow execution after the user confirms
   *   • re-attach on chat load / page refresh when the plan was already
   *     running server-side
   *
   *  When ``meta`` is provided, hydrates the plans map first so the
   *  progress panel can render immediately even before the first event.
   */
  connectStream: (planId: string, meta?: BatchPlanMeta) => void;

  /** Cancel an active stream's fetch reader without altering plan state. */
  disconnectStream: (planId: string) => void;

  /** Hydrate a completed plan (status=done/cancelled/failed) directly
   *  from its persisted ``item_results`` — no SSE stream needed. Used
   *  on chat reload so users see prior batch results without watching
   *  a replay animation or triggering a fake "in-progress" pulse. */
  hydratePlan: (
    meta: BatchPlanMeta,
    finalStatus: 'done' | 'cancelled' | 'failed',
    results: BatchItemResult[],
    summary?: { success: number; failed: number },
  ) => void;
}

export const useBatchStore = create<BatchStore>((set, get) => ({
  pendingConfirmPlanId: null,
  plans: {},
  streamControllers: {},

  setPendingConfirm: (meta) => {
    set((state) => ({
      pendingConfirmPlanId: meta.plan_id,
      plans: {
        ...state.plans,
        [meta.plan_id]: {
          meta,
          status: 'awaiting_confirm',
          results: [],
        },
      },
    }));
  },

  clearPendingConfirm: () => set({ pendingConfirmPlanId: null }),

  startRun: (planId, template) => {
    set((state) => {
      const plan = state.plans[planId];
      if (!plan) return state;
      return {
        pendingConfirmPlanId: null,
        plans: {
          ...state.plans,
          [planId]: {
            ...plan,
            template,
            status: 'running',
            results: [],
            startedAt: Date.now(),
          },
        },
      };
    });
  },

  markItemRunning: (planId, _index, _total) => {
    // Currently we just rely on `results.length` vs `meta.total` to derive
    // a progress bar; marking the running item is a UI-only concern that
    // BatchProgressPanel can compute. Reserved for future UI tweaks.
    void _index;
    void _total;
    void planId;
  },

  appendResult: (planId, result) => {
    set((state) => {
      const plan = state.plans[planId];
      if (!plan) return state;
      // Reconnects (chat-load, switch tab + back) replay already-emitted
      // items. Skip duplicates by index so React subscribers don't
      // re-render for no semantic change.
      if (plan.results.some((r) => r.index === result.index)) return state;
      return {
        plans: {
          ...state.plans,
          [planId]: {
            ...plan,
            status: 'running',
            results: [...plan.results, result],
          },
        },
      };
    });
  },

  finish: (planId, summary) => {
    set((state) => {
      const plan = state.plans[planId];
      if (!plan) return state;
      const finalStatus: BatchPlanState['status'] =
        summary.status === 'cancelled' ? 'cancelled' : 'done';
      return {
        plans: {
          ...state.plans,
          [planId]: {
            ...plan,
            status: finalStatus,
            summary: {
              total: summary.total,
              success: summary.success,
              failed: summary.failed,
            },
            finishedAt: Date.now(),
          },
        },
      };
    });
  },

  fail: (planId, errorMsg) => {
    set((state) => {
      const plan = state.plans[planId];
      if (!plan) return state;
      return {
        plans: {
          ...state.plans,
          [planId]: {
            ...plan,
            status: 'error',
            errorMsg,
            finishedAt: Date.now(),
          },
        },
      };
    });
  },

  cancel: (planId) => {
    const plan = get().plans[planId];
    const chatId = plan?.meta.chat_id;
    set((state) => {
      const cur = state.plans[planId];
      const nextPending =
        state.pendingConfirmPlanId === planId ? null : state.pendingConfirmPlanId;
      if (!cur) return { ...state, pendingConfirmPlanId: nextPending };
      return {
        pendingConfirmPlanId: nextPending,
        plans: {
          ...state.plans,
          [planId]: {
            ...cur,
            status: 'cancelled',
            finishedAt: Date.now(),
          },
        },
      };
    });
    // Drop the sidebar pulse if no other batch stream is active for this chat.
    if (chatId) {
      const plans = get().plans;
      const stillRunning = Object.values(plans).some((p) =>
        p.meta.chat_id === chatId
        && p.meta.plan_id !== planId
        && (p.status === 'running' || p.status === 'awaiting_confirm')
      );
      if (!stillRunning) {
        useChatStore.getState().removeSendingChatId(chatId);
      }
    }
  },

  getPlan: (planId) => get().plans[planId],

  connectStream: (planId, meta) => {
    // Hydrate plan state first so the progress panel has something to
    // render immediately on chat-load reconnect.
    const chatId = meta?.chat_id ?? get().plans[planId]?.meta.chat_id;
    if (meta) {
      set((state) => {
        const existing = state.plans[planId];
        if (existing) return state;  // don't clobber active state
        return {
          plans: {
            ...state.plans,
            [planId]: {
              meta,
              status: 'running',
              results: [],
              startedAt: Date.now(),
            },
          },
        };
      });
    }

    if (get().streamControllers[planId]) return;  // already streaming

    // Light up the sidebar pulse dot for the host chat — same indicator
    // chat_runs use, so the user has a single mental model for "this chat
    // has work running in it" regardless of whether it's a regular agent
    // turn or a batch execution.
    if (chatId) {
      useChatStore.getState().addSendingChatId(chatId);
    }

    const clearPulse = () => {
      if (!chatId) return;
      // Only remove the pulse if we own the only reason for it (no other
      // batch streams are active for the same chat). Cheap check — walk
      // the plans map and bail if any other unfinished plan shares this
      // chat_id.
      const plans = get().plans;
      const stillRunning = Object.values(plans).some((p) =>
        p.meta.chat_id === chatId
        && p.meta.plan_id !== planId
        && (p.status === 'running' || p.status === 'awaiting_confirm')
      );
      if (!stillRunning) {
        useChatStore.getState().removeSendingChatId(chatId);
      }
    };

    const ctrl = openBatchStream(
      planId,
      (evt) => {
        const evtType = evt.type;
        if (evtType === 'batch_item_done') {
          get().appendResult(planId, {
            index: typeof evt.index === 'number' ? evt.index : 0,
            total: typeof evt.total === 'number' ? evt.total : undefined,
            status: (evt.status as 'success' | 'skipped') || 'success',
            content: typeof evt.content === 'string' ? evt.content : undefined,
            error: typeof evt.error === 'string' ? evt.error : undefined,
            retry_count: typeof evt.retry_count === 'number' ? evt.retry_count : 0,
            progress: (evt.progress as { done: number; success: number; failed: number } | undefined),
            // Carry through tool calls / artifacts / citations so the
            // panel can render rich outputs via the chat-bubble primitives.
            tool_calls: Array.isArray(evt.tool_calls) ? (evt.tool_calls as BatchItemResult['tool_calls']) : undefined,
            artifacts: Array.isArray(evt.artifacts) ? evt.artifacts : undefined,
            citations: Array.isArray(evt.citations) ? (evt.citations as BatchItemResult['citations']) : undefined,
          });
        } else if (evtType === 'batch_done') {
          get().finish(planId, {
            total: typeof evt.total === 'number' ? evt.total : 0,
            success: typeof evt.success === 'number' ? evt.success : 0,
            failed: typeof evt.failed === 'number' ? evt.failed : 0,
            status: typeof evt.status === 'string' ? evt.status : 'done',
          });
          set((state) => {
            const next = { ...state.streamControllers };
            delete next[planId];
            return { streamControllers: next };
          });
          clearPulse();
        } else if (evtType === 'batch_error') {
          get().fail(planId, typeof evt.error === 'string' ? evt.error : t('执行异常'));
          clearPulse();
        }
      },
      (err) => {
        get().fail(planId, err.message || t('流式连接异常'));
        clearPulse();
      },
    );

    set((state) => ({
      streamControllers: { ...state.streamControllers, [planId]: ctrl },
    }));
  },

  disconnectStream: (planId) => {
    const ctrl = get().streamControllers[planId];
    if (ctrl) {
      ctrl.abort();
      set((state) => {
        const next = { ...state.streamControllers };
        delete next[planId];
        return { streamControllers: next };
      });
    }
  },

  hydratePlan: (meta, finalStatus, results, summary) => {
    set((state) => {
      // Don't clobber a plan that's currently active — let the live
      // stream be the source of truth.
      const existing = state.plans[meta.plan_id];
      if (existing && (existing.status === 'running' || existing.status === 'awaiting_confirm')) {
        return state;
      }
      const total = meta.total || results.length;
      const success = summary?.success ?? results.filter(r => r.status === 'success').length;
      const failed = summary?.failed ?? results.filter(r => r.status === 'skipped').length;
      const status: BatchPlanState['status'] =
        finalStatus === 'cancelled' ? 'cancelled'
          : finalStatus === 'failed' ? 'error'
            : 'done';
      return {
        plans: {
          ...state.plans,
          [meta.plan_id]: {
            meta,
            status,
            results,
            summary: { total, success, failed },
            finishedAt: Date.now(),
          },
        },
      };
    });
  },
}));
