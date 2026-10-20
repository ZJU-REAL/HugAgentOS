import { create } from 'zustand';

/** Transient launch page; navigation stays bound to the mounted module. */
type Navigate = (url: string) => Promise<void>;
interface LauncherState {
  open: boolean;
  pane: 'primary' | 'secondary';
  navigations: Record<string, Navigate>;
  show: (pane?: 'primary' | 'secondary') => void;
  dismiss: () => void;
  bindNavigation: (tabId: string, navigate: Navigate | null) => void;
}
export const useCanvasLauncherStore = create<LauncherState>((set) => ({
  open: false, pane: 'primary', navigations: {},
  show: (pane = 'primary') => set({ open: true, pane }),
  dismiss: () => set({ open: false }),
  bindNavigation: (tabId, navigate) => set(state => {
    const navigations = { ...state.navigations };
    if (navigate) navigations[tabId] = navigate;
    else delete navigations[tabId];
    return { navigations };
  }),
}));
