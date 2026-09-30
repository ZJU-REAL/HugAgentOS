import { DeleteOutlined, EditOutlined, LeftOutlined, ThunderboltOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Form,
  Input,
  Popconfirm,
  Switch,
  message
} from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  getAutomation, getAutomationRuns,
  listChannelConversations, type ChannelConversation
} from '../../api';
import { useDelayedFlag } from '../../hooks';
import { t } from '../../i18n';
import { useAutomationChatStore, useAutomationStore } from '../../stores';
import type { AutomationRun, AutomationScheduleType, AutomationTask } from '../../types';
import { formatDate } from '../../utils/date';
import { EASE } from '../../utils/motionTokens';
import { AutomationDetailSkeleton } from './AutomationSkeleton';
import { AutomationTaskSections, type EditFormValues } from './AutomationTaskSections';
import { SCHEDULE_TYPE_LABEL, formatRelativeTime } from './automationUtils';
import { isOnceScheduleExpired, type ScheduleValue } from './scheduleTime';

interface Props {
  taskId: string;
  onBack: () => void;
}

const STATUS_LABEL: Record<string, string> = {
  active: t('运行中'),
  paused: t('已暂停'),
  disabled: t('已停用'),
  completed: t('已完成'),
  expired: t('已过期'),
};

export function AutomationDetailPage({ taskId, onBack }: Props) {
  const [task, setTask] = useState<AutomationTask | null>(null);
  const [runs, setRuns] = useState<AutomationRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<EditFormValues>();
  const [editSchedule, setEditSchedule] = useState<ScheduleValue>({
    schedule_type: 'recurring',
    cron_expression: '0 9 * * *',
  });
  // Delivery target: 'inapp' (in-app) or `${channel_id}|${conversation_id}` (channel conversation)
  const [convs, setConvs] = useState<ChannelConversation[]>([]);
  const [channelTarget, setChannelTarget] = useState<string>('inapp');

  const { removeTask, togglePause, triggerNow, updateTask } = useAutomationStore();
  const { enterAutomationChat } = useAutomationChatStore();

  const fetchDetail = useCallback(async () => {
    setLoading(true);
    try {
      const [taskData, r] = await Promise.all([getAutomation(taskId), getAutomationRuns(taskId, 20)]);
      setTask(taskData);
      setRuns(r);
    } catch {
      message.error(t('加载失败'));
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    void fetchDetail();
  }, [fetchDetail]);

  useEffect(() => {
    listChannelConversations().then(setConvs).catch(() => { /* stay silent when there are no channel conversations */ });
  }, []);

  // ─── Derived ───
  const scheduleType: AutomationScheduleType = useMemo(() => {
    if (!task) return 'recurring';
    return task.schedule_type || 'recurring';
  }, [task]);

  const canTrigger = useMemo(() => {
    if (!task) return false;
    return task.status === 'active' || task.status === 'paused';
  }, [task]);

  // Switch only shows for "recurring/once" + active/paused; manual tasks are not controlled by the Switch
  const canToggleRun = useMemo(() => {
    if (!task) return false;
    if (scheduleType === 'manual') return false;
    return task.status === 'active' || task.status === 'paused';
  }, [task, scheduleType]);

  const displayName = useMemo(() => {
    if (!task) return '';
    return (
      task.name ||
      (task.task_type === 'prompt'
        ? task.prompt?.slice(0, 40) || t('提示词任务')
        : task.plan_title || t('计划任务'))
    );
  }, [task]);

  // ─── Handlers (view mode) ───
  const handleToggleRun = async (checked: boolean) => {
    if (!task || !canToggleRun) return;
    try {
      await togglePause(task);
      message.success(checked ? t('已恢复运行') : t('已暂停'));
      const updated = await getAutomation(task.task_id);
      setTask(updated);
    } catch {
      message.error(t('操作失败'));
    }
  };

  const handleTrigger = async () => {
    if (!task) return;
    try {
      await triggerNow(task.task_id);
      message.success(t('已触发执行'));
      // Optimistically insert a running placeholder row (the trigger API does not return run_id, so build a local placeholder,
      // later refetch the detail and reconcile-replace it with real data).
      const placeholder: AutomationRun = {
        run_id: `local-${Date.now()}`,
        task_id: task.task_id,
        status: 'running',
        started_at: new Date().toISOString(),
      };
      setRuns((prev) => [placeholder, ...prev]);
      window.setTimeout(() => { void fetchDetail(); }, 2500);
    } catch {
      message.error(t('触发失败'));
    }
  };

  const handleDelete = async () => {
    if (!task) return;
    try {
      await removeTask(task.task_id);
      message.success(t('已删除'));
      onBack();
    } catch {
      message.error(t('删除失败'));
    }
  };

  const navigateToChat = (run: AutomationRun) => {
    if (!task) return;
    // Enter automation chat mode with timeline panel
    const taskName = task.name || task.prompt?.slice(0, 30) || t('定时任务');
    enterAutomationChat(task.task_id, taskName, runs, run.run_id);
  };

  // ─── Edit mode ───
  const enterEdit = () => {
    if (!task) return;
    form.setFieldsValue({
      name: task.name || '',
      description: task.description || '',
      prompt: task.prompt || '',
    });
    setEditSchedule({
      schedule_type: scheduleType,
      cron_expression: task.cron_expression,
    });
    // Backfill the current delivery target (task_to_dict exposes channel_id/conversation_id; in-app if absent)
    setChannelTarget(
      task.channel_id && task.conversation_id
        ? `${task.channel_id}|${task.conversation_id}`
        : 'inapp',
    );
    setIsEditing(true);
  };

  const cancelEdit = () => {
    setIsEditing(false);
    form.resetFields();
  };

  const handleSave = async () => {
    if (!task) return;
    try {
      const values = await form.validateFields();
      if (isOnceScheduleExpired(editSchedule, task?.timezone)) {
        message.error(t('执行时间已过，请重新选择一个未来的时间'));
        return;
      }
      setSaving(true);

      // Delivery target: if a channel conversation is chosen, split out channel_id/conversation_id; if in-app is chosen, explicitly pass null to switch back to in-app.
      const tgt = channelTarget !== 'inapp'
        ? convs.find((c) => `${c.channel_id}|${c.conversation_id}` === channelTarget)
        : undefined;
      const payload = {
        name: values.name?.trim() || undefined,
        description: values.description?.trim() || undefined,
        prompt: task.task_type === 'prompt' ? values.prompt?.trim() : undefined,
        cron_expression: editSchedule.cron_expression,
        schedule_type: editSchedule.schedule_type,
        channel_id: tgt ? tgt.channel_id : null,
        conversation_id: tgt ? tgt.conversation_id : null,
      };

      const updated = await updateTask(task.task_id, payload);
      setTask(updated);
      setIsEditing(false);
      message.success(t('已保存'));
    } catch (e) {
      const errMsg =
        (e as { errorFields?: unknown[]; message?: string })?.errorFields
          ? t('请检查表单填写')
          : (e as Error)?.message || t('保存失败');
      message.error(errMsg);
    } finally {
      setSaving(false);
    }
  };

  // ─── Render ───
  const showDetailSkeleton = useDelayedFlag(loading && !task);
  if (showDetailSkeleton) {
    return (
      <div className="jx-agentPage">
        <AutomationDetailSkeleton onBack={onBack} />
      </div>
    );
  }
  if (loading && !task) {
    return <div className="jx-agentPage" />;
  }

  if (!task) {
    return (
      <div className="jx-agentPage">
        <div className="jx-automation-detail-top">
          <button className="jx-automation-detail-backBtn" onClick={onBack} aria-label={t('返回')}>
            <LeftOutlined />
          </button>
          <div className="jx-automation-detail-content">
            <div style={{ color: 'var(--color-text-tertiary)', marginTop: 40 }}>{t('任务不存在或已被删除。')}</div>
          </div>
        </div>
      </div>
    );
  }

  const statusLabel = STATUS_LABEL[task.status] || task.status;
  const badgeClass = `jx-automation-detail-badge is-${task.status}`;
  const failedRunWithChat = runs.find((r) => r.status === 'failed' && r.chat_id);

  const isManual = scheduleType === 'manual';
  const isOnce = scheduleType === 'once';

  return (
    <div className="jx-agentPage">
      <div className="jx-automation-detail-top">
        <button className="jx-automation-detail-backBtn" onClick={onBack} aria-label={t('返回')}>
          <LeftOutlined />
        </button>

        <div className="jx-automation-detail-content">
          <AnimatePresence initial={false}>
            {isEditing && (
              <motion.div
                key="editBar"
                className="jx-automation-detail-editBar"
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                transition={{ duration: 0.2, ease: EASE.standard }}
              >
                <span className="jx-automation-detail-editBar-text">{t('编辑中 · 修改后请保存')}</span>
                <div className="jx-automation-detail-editBar-actions">
                  <Button onClick={cancelEdit} disabled={saving}>{t('取消')}</Button>
                  <Button type="primary" onClick={handleSave} loading={saving}>{t('保存')}</Button>
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Field view ↔ edit form: key bound to mode does a 150ms pure-opacity swap (no displacement) */}
          <motion.div
            key={isEditing ? 'edit' : 'view'}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.15, ease: EASE.standard }}
          >

            {/* ── Name row ── */}
            <div className="jx-automation-detail-nameRow">
              <div className="jx-automation-detail-iconWrap">
                <img src="/home/new-icons/automation.svg" alt={t('定时任务')} />
              </div>
              {isEditing ? (
                <Form form={form} component={false}>
                  <Form.Item
                    name="name"
                    style={{ marginBottom: 0, flex: 1 }}
                    rules={[
                      { required: true, message: t('请输入任务名称') },
                      { whitespace: true, message: t('任务名称不能只包含空格') },
                    ]}
                  >
                    <Input
                      placeholder={t('任务名称')}
                      maxLength={200}
                      style={{ fontSize: 16, fontWeight: 500 }}
                    />
                  </Form.Item>
                </Form>
              ) : (
                <span className="jx-automation-detail-name" title={displayName}>
                  {displayName}
                </span>
              )}
              {!isEditing && <span className={badgeClass}>{statusLabel}</span>}
              {!isEditing && canToggleRun && (
                <div className="jx-automation-detail-runSwitch">
                  <span className="jx-automation-detail-runSwitch-label">
                    {task.status === 'active' ? t('运行中') : t('已暂停')}
                  </span>
                  <Switch
                    checked={task.status === 'active'}
                    onChange={handleToggleRun}
                  />
                </div>
              )}
            </div>

            {/* ── Meta row (view only) ── */}
            {!isEditing && (
              <div className="jx-automation-detail-metaRow">
                <span>{SCHEDULE_TYPE_LABEL[scheduleType]}</span>
                <span className="jx-automation-detail-metaRow-sep">·</span>
                <span>
                  {t('下次执行：')}
                  {isManual
                    ? t('仅手动触发')
                    : task.next_run_at
                      ? formatRelativeTime(task.next_run_at)
                      : '-'}
                </span>
                <span className="jx-automation-detail-metaRow-sep">·</span>
                <span>{t('累计执行 {n} 次', { n: task.run_count })}</span>
                <span className="jx-automation-detail-metaRow-sep">·</span>
                <span>{t('创建于 {date}', { date: formatDate(task.created_at) })}</span>
              </div>
            )}

            {/* ── Error alert ── */}
            {!isEditing && task.last_error && (
              <Alert
                className="jx-automation-detail-errorAlert"
                type="error"
                showIcon
                message={
                  <span>
                    {t('最近一次执行失败：')}{task.last_error}
                    {failedRunWithChat && (
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: '0 6px' }}
                        onClick={() => failedRunWithChat.chat_id && navigateToChat(failedRunWithChat)}
                      >
                        {t('查看详情')}
                      </Button>
                    )}
                  </span>
                }
              />
            )}

            <hr className="jx-automation-detail-divider" />

            {/* ── Sections ── */}
            <AutomationTaskSections form={form} task={task} runs={runs} isEditing={isEditing} editSchedule={editSchedule} setEditSchedule={setEditSchedule} scheduleType={scheduleType} isManual={isManual} isOnce={isOnce} channelTarget={channelTarget} setChannelTarget={setChannelTarget} convs={convs} navigateToChat={navigateToChat} />

            {/* ── Action bar (view mode only); entrance animation in automation.css (jx-kf-fadeInUp) ── */}
            {!isEditing && (
              <div className="jx-automation-detail-actionsWrap">
                <div className="jx-automation-detail-actions">
                  <Button
                    type="primary"
                    icon={<ThunderboltOutlined />}
                    disabled={!canTrigger}
                    onClick={handleTrigger}
                  >
                    {t('立即执行')}
                  </Button>
                  <Button icon={<EditOutlined />} onClick={enterEdit}>
                    {t('编辑')}
                  </Button>
                  <Popconfirm
                    title={t('确定删除此定时任务？')}
                    onConfirm={handleDelete}
                    okText={t('删除')}
                    cancelText={t('取消')}
                  >
                    <Button danger icon={<DeleteOutlined />}>{t('删除')}</Button>
                  </Popconfirm>
                </div>
              </div>
            )}
          </motion.div>
        </div>
      </div>
    </div>
  );
}
