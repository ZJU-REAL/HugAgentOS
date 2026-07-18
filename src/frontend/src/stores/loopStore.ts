import { create } from 'zustand';
import type { LoopItem } from '../types';
import { listLoops, getLoop } from '../api';

/** One requirement of an in-conversation autonomous loop (for plan-bar display). */
export interface LoopPlanReq {
  id: string;
  description: string;
  passes: boolean;
}

/** Conversation-mode "live plan bar" state — bound to the session that initiated the loop. */
export interface LoopLivePlan {
  chatId: string;
  loopId?: string;       // The bound loop_id — used on "continue" to resume the same loop from the breakpoint
  objective: string;
  requirements: LoopPlanReq[];
  currentId: string | null;
  progress: string;      // In the form "2/6"
  status: string | null; // After running ends: completed/budget_exhausted/cancelled/...
  reviewing?: boolean;   // The read-only review sub-agent is verifying/re-checking the current requirement (driver-triggered, maker≠checker)
}

interface LoopState {
  loops: LoopItem[];
  loading: boolean;
  selectedId: string | null;
  setSelectedId: (id: string | null) => void;
  fetchLoops: () => Promise<void>;
  refreshOne: (loopId: string) => Promise<void>;
  upsert: (loop: LoopItem) => void;

  // ── In-conversation live plan bar ──
  livePlan: LoopLivePlan | null;
  startLivePlan: (chatId: string, objective: string) => void;
  setLiveLoopId: (loopId: string) => void;
  setLivePlanReqs: (reqs: LoopPlanReq[], objective?: string) => void;
  setLiveCurrent: (id: string | null, progress?: string) => void;
  setLiveReviewing: (on: boolean, id: string | null, secondPass: boolean) => void;
  markLivePassed: (id: string, progress?: string) => void;
  reviveLivePlan: () => void;              // On "continue", revive the terminal-state plan bar back to running
  finishLivePlan: (status: string) => void;
  clearLivePlan: (chatId?: string) => void;
}

// livePlan is persisted to localStorage — after refreshing the page while a loop is running, the requirement bar (plan bar) can be restored immediately,
// without waiting for SSE replay (during long loops the loop_plan event may already have been evicted by the Redis stream maxlen).
const LIVE_PLAN_KEY = 'hugagent_live_loop_plan';
function loadLivePlan(): LoopLivePlan | null {
  try {
    const raw = localStorage.getItem(LIVE_PLAN_KEY);
    const obj = raw ? JSON.parse(raw) : null;
    return obj && typeof obj === 'object' && obj.chatId ? (obj as LoopLivePlan) : null;
  } catch {
    return null;
  }
}
function saveLivePlan(p: LoopLivePlan | null) {
  try {
    if (p) localStorage.setItem(LIVE_PLAN_KEY, JSON.stringify(p));
    else localStorage.removeItem(LIVE_PLAN_KEY);
  } catch {
    /* ignore quota / privacy-mode errors */
  }
}

export const useLoopStore = create<LoopState>((set, get) => ({
  loops: [],
  loading: false,
  selectedId: null,
  setSelectedId: (id) => set({ selectedId: id }),
  fetchLoops: async () => {
    set({ loading: true });
    try {
      const loops = await listLoops();
      set({ loops });
    } finally {
      set({ loading: false });
    }
  },
  refreshOne: async (loopId) => {
    try {
      const loop = await getLoop(loopId);
      get().upsert(loop);
    } catch {
      /* ignore */
    }
  },
  upsert: (loop) =>
    set((s) => {
      const idx = s.loops.findIndex((x) => x.loop_id === loop.loop_id);
      if (idx === -1) return { loops: [loop, ...s.loops] };
      const next = s.loops.slice();
      next[idx] = loop;
      return { loops: next };
    }),

  // ── In-conversation live plan bar (persisted, restorable on refresh) ──
  livePlan: loadLivePlan(),
  startLivePlan: (chatId, objective) => {
    const next: LoopLivePlan = { chatId, objective, requirements: [], currentId: null, progress: '', status: 'running' };
    saveLivePlan(next);
    set({ livePlan: next });
  },
  setLiveLoopId: (loopId) =>
    set((s) => {
      if (!s.livePlan || s.livePlan.loopId === loopId) return s;
      const next = { ...s.livePlan, loopId };
      saveLivePlan(next);
      return { livePlan: next };
    }),
  setLivePlanReqs: (reqs, objective) =>
    set((s) => {
      if (!s.livePlan) return s;
      const total = reqs.length;
      const done = reqs.filter((r) => r.passes).length;
      const next = {
        ...s.livePlan,
        requirements: reqs,
        objective: objective || s.livePlan.objective,
        progress: total ? `${done}/${total}` : s.livePlan.progress,
      };
      saveLivePlan(next);
      return { livePlan: next };
    }),
  setLiveCurrent: (id, progress) =>
    set((s) => {
      if (!s.livePlan) return s;
      const next = { ...s.livePlan, currentId: id, progress: progress ?? s.livePlan.progress };
      saveLivePlan(next);
      return { livePlan: next };
    }),
  setLiveReviewing: (on, id, _secondPass) =>
    set((s) => {
      if (!s.livePlan) return s;
      // Only update when the event corresponds to the current requirement (avoid stale events misplacing state); during verification currentId is also aligned.
      const next = {
        ...s.livePlan,
        reviewing: on,
        currentId: id ?? s.livePlan.currentId,
      };
      saveLivePlan(next);
      return { livePlan: next };
    }),
  markLivePassed: (id, progress) =>
    set((s) => {
      if (!s.livePlan) return s;
      const requirements = s.livePlan.requirements.map((r) => (r.id === id ? { ...r, passes: true } : r));
      const next = { ...s.livePlan, requirements, progress: progress ?? s.livePlan.progress, currentId: s.livePlan.currentId === id ? null : s.livePlan.currentId };
      saveLivePlan(next);
      return { livePlan: next };
    }),
  reviveLivePlan: () =>
    set((s) => {
      if (!s.livePlan) return s;
      const next = { ...s.livePlan, status: 'running' };
      saveLivePlan(next);
      return { livePlan: next };
    }),
  finishLivePlan: (status) =>
    set((s) => {
      if (!s.livePlan) return s;
      const next = { ...s.livePlan, status, currentId: null };
      saveLivePlan(next);
      return { livePlan: next };
    }),
  clearLivePlan: (chatId) =>
    set((s) => {
      if (!s.livePlan) return s;
      if (chatId && s.livePlan.chatId !== chatId) return s;
      saveLivePlan(null);
      return { livePlan: null };
    }),
}));
