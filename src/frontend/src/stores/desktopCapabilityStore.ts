import { getDeviceCapabilities, setCapabilitySyncListener } from '../api';
import { useDeploymentModeStore } from './deploymentModeStore';
import { useAuthStore } from './authStore';
import { createDesktopCapabilityStore } from './desktopCapabilityState';

export const useDesktopCapabilityStore = createDesktopCapabilityStore(
  { list: getDeviceCapabilities },
  () => useDeploymentModeStore.getState().provisionMode === 'dual',
);

useAuthStore.subscribe((next, previous) => {
  if (next.authUser?.user_id !== previous.authUser?.user_id) useDesktopCapabilityStore.getState().reset();
});
useDeploymentModeStore.subscribe((next, previous) => {
  if (next.provisionMode !== previous.provisionMode || next.serverBase !== previous.serverBase) {
    useDesktopCapabilityStore.getState().reset();
  }
});
// 云端能力被改动 → api.ts 同步完本机能力后刷新来源标记。
setCapabilitySyncListener(() => useDesktopCapabilityStore.getState().reloadAll());
