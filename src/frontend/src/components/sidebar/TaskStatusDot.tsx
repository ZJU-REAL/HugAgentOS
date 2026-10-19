import { Tooltip } from 'antd';
import { t } from '../../i18n';
import type { TaskStatus } from '../../stores/taskNotificationStore';
import '../../styles/task-status.css';

function taskStatusLabel(status: TaskStatus, seen = false): string {
  if (status === 'running') return t('运行中');
  if (status === 'waiting') return t('等待你的输入或确认');
  if (status === 'failed') return t('任务失败');
  if (status === 'completed') return seen ? t('任务已完成') : t('任务已完成，结果未读');
  return '';
}
export function TaskStatusDot({status,seen=false}: {status:TaskStatus;seen?:boolean}) {
  if (status === 'idle' || ((status === 'completed' || status === 'failed') && seen)) return null;
  const label=taskStatusLabel(status,seen);
  return <Tooltip title={label}><span className={'jx-taskStatus jx-taskStatus--'+status+(seen?' jx-taskStatus--seen':'')}
    role="img" aria-label={label} data-task-status={status} data-task-unread={!seen} >
    {status === 'running' && (
      <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
        <g className="jx-taskSpinnerMotion">
          <circle className="jx-taskSpinnerTrack" cx="12" cy="12" r="9.5" />
          <circle className="jx-taskSpinnerArc" cx="12" cy="12" r="9.5" />
        </g>
      </svg>
    )}
  </span></Tooltip>;
}
