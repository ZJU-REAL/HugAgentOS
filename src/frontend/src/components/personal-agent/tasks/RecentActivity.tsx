import { useRef, useState } from 'react';
import { Button, Tooltip } from 'antd';
import { CheckCircleOutlined, CloseCircleOutlined, DownOutlined, ExclamationCircleOutlined, LoadingOutlined, MessageOutlined, MinusCircleOutlined, BorderOutlined, UpOutlined } from '@ant-design/icons';
import { personalApi, type PersonalTask } from '../api';
import { isTerminal, needsUser, taskTitle } from './taskState';
import { t } from '../../../i18n';

const LABELS: Record<string, string> = { queued: '排队中', running: '执行中', waiting: '等待处理', completed: '已完成', failed: '失败', cancelled: '已取消' };
const VISIBLE = 5;

function StatusIcon({ task }: { task: PersonalTask }) {
  if (task.status === 'running') return <LoadingOutlined className="pa-activity-icon pa-activity-icon--running" />;
  if (needsUser(task) && task.status !== 'failed') return <ExclamationCircleOutlined className="pa-activity-icon pa-activity-icon--warning" />;
  if (task.status === 'completed') return <CheckCircleOutlined className="pa-activity-icon" />;
  if (task.status === 'failed') return <CloseCircleOutlined className="pa-activity-icon pa-activity-icon--error" />;
  if (task.status === 'cancelled') return <MinusCircleOutlined className="pa-activity-icon" />;
  return <MessageOutlined className="pa-activity-icon" />;
}

interface RecentActivityProps {
  tasks: PersonalTask[];
  onOpen: (task: PersonalTask) => void;
  onDone: () => Promise<void>;
  onError: (exc: unknown) => void;
}

function TaskRow({ task, onOpen, onDone, onError }: Omit<RecentActivityProps, 'tasks'> & { task: PersonalTask }) {
  const [stopping, setStopping] = useState(false);
  const stopPending = useRef(false);
  async function stop() {
    if (stopPending.current) return;
    stopPending.current = true;
    setStopping(true);
    try {
      await personalApi.cancel(task.id);
      await onDone();
    } catch (exc) {
      onError(exc);
    } finally {
      stopPending.current = false;
      setStopping(false);
    }
  }
  // An active task shows what it is doing now; a waiting one says why it waits.
  const detail = isTerminal(task) ? '' : task.waiting_reason || task.progress || '';
  return <li className={!isTerminal(task) ? 'pa-activity-item--active' : undefined}>
    <Tooltip title={t(LABELS[task.status] || task.status)} placement="left" mouseEnterDelay={0.4}>
      <button type="button" className="pa-activity-row" data-testid="personal-task" data-status={task.status} onClick={() => onOpen(task)}>
        <StatusIcon task={task} />
        <span className="pa-activity-text"><span className="pa-activity-title">{taskTitle(task)}</span>{detail && <small className="pa-activity-detail">{detail}</small>}</span>
      </button>
    </Tooltip>
    {!isTerminal(task) && <Tooltip title={t('停止')}>
      <Button type="text" size="small" className="pa-activity-stop" aria-label={t('停止')}
        icon={<BorderOutlined />} loading={stopping} disabled={stopping} onClick={() => void stop()} />
    </Tooltip>}
  </li>;
}

/** Like a goals list: what is in progress, then what finished recently. Open a task for its full conversation. */
export function RecentActivity({ tasks, onOpen, onDone, onError }: RecentActivityProps) {
  const [expanded, setExpanded] = useState(false);
  if (!tasks.length) return <p className="pa-side-empty">{t('还没有任务。需要时我会在后台开始任务。')}</p>;
  const active = tasks.filter(task => !isTerminal(task));
  const finished = tasks.filter(isTerminal);
  const shown = expanded ? finished : finished.slice(0, VISIBLE);
  return <>
    {!!active.length && <><h4 className="pa-activity-group">{t('进行中')} · {active.length}</h4>
      <ul className="pa-activity" aria-label={t('进行中')}>{active.map(task => <TaskRow key={task.id} task={task} onOpen={onOpen} onDone={onDone} onError={onError} />)}</ul></>}
    {!!finished.length && <><h4 className="pa-activity-group">{t('已结束')}</h4>
      <ul className="pa-activity" aria-label={t('已结束')}>{shown.map(task => <TaskRow key={task.id} task={task} onOpen={onOpen} onDone={onDone} onError={onError} />)}</ul></>}
    {finished.length > VISIBLE && <button type="button" className="pa-side-more" onClick={() => setExpanded(value => !value)}>
      {expanded ? <UpOutlined /> : <DownOutlined />} {expanded ? t('收起') : t('展开更多')}
    </button>}
  </>;
}
