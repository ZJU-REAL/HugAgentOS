import { useEffect, useRef } from 'react';
import { useChatStore } from '../stores';
import { scrollIntoViewAndFlash } from '../utils/scroll';
import { usePolling } from './usePolling';
import { useAuthStore } from '../stores/authStore';
import { useEvaluationStore } from '../stores/evaluationStore';

const REFRESH_MS = 5 * 60 * 1000;
/** A run's verdict lands on the server shortly after the stream ends. */
const AFTER_RUN_MS = 1500;

/**
 * Keeps the signed-in user's evaluation state: resets on account change, loads the open chat's
 * evaluations and refreshes the pending list. Mounted once with the chat view.
 */
export function useEvaluationScope(chatId: string) {
  const userId = useAuthStore((s) => s.authUser?.user_id ?? null);
  const enabled = useAuthStore((s) => s.authUser?.can_use_evaluation === true);
  const sending = useChatStore((s) => s.sending);
  const loadChat = useEvaluationStore((s) => s.loadChat);
  const refreshPending = useEvaluationStore((s) => s.refreshPending);
  const lastUser = useRef<string | null>(null);

  useEffect(() => {
    if (lastUser.current !== null && lastUser.current !== userId) useEvaluationStore.getState().reset();
    lastUser.current = userId;
  }, [userId]);

  useEffect(() => {
    if (userId && chatId) void loadChat(chatId);
  }, [userId, chatId, loadChat]);

  // Fetch at once for each account; then poll, paused while the page is hidden and refreshed
  // when it becomes visible again.
  useEffect(() => {
    if (enabled) void refreshPending();
  }, [enabled, userId, refreshPending]);
  usePolling(refreshPending, { interval: REFRESH_MS, enabled });

  // A finished answer becomes something to evaluate — unless it came from a loop task: re-read
  // the chat at once so a loop's answer never shows the evaluation.
  const wasSending = useRef(sending);
  useEffect(() => {
    const finished = wasSending.current && !sending;
    wasSending.current = sending;
    if (!finished || !userId || !chatId) return;
    void loadChat(chatId, { force: true });
    if (!enabled) return;
    const timer = window.setTimeout(() => { void refreshPending(); }, AFTER_RUN_MS);
    return () => window.clearTimeout(timer);
  }, [enabled, sending, refreshPending, loadChat, userId, chatId]);
}

/** How long the jump waits for the answer's evaluation card to render (history loads first). */
const FOCUS_WAIT_MS = 4000;

/**
 * After the reminder jumps to a chat, bring that answer's evaluation card into view and flash it.
 * The card sits at the end of the answer, so centring the whole message would leave it off screen.
 */
export function useEvaluationFocus(chatId: string) {
  const focus = useEvaluationStore((s) => s.focus);
  const loaded = useChatStore((s) => !!s.store.chats[chatId]?.messages.length);
  useEffect(() => {
    if (!focus || focus.chatId !== chatId || !loaded) return;
    const deadline = Date.now() + FOCUS_WAIT_MS;
    let timer = 0;
    const tryFocus = () => {
      const card = document.querySelector<HTMLElement>(`[data-eval-message="${CSS.escape(focus.messageId)}"]`);
      if (card) {
        scrollIntoViewAndFlash(card);
      } else if (Date.now() < deadline) {
        timer = window.setTimeout(tryFocus, 150);
        return;
      }
      useEvaluationStore.getState().setFocus(null);
    };
    // Let the chat's own open-at-bottom scroll settle first.
    timer = window.setTimeout(tryFocus, 300);
    return () => window.clearTimeout(timer);
  }, [focus, chatId, loaded]);
}
