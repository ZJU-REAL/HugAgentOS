import { create } from 'zustand';

import { setCapabilityDriftListener, syncDeviceCapabilities } from '../api';
import { useAuthStore } from './authStore';
import { useCatalogStore } from './catalogStore';
import { useDeploymentModeStore } from './deploymentModeStore';
import { useDesktopCapabilityStore } from './desktopCapabilityStore';

/**
 * 云端能力改动的**待同步**状态。
 *
 * 检测与执行是分开的：云端换了能力变更号只会让这里的 `pending` 变成 true，界面在左侧
 * 边栏最下方亮出一个入口，和桌面端「下载更新」是同一个位置、同一套交互。真正把云端能力
 * 落到本机，只在用户点那一下时发生——不自动同步，也不定时轮询。
 */
interface CapabilitySyncState {
  pending: boolean;
  busy: boolean;
  error: string | null;
  run: () => Promise<void>;
  reset: () => void;
}

export const useCapabilitySyncStore = create<CapabilitySyncState>((set, get) => ({
  pending: false,
  busy: false,
  error: null,
  reset: () => set({ pending: false, busy: false, error: null }),
  run: async () => {
    if (get().busy) return;
    set({ busy: true, error: null });
    try {
      await syncDeviceCapabilities();
      useDesktopCapabilityStore.getState().reloadAll();
      // 同步落的就是能力中心读的那份登记表，拉完得让界面跟上，否则用户点完没反应。
      void useCatalogStore.getState().fetchCatalog();
      set({ pending: false, busy: false });
    } catch (error) {
      set({ busy: false, error: error instanceof Error ? error.message : String(error) });
    }
  },
}));

setCapabilityDriftListener(() => {
  if (useDeploymentModeStore.getState().provisionMode !== 'dual') return;
  useCapabilitySyncStore.setState({ pending: true });
});

// 换账号后上一个账号的待同步提示不再适用。
useAuthStore.subscribe((next, previous) => {
  if (next.authUser?.user_id !== previous.authUser?.user_id) useCapabilitySyncStore.getState().reset();
});
