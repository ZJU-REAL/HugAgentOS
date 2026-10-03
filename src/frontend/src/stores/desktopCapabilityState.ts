import { create } from 'zustand';
import type { DeviceCapabilityKind, DeviceCapabilityItem, DeviceCapabilityListing } from '../api';

export interface DesktopCapabilityClient {
  list: (kind: DeviceCapabilityKind) => Promise<DeviceCapabilityListing>;
}
interface KindState {
  items: DeviceCapabilityItem[];
  byName: Record<string, DeviceCapabilityItem>;
  loaded: boolean;
  discoveryErrors: { folder: string; code: string }[];
}
interface DesktopCapabilityState {
  kinds: Record<DeviceCapabilityKind, KindState>;
  load: (kind: DeviceCapabilityKind, force?: boolean) => Promise<void>;
  reloadAll: () => void;
  reset: () => void;
}
const KINDS: DeviceCapabilityKind[] = ['skill', 'mcp', 'agent', 'plugin'];
const empty = (): KindState => ({ items: [], byName: {}, loaded: false, discoveryErrors: [] });
const emptyKinds = () => ({ skill: empty(), mcp: empty(), agent: empty(), plugin: empty() });
function fromListing(listing: DeviceCapabilityListing): KindState {
  const byName: Record<string, DeviceCapabilityItem> = Object.create(null);
  const groups = new Map<string, DeviceCapabilityItem[]>();
  for (const item of listing.items) {
    // Catalog cards project the current cloud account. A disabled cloud copy
    // still owns its card; an old local/default copy must not supply its actions.
    if (item.source === 'cloud' && (!listing.profile_id
      || !item.install_id.startsWith(item.kind + ':' + listing.profile_id + ':'))) continue;
    const group = groups.get(item.runtime_name) ?? [];
    group.push(item);
    groups.set(item.runtime_name, group);
  }
  for (const [name, items] of groups) {
    const cloud = items.filter((item) => item.source === 'cloud');
    const chosen = items.filter((item) => item.resolution?.outcome === 'chosen');
    const candidates = cloud.length ? cloud : chosen.length ? chosen : items;
    // Multiple same-named installations are ambiguous. Never choose an upload
    // target by response order (especially agents with duplicate display names).
    if (candidates.length === 1) byName[name] = candidates[0];
  }
  return { items: listing.items, byName, loaded: true, discoveryErrors: listing.discovery_errors ?? [] };
}

/** 只读来源清单：卡片上标注这条能力来自本机还是云端。每个账号单独缓存；
 *  刷新不丢请求，已退出账号的迟到响应永远不发布。 */
export function createDesktopCapabilityStore(client: DesktopCapabilityClient, enabled: () => boolean) {
  let epoch = 0;
  const pending = new Map<DeviceCapabilityKind, { promise: Promise<void>; refresh: boolean }>();
  return create<DesktopCapabilityState>((set, get) => ({
    kinds: emptyKinds(),
    reset: () => { epoch += 1; pending.clear(); set({ kinds: emptyKinds() }); },
    reloadAll: () => { KINDS.forEach((kind) => { void get().load(kind, true); }); },
    load: async (kind, force = false) => {
      if (!enabled()) return;
      const currentEpoch = epoch;
      const inFlight = pending.get(kind);
      if (inFlight) {
        if (force) inFlight.refresh = true;
        return inFlight.promise;
      }
      if (!force && get().kinds[kind].loaded) return;
      const entry = { promise: Promise.resolve(), refresh: false };
      // Register before invoking the client, including clients which throw synchronously.
      pending.set(kind, entry);
      entry.promise = (async () => {
        try {
          do {
            entry.refresh = false;
            try {
              const listing = await client.list(kind);
              if (currentEpoch === epoch && enabled()) {
                set((s) => ({ kinds: { ...s.kinds, [kind]: fromListing(listing) } }));
              }
            } catch {
              // Preserve the current labels; a later refresh can retry.
            }
          } while (entry.refresh && currentEpoch === epoch && enabled());
        } finally {
          if (pending.get(kind) === entry) pending.delete(kind);
        }
      })();
      await entry.promise;
    },
  }));
}
