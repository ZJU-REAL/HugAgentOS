import { getDeviceCapabilities } from '../api';
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
  } else if (next.capabilitiesReady && !previous.capabilitiesReady) {
    useDesktopCapabilityStore.getState().reloadAll();
  }
});
