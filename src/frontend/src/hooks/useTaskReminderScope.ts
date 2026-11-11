import { useEffect } from 'react';
import { useAuthStore } from '../stores/authStore';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { configureReadSync, useTaskNotificationStore } from '../stores/taskNotificationStore';
import { getApiUrl } from '../api';
import { markChatRead, markPersonalRead } from '../sidebarStatusApi';

// Read acknowledgements are stored by the server that owns the chat, so every client
// (desktop, web, another computer) agrees; localStorage only caches them.
configureReadSync({ chat: markChatRead, personal: markPersonalRead });

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
