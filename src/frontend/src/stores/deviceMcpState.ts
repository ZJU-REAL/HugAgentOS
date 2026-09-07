import { createStore } from 'zustand/vanilla';
import type { DeviceMcpJson } from '../api';

interface DeviceMcpClient {
  read: () => Promise<DeviceMcpJson>;
  refresh: () => Promise<void>;
}
interface DeviceMcpState {
  doc: DeviceMcpJson | null;
  error: unknown;
  busy: boolean;
  load: () => Promise<void>;
  mutate: (action: () => Promise<DeviceMcpJson>) => Promise<boolean>;
  reset: () => void;
}

/** One mounted account owns this state; both identity and request epoch gate writes. */
export function createDeviceMcpStore(client: DeviceMcpClient, active: () => boolean) {
  let epoch = 0;
  const current = (expected: number) => expected === epoch && active();
  return createStore<DeviceMcpState>((set, get) => ({
    doc: null, error: null, busy: false,
    reset: () => { epoch += 1; set({ doc: null, error: null, busy: false }); },
    load: async () => {
      if (!active()) return;
      const expected = ++epoch;
      set({ busy: true });
      try {
        const doc = await client.read();
        if (current(expected)) set({ doc, error: null });
      } catch (error) { if (current(expected)) set({ doc: null, error }); }
      finally { if (current(expected)) set({ busy: false }); }
    },
    mutate: async (action) => {
      if (!active() || get().busy) return false;
      const expected = ++epoch;
      set({ busy: true });
      try {
        const doc = await action();
        if (!current(expected)) return false;
        set({ doc, error: null });
        await client.refresh();
        return current(expected);
      } catch (error) {
        if (!current(expected)) return false;
        // Retain the baseline document and form version after a conflict.
        set({ error });
        throw error;
      } finally { if (current(expected)) set({ busy: false }); }
    },
  }));
}
