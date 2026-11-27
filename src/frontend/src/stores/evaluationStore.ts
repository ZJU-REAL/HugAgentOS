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
 *
 * Evaluation gate: right after a run the server may still be deciding whether an answer should
 * ask for an evaluation (`gating`). While the server lists any answer of the chat as gating (it
 * bounds that itself), the chat is re-read every GATE_POLL_MS. GATE_LOCAL_CAP_MS only guards
 * against reads that keep failing: an answer undecided that long gets the card (fail open).
 * Answers the gate let go (`notNeeded`) offer the quick like / dislike instead of the card.
 * Both lists are kept per chat and replaced by each (latest) server snapshot.
 */
interface EvaluationState {
  records: Record<string, EvaluationRecord>;
  /** Answers from scheduled or loop tasks: shown without any evaluation. */
  excluded: Record<string, true>;
  /** The evaluation gate decided no evaluation is needed: quick like / dislike only. */
  notNeeded: Record<string, true>;
  /** The gate has not decided yet: no evaluation card until it does (or the poll gives up). */
  gating: Record<string, true>;
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
  /** Forget a chat that was left: stop waiting for its gate decisions; it is re-read when opened again. */
  stopGating: (chatId: string) => void;
  /**
   * A run just delivered `messageId`: when the gate is known to be on, hold its card back until the
   * re-read says what the gate decided (no flash of the card in between).
   */
  expectGate: (chatId: string, messageId: string) => void;
  /** Whether gate decisions matter to this user (only users with the 会话评价 card wait for them). */
  setGateActive: (active: boolean) => void;
  reset: () => void;
}

const EMPTY = {
  records: {}, excluded: {}, notNeeded: {}, gating: {}, loadedChats: {}, pendingItems: [], pendingChatIds: {}, pendingTotal: 0, focus: null,
};

let pendingRequest: Promise<void> | null = null;
let generation = 0;

/** How often a chat with undecided answers is re-read. */
const GATE_POLL_MS = 1500;
/**
 * Local cap on waiting for one answer's decision. The server stops listing an answer as gating
 * after the gate timeout (≤ 60 s) plus a grace period; this only covers reads that keep failing.
 */
const GATE_LOCAL_CAP_MS = 75_000;

/**
 * Per chat: its gating answers with when each was first seen gating (its local cap counts from
 * there), its latest request (older replies don't touch gating), the poll.
 */
interface GateWatch {
  since: Map<string, number>;
  request: number;
  timer: number | null;
  deadline: number | null;
}
const watches = new Map<string, GateWatch>();
/** Per chat: the not-needed ids of the newest applied snapshot, and that snapshot's request. */
const notNeededByChat = new Map<string, { ids: string[]; request: number }>();
/** Answers whose local cap ran out: they keep the card even if a late reply still lists them. */
const gaveUp = new Set<string>();
let requestSeq = 0;
/** Last `gate_enabled` any feedback reply reported (null: unknown / older server). */
let gateEnabled: boolean | null = null;
/** Only users with the evaluation card wait for gate decisions. */
let gateActive = false;

function watchOf(chatId: string): GateWatch {
  let watch = watches.get(chatId);
  if (!watch) {
    watch = { since: new Map(), request: 0, timer: null, deadline: null };
    watches.set(chatId, watch);
  }
  return watch;
}

function clearTimers(watch: GateWatch) {
  if (watch.timer !== null) window.clearTimeout(watch.timer);
  if (watch.deadline !== null) window.clearTimeout(watch.deadline);
  watch.timer = null;
  watch.deadline = null;
}

/** A chat's gating answers, in the order they were first seen. */
function idsOf(watch: GateWatch | undefined): string[] {
  return watch ? [...watch.since.keys()] : [];
}

/** `map` with one chat's previous ids replaced by `next`. */
function withChatIds(map: Record<string, true>, previous: string[], next: string[]): Record<string, true> {
  if (previous.length === next.length && previous.every((id, i) => id === next[i])) return map;
  const out = { ...map };
  for (const id of previous) delete out[id];
  for (const id of next) out[id] = true;
  return out;
}

