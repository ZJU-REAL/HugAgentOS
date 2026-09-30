import { useEffect, useEffectEvent } from 'react';
import { getPendingConfirm, getPendingUserQuestions, listPendingConfirms, listPendingUserQuestions, mergePendingUserQuestionRecovery } from '../api';
import { isLocalDraftChat, useAuthStore, useUIStore } from '../stores';
import type { UserQuestionRequest } from '../types';
export function useChatRecovery(currentChatId: string, resumeRunIfAny: (id: string) => unknown) {
  const authUserId = useAuthStore(s => s.authUser?.user_id);
  const resumeCurrentRun = useEffectEvent((id: string) => { void resumeRunIfAny(id); });
  // ── Resume: when switching/refreshing into a chat, re-subscribe if the backend still has a run in progress ──
  useEffect(() => {
    if (!authUserId || !currentChatId) return;
    resumeCurrentRun(currentChatId);
  }, [currentChatId, authUserId]);

  // ── §13: when switching/refreshing into a chat, restore "pending-confirm" write-op bars from the backend registry ──
  // pendingConfirm lives only in the in-memory uiStore and is lost on refresh; the backend
  // _myspace_confirm registry is the authority on whether the chat still has pending items —
  // restore from it (or clear ones that are no longer valid).
  useEffect(() => {
    if (!authUserId || !currentChatId) return;
    // 只在浏览器里存在、还没发过一句话的新对话：服务端没有它，两个恢复请求必然 404。
    if (isLocalDraftChat(currentChatId)) return;
    let cancelled = false;
    const chatId = currentChatId;
    const questionIdsAtRequestStart = new Set(
      (useUIStore.getState().pendingUserQuestions[chatId] ?? [])
        .map((request) => request.requestId),
    );
    void Promise.allSettled([
      getPendingConfirm(chatId).then(({ confirms, designPick }) => {
        if (cancelled) return;
        useUIStore.getState().hydratePendingConfirmQueue(chatId, confirms);
        // Site-builder pick-one-of-three designs: the backend is the authority — restore the pick card if present, clear stale ones if not.
        useUIStore.getState().setPendingDesignPick(chatId, designPick);
      }),
      getPendingUserQuestions(chatId).then((requests) => {
        if (cancelled) return;
        const ui = useUIStore.getState();
        // Preserve requests delivered by SSE after this GET began. Otherwise
        // an older response snapshot could erase a newer question event.
        const merged = mergePendingUserQuestionRecovery(
          requests,
          ui.pendingUserQuestions[chatId] ?? [],
          questionIdsAtRequestStart,
        );
        ui.hydratePendingUserQuestionQueue(chatId, merged);
      }),
    ]);
    return () => { cancelled = true; };
  }, [currentChatId, authUserId]);

  // ── §13: light up sidebar blue dots in one pass after first paint/refresh (no need to open each chat) ──
  useEffect(() => {
    if (!authUserId) return;
    listPendingConfirms()
      .then(({ confirms, designPicks }) => {
        const ui = useUIStore.getState();
        ui.hydratePendingConfirms(confirms);
        // design_pick uses its own single slot (the Sidebar blue dot reads it too); not mixed into the write-confirm queue
        for (const { chatId, info } of designPicks) ui.setPendingDesignPick(chatId, info);
      })
      .catch(() => { /* silent */ });
  }, [authUserId]);

  // Pending model questions are cross-tab state: only one tab follows the run
  // SSE, while another tab/device may answer first. Reconcile the global
  // backend snapshot periodically so stale yellow dots/cards disappear even
  // in tabs that do not own the stream follower lock.
  useEffect(() => {
    if (!authUserId) return;
    let cancelled = false;
    let inFlight = false;

    const reconcile = async () => {
      if (inFlight) return;
      inFlight = true;
      const idsAtStart = new Map<string, Set<string>>();
      for (const [chatId, requests] of Object.entries(
        useUIStore.getState().pendingUserQuestions,
      )) {
        idsAtStart.set(chatId, new Set(requests.map((request) => request.requestId)));
      }
      try {
        const items = await listPendingUserQuestions();
        if (cancelled) return;
        const byChat = new Map<string, UserQuestionRequest[]>();
        for (const { chatId, request } of items) {
          byChat.set(chatId, [...(byChat.get(chatId) ?? []), request]);
        }
        const ui = useUIStore.getState();
        const chatIds = new Set([
          ...Object.keys(ui.pendingUserQuestions),
          ...byChat.keys(),
        ]);
        for (const chatId of chatIds) {
          const merged = mergePendingUserQuestionRecovery(
            byChat.get(chatId) ?? [],
            ui.pendingUserQuestions[chatId] ?? [],
            idsAtStart.get(chatId) ?? new Set<string>(),
          );
          ui.hydratePendingUserQuestionQueue(chatId, merged);
        }
      } catch {
        // Keep the in-memory/SSE state when the recovery endpoint is unavailable.
      } finally {
        inFlight = false;
      }
    };

    void reconcile();
    const timer = window.setInterval(() => { void reconcile(); }, 15_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [authUserId]);

}
