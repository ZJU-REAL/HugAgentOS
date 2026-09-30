import {
  Button,
  Form,
  Input,
  Select
} from 'antd';
import {
  type ChannelConversation
} from '../../api';
import { t } from '../../i18n';
import type { AutomationRun, AutomationScheduleType, AutomationTask } from '../../types';
import { APP_TIMEZONE, formatShortDateTime } from '../../utils/date';
import {
  RUN_STATUS_CLASS,
  RUN_STATUS_LABEL, SCHEDULE_TYPE_LABEL, channelConversationLabel,
  cronToHumanReadable,
  formatRunDuration, formatTimezone
} from './automationUtils';
import { ScheduleSelector } from './ScheduleSelector';

import type { FormInstance } from 'antd';
import type { ScheduleValue } from './scheduleTime';
export interface EditFormValues { name?: string; description?: string; prompt?: string; }
interface SectionsProps {
  form: FormInstance<EditFormValues>; task: AutomationTask; runs: AutomationRun[]; isEditing: boolean;
  editSchedule: ScheduleValue; setEditSchedule: (value: ScheduleValue) => void;
  scheduleType: AutomationScheduleType; isManual: boolean; isOnce: boolean;
  channelTarget: string; setChannelTarget: (value: string) => void; convs: ChannelConversation[];
  navigateToChat: (run: AutomationRun) => void;
}
export function AutomationTaskSections({ form, task, runs, isEditing, editSchedule, setEditSchedule, scheduleType, isManual, isOnce, channelTarget, setChannelTarget, convs, navigateToChat }: SectionsProps) {
  return (
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
                    rules={[
                      { required: true, message: t('请输入提示词') },
                      { whitespace: true, message: t('提示词不能只包含空格') },
                    ]}
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
              <ScheduleSelector value={editSchedule} onChange={setEditSchedule} timezone={task.timezone} />
            </div>
          ) : (
            <div className="jx-automation-detail-grid">
              <div className="jx-automation-detail-field">
                <div className="jx-automation-detail-fieldLabel">{t('执行位置')}</div>
                <div className="jx-automation-detail-fieldValue">
                  {task.execution_location === 'local' ? t('本机') : t('云端')}
                  {task.device_name ? ' · ' + task.device_name : ''}
                  {task.project_name ? ' · ' + task.project_name : ''}
                </div>
                {task.project_local_path && <div>{task.project_local_path}</div>}
              </div>
              <div className="jx-automation-detail-field">
                <div className="jx-automation-detail-fieldLabel">{t('调度方式')}</div>
                <div className="jx-automation-detail-fieldValue">
                  {SCHEDULE_TYPE_LABEL[scheduleType]}
                </div>
              </div>
              <div className="jx-automation-detail-field">
                <div className="jx-automation-detail-fieldLabel">{t('时区')}</div>
                <div className="jx-automation-detail-fieldValue" title={task.timezone || APP_TIMEZONE}>
                  {formatTimezone(task.timezone || APP_TIMEZONE)}
                </div>
              </div>
              {!isManual && (
                <div className="jx-automation-detail-field is-multiline">
                  <div className="jx-automation-detail-fieldLabel">
                    {isOnce ? t('执行时间') : t('执行频率')}
                  </div>
                  {/* 只展示一个人类可读的时间。原来还在后面缀了一段裸 cron
                            （`(0 9 20 8 *)`），对用户是噪音、还被误读成「第二个执行时间」；
                            保留成 title 提示，需要排查时鼠标悬停仍看得到。 */}
                  <div
                    className="jx-automation-detail-fieldValue"
                    title={task.cron_expression}
                  >
                    {isOnce
                      ? (task.next_run_at ? formatShortDateTime(task.next_run_at) : t('已执行'))
                      : cronToHumanReadable(task.cron_expression)}
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
              <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>
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
                      className={`jx-automation-runDot ${RUN_STATUS_CLASS[run.status] || 'is-failed'}`}
                      title={RUN_STATUS_LABEL[run.status] || run.status}
                    />
                    <span className="jx-automation-detail-runRow-time">
                      {formatShortDateTime(run.started_at)}
                    </span>
                    <span className="jx-automation-detail-runRow-duration">
                      {formatRunDuration(run.duration_ms)}
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

  );
}
