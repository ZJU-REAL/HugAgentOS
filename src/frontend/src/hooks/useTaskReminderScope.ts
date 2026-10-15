import { useEffect } from 'react';
import { useAuthStore } from '../stores/authStore';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { useTaskNotificationStore } from '../stores/taskNotificationStore';
import { getApiUrl } from '../api';

export function useTaskReminderScope() {
  const userId=useAuthStore(s=>s.authUser?.user_id);
  const checking=useAuthStore(s=>s.authChecking);
  const server=useDeploymentModeStore(s=>s.serverBase);
  const mode=useDeploymentModeStore(s=>s.provisionMode);
  useEffect(()=>{
    const scope=!checking&&userId?JSON.stringify([server||getApiUrl(),mode,userId]):'';
    useTaskNotificationStore.getState().hydrate(scope);
  },[userId,checking,server,mode]);
}
