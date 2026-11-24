import { create } from 'zustand';
import {
  getChatEvaluations, getPendingEvaluations, submitEvaluation,
  type EvaluationRecord, type PendingEvaluationItem,
} from '../api/evaluation';

/**
 * Task evaluations (采纳 / 需修改 / 不满意) of assistant answers, and the reminder list.
 *
 * Records are keyed by the answer's message id and kept for the signed-in user only:
 * `reset` runs when the account changes. Submitting is optimistic and rolls back on failure.
 */
interface EvaluationState {
  records: Record<string, EvaluationRecord>;
  /** Answers from scheduled or loop tasks: shown without any evaluation. */
  excluded: Record<string, true>;
  loadedChats: Record<string, true>;
  /** Up to five conversations with an unevaluated answer inside the reminder window. */
  pendingItems: PendingEvaluationItem[];
  /** Every conversation with an unevaluated answer inside the window (history list marks). */
  pendingChatIds: Record<string, true>;
  pendingTotal: number;
  /** Answer the reminder jumped to; the chat view scrolls to it once it is loaded. */
  focus: { chatId: string; messageId: string } | null;

  /** `force` re-reads a chat already loaded (a run just finished: its answer may be a loop's). */
  loadChat: (chatId: string, options?: { force?: boolean }) => Promise<void>;
  submit: (chatId: string, messageId: string, record: EvaluationRecord) => Promise<void>;
  refreshPending: () => Promise<void>;
  setFocus: (focus: EvaluationState['focus']) => void;
  reset: () => void;
}

const EMPTY = {
  records: {}, excluded: {}, loadedChats: {}, pendingItems: [], pendingChatIds: {}, pendingTotal: 0, focus: null,
};

let pendingRequest: Promise<void> | null = null;
let generation = 0;

export const useEvaluationStore = create<EvaluationState>((set, get) => ({
  ...EMPTY,

  loadChat: async (chatId, options) => {
    if (!chatId || (get().loadedChats[chatId] && !options?.force)) return;
    const gen = generation;
    set((s) => ({ loadedChats: { ...s.loadedChats, [chatId]: true } }));
    try {
      const { records, excluded } = await getChatEvaluations(chatId);
      if (gen !== generation) return;
      // Local submissions made while the request was in flight win over the server snapshot.
      set((s) => ({
        records: { ...records, ...s.records },
        excluded: excluded.some((id) => !s.excluded[id])
          ? { ...s.excluded, ...Object.fromEntries(excluded.map((id) => [id, true as const])) }
          : s.excluded,
      }));
    } catch {
      if (gen !== generation) return;
      set((s) => {
        const loadedChats = { ...s.loadedChats };
        delete loadedChats[chatId];
        return { loadedChats };
      });
    }
  },

  submit: async (chatId, messageId, record) => {
    const previous = get().records[messageId];
    set((s) => ({ records: { ...s.records, [messageId]: record } }));
    try {
      const saved = await submitEvaluation(messageId, chatId, record);
      set((s) => ({ records: { ...s.records, [messageId]: saved } }));
    } catch (error) {
      set((s) => {
        const records = { ...s.records };
        if (previous) records[messageId] = previous;
        else delete records[messageId];
        return { records };
      });
      throw error;
    }
    void get().refreshPending();
  },

  refreshPending: () => {
    if (pendingRequest) return pendingRequest;
    const gen = generation;
    pendingRequest = (async () => {
      try {
        const data = await getPendingEvaluations();
        if (gen !== generation) return;
        // Polling mostly returns the same list: keep references so subscribers do not re-render.
        const prev = get();
        const same = prev.pendingTotal === data.total
          && prev.pendingItems.length === data.items.length
          && prev.pendingItems.every((item, i) => {
            const next = data.items[i];
            return item.chat_id === next.chat_id && item.message_id === next.message_id && item.unrated === next.unrated
              && item.title === next.title;
          })
          && Object.keys(prev.pendingChatIds).length === data.chat_ids.length
          && data.chat_ids.every((id) => prev.pendingChatIds[id]);
        if (same) return;
        set({
          pendingItems: data.items,
          pendingChatIds: Object.fromEntries(data.chat_ids.map((id) => [id, true as const])),
          pendingTotal: data.total,
        });
      } catch {
        // The reminder is best effort: keep the last list rather than flashing it away.
      } finally {
        pendingRequest = null;
      }
    })();
    return pendingRequest;
  },

  setFocus: (focus) => set({ focus }),

  reset: () => {
    generation += 1;
    pendingRequest = null;
    set({ ...EMPTY });
  },
}));
