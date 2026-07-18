import { Button, Tag, Popconfirm, message } from 'antd';
import { PauseCircleOutlined, PlayCircleOutlined, DeleteOutlined, ThunderboltOutlined } from '@ant-design/icons';
import type { AutomationTask } from '../../types';
import { useAutomationStore } from '../../stores';
import { cronToHumanReadable, formatRelativeTime } from './automationUtils';
import { t } from '../../i18n';

interface Props {
  task: AutomationTask;
  onClick: () => void;
}

const STATUS_MAP: Record<string, { color: string; label: string }> = {
  active: { color: 'green', label: t('运行中') },
  paused: { color: 'orange', label: t('已暂停') },
  disabled: { color: 'red', label: t('已停用') },
  completed: { color: 'default', label: t('已完成') },
  expired: { color: 'default', label: t('已过期') },
};

export function AutomationCard({ task, onClick }: Props) {
  const { togglePause, removeTask, triggerNow } = useAutomationStore();
  const statusInfo = STATUS_MAP[task.status] || STATUS_MAP.completed;

  const handlePause = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await togglePause(task);
      message.success(task.status === 'active' ? t('已暂停') : t('已恢复'));
    } catch {
      message.error(t('操作失败'));
    }
  };

  const handleDelete = async () => {
    try {
      await removeTask(task.task_id);
      message.success(t('已删除'));
    } catch {
      message.error(t('删除失败'));
    }
  };

  const handleTrigger = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await triggerNow(task.task_id);
      message.success(t('已触发执行'));
    } catch {
      message.error(t('触发失败'));
    }
  };

  const displayName = task.name || (task.task_type === 'prompt' ? (task.prompt?.slice(0, 40) || t('提示词任务')) : (task.plan_title || t('计划任务')));

  return (
    <div className="jx-automation-card" onClick={onClick} role="button" tabIndex={0}>
      <div className="jx-automation-card-main">
        <div className="jx-automation-card-header">
          <span className="jx-automation-card-name">{displayName}</span>
          <Tag color={statusInfo.color}>{statusInfo.label}</Tag>
        </div>
        <div className="jx-automation-card-meta">
          <span className="jx-automation-card-type">
            {task.task_type === 'prompt' ? t('提示词') : t('计划')}
          </span>
          <span className="jx-automation-card-sep" />
          <span className="jx-automation-card-schedule">
            {task.schedule_type === 'recurring' ? cronToHumanReadable(task.cron_expression) : t('单次执行')}
          </span>
          {task.next_run_at && task.status === 'active' && (
            <>
              <span className="jx-automation-card-sep" />
              <span className="jx-automation-card-next">
                {t('下次: {time}', { time: formatRelativeTime(task.next_run_at) })}
              </span>
            </>
          )}
          {task.run_count > 0 && (
            <>
              <span className="jx-automation-card-sep" />
              <span>{t('已执行 {n} 次', { n: task.run_count })}</span>
            </>
          )}
        </div>
      </div>
      <div className="jx-automation-card-actions" onClick={(e) => e.stopPropagation()}>
        {(task.status === 'active' || task.status === 'paused') && (
          <Button
            type="text"
            size="small"
            icon={<ThunderboltOutlined />}
            title={t('立即执行')}
            onClick={handleTrigger}
          />
        )}
        {(task.status === 'active' || task.status === 'paused') && (
          <Button
            type="text"
            size="small"
            icon={task.status === 'active' ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
            title={task.status === 'active' ? t('暂停') : t('恢复')}
            onClick={handlePause}
          />
        )}
        <Popconfirm
          title={t('确定删除此自动化任务？')}
          onConfirm={handleDelete}
          okText={t('删除')}
          cancelText={t('取消')}
        >
          <Button type="text" size="small" icon={<DeleteOutlined />} title={t('删除')} danger />
        </Popconfirm>
      </div>
    </div>
  );
}
