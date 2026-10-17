import { needsResultCheck } from '../tasks/taskState';
import { t } from '../../../i18n';
import { useEffect } from 'react';
import { useAuthStore } from '../../../stores/authStore';
import { useTaskNotificationStore, normalizeTaskStatus, taskIndicator } from '../../../stores/taskNotificationStore';
import { personalRequest, type PersonalTask } from '../api';

interface PersonalSidebarStatus {tasks:Array<PersonalTask & {result_seq?:number}>;replying:boolean;latest_seq:number;unread_count:number}
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
        const baselining=!store.personalBaselineReady;
        if(baselining)store.establishPersonalBaseline(data.latest_seq);
        store.observe(data.tasks.map(task=>({key:'personal:'+task.id,id:task.id+':'+(task.run_id||'queued'),
          status:task.status!=='failed'&&(task.pending_approval||needsResultCheck(task))?'waiting':normalizeTaskStatus(task.status),
          messageId:task.result_seq?String(task.result_seq):undefined,
          startedAt:0,updatedAt:Date.parse(task.updated_at||'')||0})));
        // Reading while this request is pending invalidates only its unread count.
        if(baselining)store.setPersonal(0,data.replying);
        else if(store.personalReadSeq===after)store.setPersonal(data.unread_count,data.replying);
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
  const notices=useTaskNotificationStore(s=>s.notices);
  const unread=useTaskNotificationStore(s=>s.personalUnread);
  const replying=useTaskNotificationStore(s=>s.personalReplying);
  const indicator=taskIndicator(Object.values(notices).filter(n=>n.key.startsWith('personal:')),replying);
  if((indicator.status==='idle'||(indicator.status==='completed'&&indicator.seen))&&unread>0)return {status:'completed' as const,seen:false,unread};
  // A completed and already-read task should not leave the permanent agent entry lit.
  return {...indicator,status:indicator.status==='completed'&&indicator.seen?'idle' as const:indicator.status,unread};
}

export function usePersonalAgentStatusText(){
  const available=useAuthStore(s=>s.authUser?.can_use_personal_agent===true);
  const state=usePersonalTaskIndicator();
  if(!available)return '';
  const labels={running:t('运行中'),waiting:t('等待你的输入或确认'),failed:t('任务失败'),completed:t('任务已完成，结果未读'),idle:''};
  return [labels[state.status],state.unread>0?t('{count} 条未读消息',{count:state.unread}):''].filter(Boolean).join(' · ');
}