export const useEvaluationStore = create<EvaluationState>((set, get) => ({
  ...EMPTY,

  loadChat: async (chatId, options) => {
    if (!chatId || (get().loadedChats[chatId] && !options?.force)) return;
    const gen = generation;
    const request = ++requestSeq;
    watchOf(chatId).request = request;
    set((s) => ({ loadedChats: { ...s.loadedChats, [chatId]: true } }));
    try {
      const { records, excluded, notNeeded, gating, gateEnabled: enabled } = await getChatEvaluations(chatId);
      if (gen !== generation) return;
      if (enabled !== null) gateEnabled = enabled;
      const watch = watches.get(chatId);
      // Only the chat's latest reply says what is still gating: an older one could re-hide a card,
      // and a chat left meanwhile (stopGating) is not watched any more.
      const latest = watch?.request === request;
      const previous = idsOf(watch);
      let nextGating = previous;
      if (watch && latest) {
        const now = Date.now();
        const since = new Map<string, number>();
        if (gateActive) {
          for (const id of gating) if (!gaveUp.has(id)) since.set(id, watch.since.get(id) ?? now);
        }
        watch.since = since;
        nextGating = idsOf(watch);
      }
      // The newest snapshot decides the chat's not-needed answers (the gate switched off clears them).
      const priorNotNeeded = notNeededByChat.get(chatId);
      const applyNotNeeded = !priorNotNeeded || request > priorNotNeeded.request;
      if (applyNotNeeded) notNeededByChat.set(chatId, { ids: notNeeded, request });
      // Local submissions made while the request was in flight win over the server snapshot.
      set((s) => ({
        records: { ...records, ...s.records },
        excluded: excluded.some((id) => !s.excluded[id])
          ? { ...s.excluded, ...Object.fromEntries(excluded.map((id) => [id, true as const])) }
          : s.excluded,
        notNeeded: applyNotNeeded ? withChatIds(s.notNeeded, priorNotNeeded?.ids ?? [], notNeeded) : s.notNeeded,
        gating: withChatIds(s.gating, previous, nextGating),
      }));
      if (watch && latest) scheduleGatePoll(chatId);
    } catch {
      if (gen !== generation) return;
      set((s) => {
        const loadedChats = { ...s.loadedChats };
        delete loadedChats[chatId];
        return { loadedChats };
      });
      // A failed read must not leave cards hidden for good: keep polling until the local cap.
      if (watches.get(chatId)?.request === request) scheduleGatePoll(chatId);
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
        if (data.gateEnabled !== null) gateEnabled = data.gateEnabled;
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

  stopGating: (chatId) => {
    const watch = watches.get(chatId);
    if (watch) {
      clearTimers(watch);
      watches.delete(chatId);
    }
    const held = idsOf(watch);
    // Always forget the chat: an answer that finished (or a read that was in flight) while the user
    // was elsewhere must be read again when the chat is opened again.
    set((s) => {
      if (!s.loadedChats[chatId] && !held.length) return s;
      const loadedChats = { ...s.loadedChats };
      delete loadedChats[chatId];
      return { loadedChats, gating: withChatIds(s.gating, held, []) };
    });
  },

  expectGate: (chatId, messageId) => {
    if (!gateActive || gateEnabled !== true || !chatId || !messageId) return;
    if (gaveUp.has(messageId) || get().gating[messageId]) return;
    const watch = watchOf(chatId);
    // A read already in flight predates this answer: it must not clear the hold.
    watch.request = ++requestSeq;
    const previous = idsOf(watch);
    watch.since.set(messageId, Date.now());
    set((s) => ({ gating: withChatIds(s.gating, previous, idsOf(watch)) }));
    // The end-of-run re-read normally answers first; this poll covers a run that ended elsewhere.
    scheduleGatePoll(chatId);
  },

  setGateActive: (active) => {
    if (gateActive === active) return;
    gateActive = active;
    if (active) return;
    // Nothing waits any more: drop every pending wait.
    const held: string[] = [];
    for (const watch of watches.values()) {
      clearTimers(watch);
      held.push(...idsOf(watch));
      watch.since = new Map();
    }
    if (held.length) set((s) => ({ gating: withChatIds(s.gating, held, []) }));
  },

  reset: () => {
    generation += 1;
    pendingRequest = null;
    for (const watch of watches.values()) clearTimers(watch);
    watches.clear();
    notNeededByChat.clear();
    gaveUp.clear();
    gateEnabled = null;
    set({ ...EMPTY });
  },
}));

/** Give up on the chat's answers whose local cap ran out: they show the card (fail open). */
function giveUpExpired(chatId: string) {
  const watch = watches.get(chatId);
  if (!watch) return;
  const now = Date.now();
  const previous = idsOf(watch);
  const expired = previous.filter((id) => (watch.since.get(id) ?? now) + GATE_LOCAL_CAP_MS <= now);
  if (!expired.length) return;
  for (const id of expired) {
    gaveUp.add(id);
    watch.since.delete(id);
  }
  useEvaluationStore.setState((s) => ({ gating: withChatIds(s.gating, previous, idsOf(watch)) }));
}

/** Keep re-reading a chat while it has undecided answers. */
function scheduleGatePoll(chatId: string) {
  giveUpExpired(chatId);
  const watch = watches.get(chatId);
  if (!watch) return;
  clearTimers(watch);
  if (!watch.since.size) return;
  // Each answer's own cap fires even if a request hangs.
  const next = Math.min(...watch.since.values()) + GATE_LOCAL_CAP_MS;
  watch.deadline = window.setTimeout(() => {
    watch.deadline = null;
    if (watches.get(chatId) === watch) scheduleGatePoll(chatId);
  }, Math.max(0, next - Date.now()));
  const gen = generation;
  watch.timer = window.setTimeout(() => {
    watch.timer = null;
    if (gen !== generation || watches.get(chatId) !== watch) return;
    void useEvaluationStore.getState().loadChat(chatId, { force: true });
  }, GATE_POLL_MS);
}
