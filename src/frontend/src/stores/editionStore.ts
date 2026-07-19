import { create } from 'zustand';
import { getEditionInfo } from '../api';
import { IS_COMMUNITY_EDITION_BUILD } from '../edition';

const BUILD_EDITION = IS_COMMUNITY_EDITION_BUILD
  ? 'ce'
  : ((import.meta.env.VITE_EDITION as string) || 'ee').trim().toLowerCase();

/**
 * Frontend consumer of the Edition / license probe (/v1/meta/edition).
 *
 * Before the probe returns, allow through optimistically (features treated as all true)——so the UI does not flash-hide under an EE deployment;
 * after it returns, tighten by feature flags: CE all false, EE per license entitlement.
 * Components read feature flags via a reactive selector:
 *   useEditionStore((s) => (s.loaded ? !!s.features.multi_tenancy : true))
 */
interface EditionState {
  /** Deployment edition: ce (community) / ee (commercial). */
  edition: string;
  /** license state machine: internal / licensed / grace / expired / invalid / missing / ce. */
  mode: string;
  /** Feature-flag boolean map (multi_tenancy / audit / billing …). */
  features: Record<string, boolean>;
  loaded: boolean;
  fetching: boolean;
  fetchEdition: () => Promise<void>;
}

export const useEditionStore = create<EditionState>((set, get) => ({
  edition: BUILD_EDITION,
  mode: BUILD_EDITION === 'ce' ? 'ce' : 'internal',
  features: {},
  loaded: false,
  fetching: false,
  fetchEdition: async () => {
    if (get().fetching || get().loaded) return;
    set({ fetching: true });
    try {
      const info = await getEditionInfo();
      set({ edition: info.edition, mode: info.mode, features: info.features, loaded: true });
    } catch {
      // Silent failure: keep the optimistic default (full UI); backend gating still backstops it
    } finally {
      set({ fetching: false });
    }
  },
}));
