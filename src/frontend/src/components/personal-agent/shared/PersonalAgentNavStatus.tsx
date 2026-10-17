import { useAuthStore } from '../../../stores/authStore';
import { t } from '../../../i18n';
import { TaskStatusDot } from '../../sidebar/TaskStatusDot';
import { usePersonalTaskIndicator } from './notifications';
export function PersonalAgentNavStatus({compact=false}:{compact?:boolean}){
  const state=usePersonalTaskIndicator();
  const available=useAuthStore(s=>s.authUser?.can_use_personal_agent===true);
  if (!available) return null;
  return <span className={'jx-personalStatus'+(compact?' jx-personalStatus--compact':'')}><TaskStatusDot status={state.status} seen={state.seen}/>
    {state.unread>0&&<span className="jx-personalUnread" aria-label={t('{count} 条未读消息',{count:state.unread})}>{state.unread>99?'99+':state.unread}</span>}
  </span>;
}
