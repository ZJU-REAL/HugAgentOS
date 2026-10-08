import { create } from 'zustand';

/** Transient launch page; navigation stays bound to the mounted module. */
type Navigate = (url: string) => Promise<void>;
interface LauncherState {
  open: boolean;
  navigation: { tabId: string; navigate: Navigate } | null;
  show: () => void;
  dismiss: () => void;
  bindNavigation: (tabId: string, navigate: Navigate | null) => void;
}
export const useCanvasLauncherStore = create<LauncherState>((set) => ({
  open: false, navigation: null,
  show: () => set({ open: true }),
  dismiss: () => set({ open: false }),
  bindNavigation: (tabId, navigate) => set(state => navigate
    ? { navigation: { tabId, navigate } }
    : state.navigation?.tabId === tabId ? { navigation: null } : {}),
}));
