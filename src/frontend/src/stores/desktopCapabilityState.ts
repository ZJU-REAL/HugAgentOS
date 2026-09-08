import { create } from 'zustand';
import type { DeviceCapabilityKind, DeviceCapabilityItem, DeviceCapabilityListing } from '../api';

export interface DesktopCapabilityClient {
  list: (kind: DeviceCapabilityKind) => Promise<DeviceCapabilityListing>;
}
interface KindState {
  items: DeviceCapabilityItem[];
  byName: Record<string, DeviceCapabilityItem>;
  loaded: boolean;
}
interface DesktopCapabilityState {
  kinds: Record<DeviceCapabilityKind, KindState>;
  load: (kind: DeviceCapabilityKind, force?: boolean) => Promise<void>;
  reloadAll: () => void;
  reset: () => void;
}
const KINDS: DeviceCapabilityKind[] = ['skill', 'mcp', 'agent', 'plugin'];
const empty = (): KindState => ({ items: [], byName: {}, loaded: false });
const emptyKinds = () => ({ skill: empty(), mcp: empty(), agent: empty(), plugin: empty() });
function fromListing(listing: DeviceCapabilityListing): KindState {
  const byName: Record<string, DeviceCapabilityItem> = {};
  for (const item of listing.items) byName[item.runtime_name] ||= item;
  return { items: listing.items, byName, loaded: true };
}

/** 只读来源清单：卡片上标注这条能力来自本机还是云端。每个账号单独缓存；
 *  刷新不丢请求，已退出账号的迟到响应永远不发布。 */
export function createDesktopCapabilityStore(client: DesktopCapabilityClient, enabled: () => boolean) {
  let epoch = 0;
  const pending = new Map<DeviceCapabilityKind, Promise<void>>();
  return create<DesktopCapabilityState>((set, get) => ({
    kinds: emptyKinds(),
    reset: () => { epoch += 1; pending.clear(); set({ kinds: emptyKinds() }); },
    reloadAll: () => { KINDS.forEach((kind) => { void get().load(kind, true); }); },
    load: async (kind, force = false) => {
      if (!enabled()) return;
      const currentEpoch = epoch;
      const inFlight = pending.get(kind);
      if (inFlight) {
        await inFlight;
        if (force && currentEpoch === epoch) await get().load(kind, true);
        return;
      }
      if (!force && get().kinds[kind].loaded) return;
      const request = (async () => {
        try {
          const listing = await client.list(kind);
          if (currentEpoch === epoch) set((s) => ({ kinds: { ...s.kinds, [kind]: fromListing(listing) } }));
        } catch {
          // 来源标记拿不到就不标，不打扰用户；下一次同步后会自动补上。
        } finally {
          if (currentEpoch === epoch) pending.delete(kind);
        }
      })();
      pending.set(kind, request);
      await request;
    },
  }));
}
