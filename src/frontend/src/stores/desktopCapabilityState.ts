import { create } from 'zustand';
import type { DeviceCapabilityKind, DeviceCapabilityItem, DeviceCapabilityListing, DevicePrepareResult, DeviceLocalCopyResult } from '../api';

export interface DesktopCapabilityClient {
  list: (kind: DeviceCapabilityKind) => Promise<DeviceCapabilityListing>;
  sync: () => Promise<Record<string, unknown>>;
  prepare: (body: { install_ids: string[]; sync_first: boolean }) => Promise<DevicePrepareResult[]>;
  copyLocal: (installId: string, runtimeName?: string) => Promise<DeviceLocalCopyResult>;
  setEnabled: (installId: string, enabled: boolean) => Promise<DeviceCapabilityListing>;
  remove: (installId: string) => Promise<void>;
  choose: (kind: DeviceCapabilityKind, name: string, id: string | null) => Promise<DeviceCapabilityListing>;
}
interface KindState {
  listing: DeviceCapabilityListing | null;
  loading: boolean;
  error: string | null;
  byName: Record<string, DeviceCapabilityItem[]>;
  fetchedAt: number;
}
interface DesktopCapabilityState {
  kinds: Record<DeviceCapabilityKind, KindState>;
  busy: Record<string, boolean>;
  syncing: boolean;
  enabled: () => boolean;
  load: (kind: DeviceCapabilityKind, force?: boolean) => Promise<void>;
  refresh: () => Promise<void>;
  syncAll: () => Promise<void>;
  prepare: (kind: DeviceCapabilityKind, ids: string[]) => Promise<void>;
  copyLocal: (kind: DeviceCapabilityKind, id: string, runtimeName?: string) => Promise<void>;
  setEnabled: (kind: DeviceCapabilityKind, id: string, enabled: boolean) => Promise<void>;
  removeFiles: (kind: DeviceCapabilityKind, id: string) => Promise<void>;
  choose: (kind: DeviceCapabilityKind, name: string, id: string | null) => Promise<void>;
  reset: () => void;
}
const KINDS: DeviceCapabilityKind[] = ['skill', 'mcp', 'agent', 'plugin'];
const empty = (): KindState => ({ listing: null, loading: false, error: null, byName: {}, fetchedAt: 0 });
const emptyKinds = () => ({ skill: empty(), mcp: empty(), agent: empty(), plugin: empty() });
function fromListing(listing: DeviceCapabilityListing): KindState {
  const byName: Record<string, DeviceCapabilityItem[]> = {};
  for (const item of listing.items) (byName[item.runtime_name] ||= []).push(item);
  return { listing, byName, loading: false, error: null, fetchedAt: Date.now() };
}

/** 每个账号单独缓存；刷新不丢请求，已退出账号的迟到响应永远不发布。 */
export function createDesktopCapabilityStore(client: DesktopCapabilityClient, enabled: () => boolean) {
  let epoch = 0;
  const pending = new Map<DeviceCapabilityKind, Promise<void>>();
  return create<DesktopCapabilityState>((set, get) => {
    const refreshKinds = () => Promise.all(KINDS.map((kind) => get().load(kind, true))).then(() => {});
    return {
      kinds: emptyKinds(), busy: {}, syncing: false, enabled,
      reset: () => { epoch += 1; pending.clear(); set({ kinds: emptyKinds(), busy: {}, syncing: false }); },
      load: async (kind, force = false) => {
        if (!enabled()) return;
        const currentEpoch = epoch;
        const inFlight = pending.get(kind);
        if (inFlight) {
          await inFlight;
          if (force && currentEpoch === epoch) await get().load(kind, true);
          return;
        }
        const cached = get().kinds[kind];
        if (!force && cached.listing && Date.now() - cached.fetchedAt < 15000) return;
        set((s) => ({ kinds: { ...s.kinds, [kind]: { ...s.kinds[kind], loading: true, error: null } } }));
        const request = (async () => {
          try {
            const listing = await client.list(kind);
            if (currentEpoch === epoch) set((s) => ({ kinds: { ...s.kinds, [kind]: fromListing(listing) } }));
          } catch (error) {
            if (currentEpoch === epoch) set((s) => ({ kinds: { ...s.kinds, [kind]: {
              ...s.kinds[kind], loading: false, error: (error as Error).message,
            } } }));
          } finally {
            if (currentEpoch === epoch) pending.delete(kind);
          }
        })();
        pending.set(kind, request);
        await request;
      },
      refresh: async () => {
        if (!enabled() || get().syncing) return;
        const currentEpoch = epoch;
        set({ syncing: true });
        try { await client.sync(); }
        finally {
          if (currentEpoch === epoch) {
            await refreshKinds();
            set({ syncing: false });
          }
        }
      },
      syncAll: () => get().refresh(),
      prepare: async (_kind, ids) => {
        const currentEpoch = epoch;
        set((s) => ({ busy: { ...s.busy, ...Object.fromEntries(ids.map((id) => [id, true])) } }));
        try {
          const results = await client.prepare({ install_ids: ids, sync_first: true });
          const failed = results.filter((r) => !r.ok);
          if (failed.length) throw new Error(failed.map((r) => r.error?.message || r.error?.code || r.install_id).join('；'));
        } finally {
          if (currentEpoch === epoch) {
            set((s) => { const busy = { ...s.busy }; ids.forEach((id) => delete busy[id]); return { busy }; });
            await refreshKinds();
          }
        }
      },
      copyLocal: async (_kind, id, runtimeName) => {
        if (get().busy[id]) return;
        const currentEpoch = epoch;
        set((s) => ({ busy: { ...s.busy, [id]: true } }));
        try { await client.copyLocal(id, runtimeName); }
        finally {
          if (currentEpoch === epoch) {
            set((s) => { const busy = { ...s.busy }; delete busy[id]; return { busy }; });
            await refreshKinds();
          }
        }
      },
      setEnabled: async (kind, id, enabled) => {
        if (get().busy[id]) return;
        const currentEpoch = epoch;
        set((s) => ({ busy: { ...s.busy, [id]: true } }));
        try {
          const listing = await client.setEnabled(id, enabled);
          if (currentEpoch === epoch) {
            set((s) => ({ kinds: { ...s.kinds, [kind]: fromListing(listing) } }));
            await refreshKinds();
          }
        } finally {
          if (currentEpoch === epoch) set((s) => {
            const busy = { ...s.busy }; delete busy[id]; return { busy };
          });
        }
      },
      removeFiles: async (_kind, id) => {
        const currentEpoch = epoch;
        await client.remove(id);
        if (currentEpoch === epoch) await refreshKinds();
      },
      choose: async (kind, name, id) => {
        const currentEpoch = epoch;
        const listing = await client.choose(kind, name, id);
        if (currentEpoch === epoch) set((s) => ({ kinds: { ...s.kinds, [kind]: fromListing(listing) } }));
      },
    };
  });
}
