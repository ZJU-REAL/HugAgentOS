import { create } from 'zustand';
import { loadJsonPref, saveJsonPref } from '../storage';
import { usePageConfigStore } from './pageConfigStore';

/** The user's own page setting; absent until they change it, so the administrator's default applies. */
const USER_PREF_KEY = 'jx_run_monitor_open_by_default';

function loadUserPref(): boolean | null {
  const { enabled } = loadJsonPref<{ enabled?: unknown }>(USER_PREF_KEY, {});
  return typeof enabled === 'boolean' ? enabled : null;
}

/** Config →「页面配置」→「会话设置」. */
export function adminRunMonitorDefault(): boolean {
  return usePageConfigStore.getState().config.chat.run_monitor_open_by_default;
}

interface RunMonitorState {
  /** Whether the rail is expanded in the current chat. */
  open: boolean;
  /** The user's own choice, or null to follow the administrator's default. */
  userPref: boolean | null;
  toggle: () => void;
  setUserPref: (enabled: boolean) => void;
  /** Called on chat change (and when the default changes): each chat starts from the effective default. */
  resetForChat: () => void;
}

const initialPref = loadUserPref();

export const useRunMonitorStore = create<RunMonitorState>((set, get) => ({
  open: initialPref ?? adminRunMonitorDefault(),
  userPref: initialPref,
  toggle: () => set((state) => ({ open: !state.open })),
  setUserPref: (enabled) => {
    saveJsonPref(USER_PREF_KEY, { enabled });
    set({ userPref: enabled, open: enabled });
  },
  resetForChat: () => set({ open: get().userPref ?? adminRunMonitorDefault() }),
}));

/** Effective default for the current user: their own setting, else the administrator's. */
export function useRunMonitorOpenByDefault(): boolean {
  const userPref = useRunMonitorStore((s) => s.userPref);
  const adminDefault = usePageConfigStore((s) => s.config.chat.run_monitor_open_by_default);
  return userPref ?? adminDefault;
}
