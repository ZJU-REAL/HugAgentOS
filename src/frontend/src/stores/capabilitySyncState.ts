import { create } from 'zustand';

interface CapabilitySyncState {
  pending: boolean;
  busy: boolean;
  error: string | null;
  check: (force?: boolean) => Promise<void>;
  run: () => Promise<void>;
  reset: () => void;
}

interface SyncApi {
  check: () => Promise<{ changed: boolean | null }>;
  sync: () => Promise<unknown>;
  afterSync: () => void;
}

/** Activity-driven checks, never an idle timer. Global epochs are only hints. */
export function createCapabilitySyncStore(
  api: SyncApi,
  enabled: () => boolean,
  now: () => number = Date.now,
) {
  let generation = 0;
  let lastCheck = -Infinity;
  let checking: Promise<void> | null = null;
  return create<CapabilitySyncState>((set, get) => ({
    pending: false,
    busy: false,
    error: null,
    reset: () => {
      generation++;
      checking = null;
      lastCheck = -Infinity;
      set({ pending: false, busy: false, error: null });
    },
    check: async (force = false) => {
      if (!enabled() || get().busy) return;
      if (checking) return checking;
      if (!force && now() - lastCheck < 60_000) return;
      const ticket = generation;
      lastCheck = now();
      const request = (async () => {
        try {
          const result = await api.check();
          if (ticket !== generation || !enabled()) return;
          // Unavailable is not evidence of an update (or of successful sync).
          if (typeof result.changed === 'boolean') set({ pending: result.changed });
        } catch {
          // Background advisory checks must not interrupt ordinary work.
        }
      })();
      checking = request;
      try { await request; }
      finally { if (ticket === generation) checking = null; }
    },
    run: async () => {
      if (!enabled() || get().busy) return;
      const ticket = ++generation; // Invalidate any pre-sync comparison.
      checking = null;
      set({ busy: true, error: null });
      try {
        await api.sync();
        if (ticket !== generation || !enabled()) return;
        api.afterSync();
        set({ pending: false, busy: false });
        // A second change during synchronization must not be lost.
        await get().check(true);
      } catch (error) {
        if (ticket !== generation || !enabled()) return;
        set({ busy: false, error: error instanceof Error ? error.message : String(error) });
      }
    },
  }));
}
