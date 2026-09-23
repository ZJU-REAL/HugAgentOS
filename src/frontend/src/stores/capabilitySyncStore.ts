import { checkDeviceCapabilitySync, setCapabilityCheckListener, syncDeviceCapabilities } from '../api';
import { useAuthStore } from './authStore';
import { useCatalogStore } from './catalogStore';
import { useDeploymentModeStore } from './deploymentModeStore';
import { useDesktopCapabilityStore } from './desktopCapabilityStore';
import { createCapabilitySyncStore } from './capabilitySyncState';

/** Show a sync entry only after comparing this account's cloud and local snapshots. */
export const useCapabilitySyncStore = createCapabilitySyncStore({
  check: checkDeviceCapabilitySync,
  sync: syncDeviceCapabilities,
  afterSync: () => {
    useDesktopCapabilityStore.getState().reloadAll();
    void useCatalogStore.getState().fetchCatalog();
  },
}, () => {
  const mode = useDeploymentModeStore.getState();
  return mode.provisionMode === 'dual' && mode.capabilitiesReady
    && !!useAuthStore.getState().authUser;
});

setCapabilityCheckListener(() => { void useCapabilitySyncStore.getState().check(); });

useAuthStore.subscribe((next, previous) => {
  if (next.authUser?.user_id !== previous.authUser?.user_id) {
    useCapabilitySyncStore.getState().reset();
    void useCapabilitySyncStore.getState().check();
  }
});
useDeploymentModeStore.subscribe((next, previous) => {
  if (next.provisionMode !== previous.provisionMode || next.serverBase !== previous.serverBase
    || next.capabilitiesReady !== previous.capabilitiesReady) {
    useCapabilitySyncStore.getState().reset();
    void useCapabilitySyncStore.getState().check();
  }
});
