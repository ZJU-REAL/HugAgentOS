import { useEffect, useRef } from 'react';

import { getTurnSettlement } from '../api';
import { useChatStore } from '../stores';
import type { EvolutionSummary } from '../types';

/**
 * Watches for a turn's settlement and reveals the card once it lands.
 *
 * Settlement cannot complete while the stream is open — memory writes are
 * scheduled after it closes and their extractors call an LLM — so the closing
 * frame only carries a `pending` marker. This hook polls the durable endpoint
 * until the turn reports a terminal state. Nothing is rendered while it does:
 * the card appears only if the settlement has something to report.
 *
 * Running out of attempts settles the card to `empty`, not `failed`. A poll
 * that outran the backend says nothing about whether the turn evolved anything
 * — and the overwhelming majority of turns evolve nothing — so treating the
 * timeout as a failure turned a silent non-event into "本轮进化结算未完成" on
 * ordinary conversations. Only the backend declares a real failure.
 */
const POLL_INTERVAL_MS = 3000;
// ~90s: the memory pipeline's extractors have their own 30s timeout, and the
// settlement runner waits up to another 45s (its watchdog) for their real write
// count before reporting — a 60s budget quietly outran slow-but-successful
// settlements, so this sits beyond the backend's worst case.
const MAX_ATTEMPTS = 30;

const TERMINAL_STATES: EvolutionSummary['state'][] = ['settled', 'failed', 'empty'];

export function useEvolutionSettlement(
  messageId: string | undefined,
  state: EvolutionSummary['state'] | undefined,
  onSettled: (summary: EvolutionSummary) => void,
) {
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  useEffect(() => {
    if (!messageId || state !== 'pending') return undefined;

    let cancelled = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      if (cancelled) return;
      attempts += 1;
      try {
        const summary = await getTurnSettlement(messageId);
        if (cancelled) return;
        if (TERMINAL_STATES.includes(summary.state)) {
          onSettledRef.current(summary);
          return;
        }
      } catch {
        // A transient failure is not a settlement failure; keep trying until
        // the attempt budget runs out.
      }
      if (attempts >= MAX_ATTEMPTS) {
        if (!cancelled) {
          // Stop watching without claiming anything happened or failed. If the
          // settlement lands later it is on the message, and a reload shows it.
          onSettledRef.current({ message_id: messageId, state: 'empty' });
        }
        return;
      }
      timer = setTimeout(poll, POLL_INTERVAL_MS);
    };

    timer = setTimeout(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [messageId, state]);
}

/** Patch a settled summary onto the message it belongs to. */
export function applySettlement(chatId: string, messageId: string, summary: EvolutionSummary) {
  const state = useChatStore.getState();
  const messages = state.store?.chats?.[chatId]?.messages;
  if (!Array.isArray(messages)) return;
  const next = messages.map((m) =>
    m.messageId === messageId ? { ...m, evolution: summary } : m,
  );
  state.updateMessages(chatId, next);
}
