import { create } from 'zustand';
import { getSidebarLayout, type SidebarLayout } from '../utils/sidebarLayout';

interface State {
  userId: string;
  layout: SidebarLayout;
  status: 'idle' | 'loading' | 'ready' | 'error';
  load: (userId: string) => Promise<void>;
  reset: () => void;
}
let generation = 0;
export const useSidebarLayoutStore = create<State>((set, get) => ({
  userId: '', layout: 'modern', status: 'idle',
  reset: () => {
    ++generation;
    set({ userId: '', layout: 'modern', status: 'idle' });
  },
  load: async userId => {
    if (get().userId === userId && ['loading', 'ready'].includes(get().status)) return;
    const request = ++generation;
    set({ userId, layout: 'modern', status: 'loading' });
    try {
      const layout = await getSidebarLayout(userId);
      if (request === generation) set({ layout, status: 'ready' });
    } catch {
      if (request === generation) set({ status: 'error' });
    }
  },
}));
