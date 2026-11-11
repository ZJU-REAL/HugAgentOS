import { needsResultCheck } from '../tasks/taskState';
import { t } from '../../../i18n';
import { useEffect } from 'react';
import { useAuthStore } from '../../../stores/authStore';
import { useTaskNotificationStore, normalizeTaskStatus } from '../../../stores/taskNotificationStore';
import { personalRequest, type PersonalTask } from '../api';

interface PersonalSidebarStatus {tasks:Array<PersonalTask & {result_seq?:number}>;replying:boolean;latest_seq:number;unread_count:number;
  /** Server read position shared by every client; absent on backends that do not track it. */
  read_seq?:number}
export function usePersonalAgentNotifications() {
  const available=useAuthStore(s=>s.authUser?.can_use_personal_agent===true);
  const scope=useTaskNotificationStore(s=>s.scope);
  useEffect(()=>{
    if(!available||!scope)return;
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;let inflight=false;
    const refresh=async()=>{
      if(inflight||document.visibilityState!=='visible')return;
      inflight=true;const after=useTaskNotificationStore.getState().personalReadSeq;
      try{
        const data=await personalRequest<PersonalSidebarStatus>('/v1/personal-agent/sidebar-status?after_seq='+after,{signal:controller.signal});
        const store=useTaskNotificationStore.getState();
        if(controller.signal.aborted||store.scope!==scope)return;
        const serverRead=Number.isSafeInteger(data.read_seq)?data.read_seq as number:null;
        // A backend without server read state falls back to a one-time local baseline.
        const baselining=serverRead===null&&!store.personalBaselineReady;
        if(serverRead!==null)store.applyPersonalServerRead(serverRead);
        else if(baselining)store.establishPersonalBaseline(data.latest_seq);
        const readSeq=useTaskNotificationStore.getState().personalReadSeq;
        store.observe(data.tasks.map(task=>({key:'personal:'+task.id,id:task.id+':'+(task.run_id||'queued'),
          status:task.status!=='failed'&&(task.pending_approval||needsResultCheck(task))?'waiting':normalizeTaskStatus(task.status),
          messageId:task.result_seq?String(task.result_seq):undefined,
          read:serverRead!==null&&task.result_seq?task.result_seq<=readSeq:undefined,
          startedAt:0,updatedAt:Date.parse(task.updated_at||'')||0})));
        // The server counted from max(after, its own position). Reading while this
        // request was pending moves the local position on and invalidates the count.
        if(baselining)store.setPersonal(0,data.replying);
        else if(useTaskNotificationStore.getState().personalReadSeq===Math.max(after,serverRead??0))store.setPersonal(data.unread_count,data.replying);
      }catch{ /* Keep existing reminders across network failures; retry on the next tick. */ }
      finally{inflight=false;}
    };
    const tick=async()=>{await refresh();if(!controller.signal.aborted)timer=setTimeout(()=>void tick(),10_000);};
    const wake=()=>void refresh();void tick();window.addEventListener('focus',wake);document.addEventListener('visibilitychange',wake);
    window.addEventListener('hugagent:personal-agent-updated',wake);
    return()=>{controller.abort();clearTimeout(timer);window.removeEventListener('focus',wake);
      document.removeEventListener('visibilitychange',wake);window.removeEventListener('hugagent:personal-agent-updated',wake);};
  },[available,scope]);
}
export function usePersonalTaskIndicator(){
  // The permanent agent entry lights up only for unread messages; task progress stays in the panel.
  const unread=useTaskNotificationStore(s=>s.personalUnread);
  return {lit:unread>0,unread};
}

export function usePersonalAgentStatusText(){
  const available=useAuthStore(s=>s.authUser?.can_use_personal_agent===true);
  const state=usePersonalTaskIndicator();
  if(!available||!state.lit)return '';
  return t('{count} 条未读消息',{count:state.unread});
}
