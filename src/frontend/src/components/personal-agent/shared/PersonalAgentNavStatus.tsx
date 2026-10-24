import { useAuthStore } from '../../../stores/authStore';
import { t } from '../../../i18n';
import '../../../styles/task-status.css';
import { usePersonalTaskIndicator } from './notifications';
export function PersonalAgentNavStatus({compact=false}:{compact?:boolean}){
  const state=usePersonalTaskIndicator();
  const available=useAuthStore(s=>s.authUser?.can_use_personal_agent===true);
  if (!available||!state.lit) return null;
  return <span className={'jx-personalStatus'+(compact?' jx-personalStatus--compact':'')}>
    <span className="jx-taskStatus jx-taskStatus--completed" aria-hidden="true" />
    <span className="jx-personalUnread" aria-label={t('{count} 条未读消息',{count:state.unread})}>{state.unread>99?'99+':state.unread}</span>
  </span>;
}
