import { Alert, Form, Input, Modal, Radio, Select, message } from 'antd';
import { useEffect, useEffectEvent, useState } from 'react';
import { createAutomation, listChannelConversations, type ChannelConversation } from '../../api';
import { t } from '../../i18n';
import { AdvancedFields, FormGrid } from '../common/FormLayout';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { useProjectStore } from '../../stores/projectStore';
import type { AutomationScheduleType } from '../../types';
import { ScheduleSelector } from './ScheduleSelector';
import { availableTimezones, currentTimezone, defaultExecutionLocation, executionLocationError, type ExecutionLocation } from './automationLocation';
import { channelConversationLabel } from './automationUtils';
import { isOnceScheduleExpired, type ScheduleValue } from './scheduleTime';

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
  /** 从「推荐任务」卡片进入时的预填值（名称 / 提示词 / cron）。空则走原来的空白表单。 */
  preset?: { name: string; prompt: string; cron: string } | null;
}

function defaultSchedule(): ScheduleValue {
  // Default: recurring schedule · daily at 09:00
  return { schedule_type: 'recurring', cron_expression: '0 9 * * *' };
}

export function AutomationCreateModal({ open, onClose, onCreated, preset = null }: Props) {
  const [form] = Form.useForm();
  const [defaultTimezone] = useState(currentTimezone);
  const [timezoneOptions] = useState(() => availableTimezones().map(value => ({ value, label: value })));
  const deployment = useDeploymentModeStore();
  const { list: projects, currentProject, fetchProjects } = useProjectStore();
  const canChooseLocation = deployment.isDesktop && deployment.provisionMode === 'dual';
  const [location, setLocation] = useState<ExecutionLocation>('cloud');
  const [projectId, setProjectId] = useState<string | undefined>();
  const project = projects.find(item => item.project_id === projectId) || (currentProject?.project_id === projectId ? currentProject : null);
  const localPath = (project?.metadata?.local as { path?: string } | undefined)?.path;
  const promptValue = Form.useWatch('prompt', form) || '';
  const selectedTimezone = Form.useWatch('timezone', form) || defaultTimezone;
  const locationError = executionLocationError(location, project?.kind, promptValue);
  const initializeForm = useEffectEvent(() => {
    setLocation(defaultExecutionLocation(deployment.provisionMode, currentProject?.kind));
    setProjectId(currentProject?.project_id);
    form.setFieldValue('timezone', defaultTimezone);
    void fetchProjects();
  });
  useEffect(() => {
    if (open) initializeForm();
  }, [open]);

  const [loading, setLoading] = useState(false);
  const [schedule, setSchedule] = useState<ScheduleValue>(defaultSchedule());
  // Delivery target: 'inapp' (in-app/on-site, default) or a specific channel conversation `${channel_id}|${conversation_id}`
  const [convs, setConvs] = useState<ChannelConversation[]>([]);
  const [channelTarget, setChannelTarget] = useState<string>('inapp');

  useEffect(() => {
    if (!open) return;
    setConvs([]); setChannelTarget('inapp');
    let active = true;
    listChannelConversations(location === 'local' && deployment.provisionMode === 'dual' ? 'local' : undefined)
      .then(result => { if (active) setConvs(result); }).catch(() => { /* Local CE may have no channel service. */ });
    return () => { active = false; };
  }, [open, location, deployment.provisionMode]);

  // 推荐任务预填：弹窗打开时把示例灌进表单（destroyOnClose 会重建 Form，所以要在 open 后再 set）。
  useEffect(() => {
    if (!open || !preset) return;
    setSchedule({ schedule_type: 'recurring', cron_expression: preset.cron });
    form.setFieldsValue({ name: preset.name, prompt: preset.prompt });
  }, [open, preset, form]);

  const resetAll = () => {
    form.resetFields();
    setSchedule(defaultSchedule());
    setChannelTarget('inapp');
  };

  const handleCancel = () => {
    resetAll();
    onClose();
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      if (isOnceScheduleExpired(schedule, values.timezone)) {
        message.error(t('执行时间已过，请重新选择一个未来的时间'));
        return;
      }
      if (locationError) {
        message.error(t(locationError));
        return;
      }
      if (location === 'local' && deployment.provisionMode === 'dual' && !deployment.localReady) {
        message.error(t('本机服务尚未就绪，请稍后重试'));
        return;
      }
      setLoading(true);

      const target = channelTarget && channelTarget !== 'inapp'
        ? convs.find((c) => `${c.channel_id}|${c.conversation_id}` === channelTarget)
        : undefined;
      await createAutomation({
        task_type: 'prompt',
        execution_location: location,
        project_id: projectId,
        timezone: values.timezone,
        prompt: values.prompt?.trim(),
        cron_expression: schedule.cron_expression,
        schedule_type: schedule.schedule_type as AutomationScheduleType,
        name: values.name?.trim() || undefined,
        description: values.description?.trim() || undefined,
        channel_id: target?.channel_id,
        conversation_id: target?.conversation_id,
      });

      message.success(t('定时任务创建成功'));
      resetAll();
      onCreated();
    } catch (e: unknown) {
      const msg =
        (e as { errorFields?: unknown[] })?.errorFields
          ? t('请检查表单填写')
          : (e as Error)?.message || t('创建失败');
      message.error(msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      wrapClassName="jx-automation-createModal"
      title={t('新建定时任务')}
      open={open}
      onCancel={handleCancel}
      onOk={handleSubmit}
      confirmLoading={loading}
      okText={t('创建')}
      cancelText={t('取消')}
      width={880}
      centered
      destroyOnHidden
      mask={{ closable: false }}
      keyboard={false}
    >
      <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
        <FormGrid>
          <Form.Item label={t('执行位置')} required={canChooseLocation}>
            <div className="jx-automation-locationRow">
              {canChooseLocation ? (
                <Radio.Group value={location} onChange={e => setLocation(e.target.value)}>
                  <Radio.Button value="local">{t('本机')}</Radio.Button>
                  <Radio.Button value="cloud">{t('云端')}</Radio.Button>
                </Radio.Group>
              ) : (
                <span>{location === 'local' ? t('本机') : t('云端')}</span>
              )}
              <span className="jx-automation-locationHint">
                {location === 'local'
                  ? t('在当前电脑执行；执行时电脑需开机且本机服务运行')
                  : t('电脑关闭后仍可执行；无法直接访问本机文件')}
              </span>
            </div>
          </Form.Item>
          <Form.Item
            label={t('任务名称')}
            name="name"
            rules={[
              { required: true, message: t('请输入任务名称') },
              { whitespace: true, message: t('任务名称不能只包含空格') },
            ]}
          >
            <Input placeholder={t('为任务取一个名称，列表与搜索都按它展示')} maxLength={200} />
          </Form.Item>
          <Form.Item label={t('关联项目')} help={localPath}>
            <Select allowClear value={projectId} onChange={setProjectId}
              placeholder={t('选择任务需要访问的项目')}
              options={projects.map(item => ({
                value: item.project_id,
                label: item.name + ((item.kind as string) === 'local' ? ' · ' + t('本机') : ' · ' + t('云端'))
              }))} />
          </Form.Item>
        </FormGrid>
        {locationError && <Alert type="error" showIcon title={t(locationError)} style={{ marginBottom: 16 }} />}

        <Form.Item
          label={t('提示词')}
          name="prompt"
          rules={[
            { required: true, message: t('请输入提示词') },
            { whitespace: true, message: t('提示词不能只包含空格') },
          ]}
        >
          <Input.TextArea
            placeholder={t('输入需要定时执行的提示词，如：帮我搜索今天的政策新闻并生成摘要')}
            autoSize={{ minRows: 3, maxRows: 6 }}
            maxLength={5000}
            showCount
          />
        </Form.Item>

        <Form.Item label={t('调度方式')} required>
          <ScheduleSelector value={schedule} onChange={setSchedule} timezone={selectedTimezone} />
        </Form.Item>

        <FormGrid>
          <Form.Item label={t('时区')} name="timezone" initialValue={defaultTimezone}
            rules={[{ required: true, message: t('请选择时区') }]}>
            <Select showSearch options={timezoneOptions} />
          </Form.Item>
          <Form.Item label={t('投递目标')} tooltip={convs.length > 0
            ? t('到点把结果发到哪：页面端=站内生成一条会话；选某个渠道会话=推送到对应的飞书/钉钉/微信群或私聊。')
            : t('结果发到页面端（站内生成一条会话）。绑定渠道机器人并产生会话后，这里可选投递到对应渠道会话。')}>
            <Select
              value={channelTarget}
              onChange={setChannelTarget}
              options={[
                { value: 'inapp', label: t('页面端（站内）') },
                ...convs.map((c) => ({
                  value: `${c.channel_id}|${c.conversation_id}`,
                  label: channelConversationLabel(c),
                })),
              ]}
            />
          </Form.Item>
        </FormGrid>

        <AdvancedFields title={t('描述（可选）')}>
          <Form.Item label={t('描述')} name="description">
            <Input.TextArea placeholder={t('任务描述（可选）')} rows={2} maxLength={500} />
          </Form.Item>
        </AdvancedFields>
      </Form>
    </Modal>
  );
}
