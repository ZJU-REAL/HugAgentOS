import { create } from 'zustand';

interface DesktopOverlayState {
  activeOverlay: string | null;
  openOverlay: (overlay: string) => void;
  closeOverlay: (overlay?: string) => void;
  toggleOverlay: (overlay: string) => void;
}

export const useDesktopOverlayStore = create<DesktopOverlayState>((set) => ({
  activeOverlay: null,
  openOverlay: (overlay) => set({ activeOverlay: overlay }),
  closeOverlay: (overlay) => set((state) => (
    overlay === undefined || state.activeOverlay === overlay
      ? { activeOverlay: null }
      : state
  )),
  toggleOverlay: (overlay) => set((state) => ({
    activeOverlay: state.activeOverlay === overlay ? null : overlay,
  })),
}));
