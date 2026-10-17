import { Button } from 'antd';
import { ExclamationCircleOutlined } from '@ant-design/icons';
import { TaskActions } from './TaskActions';
import { needsUser, taskTitle } from './taskState';
import { type PersonalTask } from '../api';
import { t } from '../../../i18n';

const DAY = 24 * 3600 * 1000;

/** Tasks that wait for the user, shown in the main conversation and not only in the monitor. */
export function AttentionBar({ tasks, now, onDone, onError, onOpen }: { tasks: PersonalTask[]; now: number; onDone: () => Promise<void>; onError: (exc: unknown) => void; onOpen: (task: PersonalTask) => void }) {
  // Pending approvals have their own confirmation bar above the composer.
  const items = tasks.filter(task => !task.pending_approval && needsUser(task) && (task.status !== 'failed' || now - Date.parse(task.updated_at || '') < DAY));
  if (!items.length) return null;
  return <section className="pa-attention" aria-label={t('需要你处理')} data-testid="pa-attention">
    <header><ExclamationCircleOutlined /> {t('需要你处理')} ({items.length})</header>
    {items.map(task => <div className="pa-attention-item" key={task.id}>
      <Button type="link" className="pa-task-title" onClick={() => onOpen(task)}>{taskTitle(task).slice(0, 80)}</Button>
      <TaskActions task={task} onDone={onDone} onError={onError} />
    </div>)}
  </section>;
}
