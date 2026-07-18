import { create } from 'zustand';
import { listInstalledPlugins } from '../api';
import type { InstalledPluginItem } from '../types';

/**
 * Shared state for installed plugins. The chat input box (the "+" menu / "/" slash popup) reads
 * from here, and the capability center calls ``fetchInstalled(true)`` after install/uninstall to
 * force a refresh, keeping both places instantly in sync -- avoiding the case where the input box
 * only fetches once on mount and newly installed plugins don't show up.
 */
interface PluginState {
  installed: InstalledPluginItem[];
  loaded: boolean;
  fetchInstalled: (force?: boolean) => Promise<void>;
}

export const usePluginStore = create<PluginState>((set, get) => ({
  installed: [],
  loaded: false,
  fetchInstalled: async (force = false) => {
    if (get().loaded && !force) return;
    try {
      const items = await listInstalledPlugins();
      set({ installed: items, loaded: true });
    } catch {
      /* load failure is non-fatal: the menu/popup just misses the plugin items */
    }
  },
}));
