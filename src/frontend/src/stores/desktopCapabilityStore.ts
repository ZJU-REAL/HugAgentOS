import {
  getDeviceCapabilities, syncDeviceCapabilities, prepareDeviceCapabilities,
  removeDeviceCapabilityFiles, setDeviceNamePreference, createDeviceLocalCopy, setDeviceCapabilityEnabled,
} from '../api';
import { useDeploymentModeStore } from './deploymentModeStore';
import { useAuthStore } from './authStore';
import { createDesktopCapabilityStore } from './desktopCapabilityState';

export const useDesktopCapabilityStore = createDesktopCapabilityStore({
  list: getDeviceCapabilities, sync: syncDeviceCapabilities, prepare: prepareDeviceCapabilities,
  setEnabled: setDeviceCapabilityEnabled, copyLocal: createDeviceLocalCopy, remove: removeDeviceCapabilityFiles, choose: setDeviceNamePreference,
}, () => useDeploymentModeStore.getState().provisionMode === 'dual');

useAuthStore.subscribe((next, previous) => {
  if (next.authUser?.user_id !== previous.authUser?.user_id) useDesktopCapabilityStore.getState().reset();
});
useDeploymentModeStore.subscribe((next, previous) => {
  if (next.provisionMode !== previous.provisionMode || next.serverBase !== previous.serverBase) {
    useDesktopCapabilityStore.getState().reset();
  }
});
