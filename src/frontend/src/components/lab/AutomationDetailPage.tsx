import { useCallback, useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  Alert,
  Button,
  Form,
  Input,
  Popconfirm,
  Select,
  Switch,
  message,
} from 'antd';
import { LeftOutlined, DeleteOutlined, EditOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { EASE } from '../../utils/motionTokens';
import type { AutomationRun, AutomationScheduleType, AutomationTask } from '../../types';
import {
  getAutomation, getAutomationRuns, activateAutomationSidebar,
  listChannelConversations, type ChannelConversation,
} from '../../api';
import { useAutomationStore, useAutomationChatStore } from '../../stores';
import { useDelayedFlag } from '../../hooks';
import {
  RUN_STATUS_LABEL, cronToHumanReadable, formatRelativeTime, channelConversationLabel,
} from './automationUtils';
import { APP_TIMEZONE, formatDate, formatShortDateTime } from '../../utils/date';
import { ScheduleSelector, type ScheduleValue } from './ScheduleSelector';
import { AutomationDetailSkeleton } from './AutomationSkeleton';
import { t } from '../../i18n';

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

const RUN_STATUS_CLASS: Record<string, string> = {
  // The running-state dot carries .jx-anim-ripple for a spreading ripple (motion.css primitive; color/multiplier in automation.css)
  running: 'is-running jx-anim-ripple',
  success: 'is-success',
  failed: 'is-failed',
};

const SCHEDULE_TYPE_LABEL: Record<AutomationScheduleType, string> = {
  recurring: t('周期执行'),
  once: t('单次执行'),
  manual: t('手动执行'),
};

interface EditFormValues {
  name?: string;
  description?: string;
  prompt?: string;
}

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
    // Activate sidebar on first click (idempotent)
    if (!task.sidebar_activated) {
      activateAutomationSidebar(task.task_id).catch(() => {});
      setTask((prev) => prev ? { ...prev, sidebar_activated: true } : prev);
    }
    // Enter automation chat mode with timeline panel
    const taskName = task.name || task.prompt?.slice(0, 30) || t('自动化任务');
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
            <div style={{ color: '#9CA3AF', marginTop: 40 }}>{t('任务不存在或已被删除。')}</div>
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
              <img src="/home/new-icons/automation.svg" alt={t('自动化')} />
            </div>
            {isEditing ? (
              <Form form={form} component={false}>
                <Form.Item name="name" style={{ marginBottom: 0, flex: 1 }}>
                  <Input
                    placeholder={t('任务名称（可选）')}
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
          <Form form={form} component={false}>
            <div className="jx-automation-detail-sections">
              {/* Task content */}
              <section className="jx-automation-detail-section">
                <div className="jx-automation-detail-sectionHead">
                  <h3 className="jx-automation-detail-sectionTitle">{t('任务内容')}</h3>
                </div>
                <div className="jx-automation-detail-grid">
                  <div className="jx-automation-detail-field">
                    <div className="jx-automation-detail-fieldLabel">{t('任务类型')}</div>
                    <div className="jx-automation-detail-fieldValue">
                      {task.task_type === 'prompt' ? t('提示词') : t('执行计划')}
                      {isEditing && (
                        <span className="jx-automation-detail-fieldValue is-muted" style={{ fontSize: 12, marginLeft: 8 }}>
                          {t('(不可修改)')}
                        </span>
                      )}
                    </div>
                  </div>

                  {task.task_type === 'prompt' ? (
                    <div className="jx-automation-detail-field is-multiline">
                      <div className="jx-automation-detail-fieldLabel">{t('提示词')}</div>
                      {isEditing ? (
                        <Form.Item
                          name="prompt"
                          style={{ marginBottom: 0 }}
                          rules={[{ required: true, message: t('请输入提示词') }]}
                        >
                          <Input.TextArea rows={5} maxLength={5000} showCount />
                        </Form.Item>
                      ) : (
                        <div className="jx-automation-detail-fieldValue">
                          {task.prompt || <span className="is-muted">{t('（空）')}</span>}
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="jx-automation-detail-field is-multiline">
                      <div className="jx-automation-detail-fieldLabel">{t('关联计划')}</div>
                      <div className="jx-automation-detail-fieldValue">
                        {task.plan_title || task.plan_id || <span className="is-muted">{t('（未绑定）')}</span>}
                      </div>
                    </div>
                  )}

                  <div className="jx-automation-detail-field is-multiline">
                    <div className="jx-automation-detail-fieldLabel">{t('描述')}</div>
                    {isEditing ? (
                      <Form.Item name="description" style={{ marginBottom: 0 }}>
                        <Input.TextArea rows={2} maxLength={500} placeholder={t('任务描述（可选）')} />
                      </Form.Item>
                    ) : (
                      <div className="jx-automation-detail-fieldValue">
                        {task.description || <span className="is-muted">{t('（未填写）')}</span>}
                      </div>
                    )}
                  </div>
                </div>
              </section>

              {/* Schedule settings */}
              <section className="jx-automation-detail-section">
                <div className="jx-automation-detail-sectionHead">
                  <h3 className="jx-automation-detail-sectionTitle">{t('调度设定')}</h3>
                </div>
                {isEditing ? (
                  <div className="jx-automation-detail-field is-multiline">
                    <div className="jx-automation-detail-fieldLabel">{t('调度方式')}</div>
                    <ScheduleSelector value={editSchedule} onChange={setEditSchedule} />
                  </div>
                ) : (
                  <div className="jx-automation-detail-grid">
                    <div className="jx-automation-detail-field">
                      <div className="jx-automation-detail-fieldLabel">{t('调度方式')}</div>
                      <div className="jx-automation-detail-fieldValue">
                        {SCHEDULE_TYPE_LABEL[scheduleType]}
                      </div>
                    </div>
                    <div className="jx-automation-detail-field">
                      <div className="jx-automation-detail-fieldLabel">{t('时区')}</div>
                      <div className="jx-automation-detail-fieldValue">
                        {task.timezone || APP_TIMEZONE}
                      </div>
                    </div>
                    {!isManual && (
                      <div className="jx-automation-detail-field is-multiline">
                        <div className="jx-automation-detail-fieldLabel">
                          {isOnce ? t('执行时间') : t('执行频率')}
                        </div>
                        <div className="jx-automation-detail-fieldValue">
                          {isOnce
                            ? (task.next_run_at ? formatShortDateTime(task.next_run_at) : t('已执行'))
                            : cronToHumanReadable(task.cron_expression)}
                          <span style={{ color: '#7B8794', marginLeft: 10, fontSize: 12 }}>
                            ({task.cron_expression})
                          </span>
                        </div>
                      </div>
                    )}
                    {isManual && (
                      <div className="jx-automation-detail-field is-multiline">
                        <div className="jx-automation-detail-fieldLabel">{t('说明')}</div>
                        <div className="jx-automation-detail-fieldValue is-muted">
                          {t('该任务仅在您点击"立即执行"时运行，不会自动触发。')}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </section>

              {/* Delivery target */}
              <section className="jx-automation-detail-section">
                <div className="jx-automation-detail-sectionHead">
                  <h3 className="jx-automation-detail-sectionTitle">{t('投递目标')}</h3>
                </div>
                <div className="jx-automation-detail-field is-multiline">
                  <div className="jx-automation-detail-fieldLabel">{t('发送到')}</div>
                  {isEditing ? (
                    <Select
                      value={channelTarget}
                      onChange={setChannelTarget}
                      style={{ width: '100%' }}
                      options={[
                        { value: 'inapp', label: t('页面端（站内）') },
                        ...convs.map((c) => ({
                          value: `${c.channel_id}|${c.conversation_id}`,
                          label: channelConversationLabel(c),
                        })),
                      ]}
                    />
                  ) : (
                    <div className="jx-automation-detail-fieldValue">
                      {task.channel_id && task.conversation_id
                        ? (() => {
                            const c = convs.find(
                              (x) => x.channel_id === task.channel_id && x.conversation_id === task.conversation_id,
                            );
                            return c ? channelConversationLabel(c) : `${t('渠道会话')} · ${task.conversation_id}`;
                          })()
                        : t('页面端（站内）')}
                    </div>
                  )}
                </div>
              </section>

              {/* Execution records */}
              {!isEditing && (
                <section className="jx-automation-detail-section">
                  <div className="jx-automation-detail-sectionHead">
                    <h3 className="jx-automation-detail-sectionTitle">{t('执行记录')}</h3>
                    <span style={{ fontSize: 12, color: '#7B8794' }}>
                      {runs.length > 0 ? t('最近 {n} 次', { n: runs.length }) : ''}
                    </span>
                  </div>
                  {runs.length === 0 ? (
                    <div className="jx-automation-detail-runEmpty">{t('暂无执行记录')}</div>
                  ) : (
                    <div className="jx-automation-detail-runList">
                      {runs.map((run) => (
                        <div key={run.run_id} className="jx-automation-detail-runRow">
                          <span
                            className={`jx-automation-detail-runRow-dot ${RUN_STATUS_CLASS[run.status] || 'is-failed'}`}
                            title={RUN_STATUS_LABEL[run.status] || run.status}
                          />
                          <span className="jx-automation-detail-runRow-time">
                            {formatShortDateTime(run.started_at)}
                          </span>
                          <span className="jx-automation-detail-runRow-duration">
                            {run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : '-'}
                          </span>
                          <span className="jx-automation-detail-runRow-summary">
                            {run.result_summary || RUN_STATUS_LABEL[run.status] || '-'}
                          </span>
                          {run.status !== 'running' && run.chat_id && (
                            <Button
                              type="link"
                              size="small"
                              onClick={() => navigateToChat(run)}
                            >
                              {t('查看对话')}
                            </Button>
                          )}
                          {run.status === 'failed' && run.error_message && (
                            <div className="jx-automation-detail-runRow-error">
                              {run.error_message}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              )}
            </div>
          </Form>

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
                  title={t('确定删除此自动化任务？')}
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
