import { useChatStore } from '../stores/chatStore';
import { useTaskNotificationStore } from '../stores/taskNotificationStore';

/** The final stream frame can precede the journal commit by a few milliseconds. */
export async function refreshTaskVerdict(chatId:string,runId:string) {
  const owner=useChatStore.getState().currentUserId;
  const scope=useTaskNotificationStore.getState().scope;
  for(let attempt=0;attempt<3;attempt++) {
    if(useChatStore.getState().currentUserId!==owner || useTaskNotificationStore.getState().scope!==scope)return;
    try { await useChatStore.getState().refreshRemoteRunningChats(); }
    catch { return; } // Normal foreground polling retries network failures.
    const notice=useTaskNotificationStore.getState().notices['chat:'+chatId];
    if(!runId || (notice?.id===runId && notice.status!=='running'))return;
    if(attempt<2)await new Promise(resolve=>setTimeout(resolve,500));
  }
}
