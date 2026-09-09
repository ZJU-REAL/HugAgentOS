import { useEffect, useRef, useState } from 'react';
import { Modal, Form, Input, Radio, Select, Alert, message } from 'antd';
import { LoadingOutlined, ClockCircleOutlined } from '@ant-design/icons';
import { createAutomation, listPlans, getPlanApi, listChannelConversations, type ChannelConversation } from '../../api';
import type { AutomationScheduleType, Plan } from '../../types';
import { PlanCard, type PlanStepData } from '../chat/PlanCard';
import { ScheduleSelector, isOnceScheduleExpired, type ScheduleValue } from './ScheduleSelector';
import { channelConversationLabel } from './automationUtils';
import { t } from '../../i18n';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { useProjectStore } from '../../stores/projectStore';
import { defaultExecutionLocation, executionLocationError, currentTimezone, availableTimezones, type ExecutionLocation } from './automationLocation';

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

function toPlanStepData(plan: Plan): PlanStepData[] {
  return plan.steps.map((s) => ({
    step_order: s.step_order,
    title: s.title,
    description: s.description,
    expected_tools: s.expected_tools,
    expected_skills: s.expected_skills,
    expected_agents: s.expected_agents,
  }));
}

export function AutomationCreateModal({ open, onClose, onCreated, preset = null }: Props) {
  const [form] = Form.useForm();
  const [defaultTimezone] = useState(currentTimezone);
  const [timezoneOptions] = useState(() => availableTimezones().map(value => ({ value, label: value })));
  const deployment = useDeploymentModeStore();
  const { list: projects, currentProject, fetchProjects } = useProjectStore();
  const canChooseLocation = deployment.isDesktop && deployment.provisionMode === 'dual';
  const resourceGeneration = useRef(0);
  const planRequest = useRef(0);
  const [location, setLocation] = useState<ExecutionLocation>('cloud');
  const [projectId, setProjectId] = useState<string | undefined>();
  const project = projects.find(item => item.project_id === projectId) || (currentProject?.project_id === projectId ? currentProject : null);
  const localPath = (project?.metadata?.local as { path?: string } | undefined)?.path;
  const promptValue = Form.useWatch('prompt', form) || '';
  const selectedTimezone = Form.useWatch('timezone', form) || defaultTimezone;
  const locationError = executionLocationError(location, project?.kind, promptValue);
  useEffect(() => {
    resourceGeneration.current += 1;
    if (!open) return;
    setLocation(defaultExecutionLocation(deployment.provisionMode, currentProject?.kind));
    setProjectId(currentProject?.project_id);
    setPlans([]); setPlansLoaded(false); setPlanCache({}); setSelectedPlan(null);
    form.setFieldValue('plan_id', undefined);
    form.setFieldValue('timezone', defaultTimezone);
    void fetchProjects();
  }, [open]); // Capture the project when the form opens, not on background list refresh.

  const [loading, setLoading] = useState(false);
  const [taskType, setTaskType] = useState<'prompt' | 'plan'>('prompt');
  const [schedule, setSchedule] = useState<ScheduleValue>(defaultSchedule());
  const [plans, setPlans] = useState<
    Array<{ plan_id: string; title: string; total_steps: number }>
  >([]);
  const [plansLoaded, setPlansLoaded] = useState(false);
  const [selectedPlan, setSelectedPlan] = useState<Plan | null>(null);
  const [planDetailLoading, setPlanDetailLoading] = useState(false);
  const [planCache, setPlanCache] = useState<Record<string, Plan>>({});
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
    setTaskType('prompt');
    setSchedule({ schedule_type: 'recurring', cron_expression: preset.cron });
    form.setFieldsValue({ name: preset.name, prompt: preset.prompt });
  }, [open, preset, form]);

  const loadPlans = async () => {
    if (plansLoaded) return;
    const generation = resourceGeneration.current;
    try {
      const result = await listPlans(location === "local" && deployment.provisionMode === "dual" ? "local" : undefined);
      if (generation !== resourceGeneration.current) return;
      setPlans(
        result.map((p) => ({
          plan_id: p.plan_id,
          title: p.title,
          total_steps: p.total_steps,
        })),
      );
      setPlansLoaded(true);
    } catch {
      if (generation === resourceGeneration.current) message.error(t('加载计划列表失败'));
    }
  };

  const handlePlanChange = async (planId: string) => {
    const request = ++planRequest.current;
    const generation = resourceGeneration.current;
    const isCurrent = () => request === planRequest.current && generation === resourceGeneration.current;
    if (!planId) {
      setSelectedPlan(null);
      return;
    }
    if (planCache[planId]) {
      setSelectedPlan(planCache[planId]);
      return;
    }
    setPlanDetailLoading(true);
    setSelectedPlan(null);
    try {
      const plan = await getPlanApi(planId, undefined, location === 'local' && deployment.provisionMode === 'dual' ? 'local' : undefined);
      if (!isCurrent()) return;
      setPlanCache((prev) => ({ ...prev, [planId]: plan }));
      setSelectedPlan(plan);
    } catch {
      if (isCurrent()) message.error(t('加载计划详情失败'));
    } finally {
      if (isCurrent()) setPlanDetailLoading(false);
    }
  };

  const resetAll = () => {
    form.resetFields();
    setTaskType('prompt');
    setSchedule(defaultSchedule());
    setSelectedPlan(null);
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
        task_type: taskType,
        execution_location: location,
        project_id: projectId,
        timezone: values.timezone,
        prompt: taskType === 'prompt' ? values.prompt?.trim() : undefined,
        plan_id: taskType === 'plan' ? values.plan_id : undefined,
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
      width={620}
      centered
      destroyOnClose
      maskClosable={false}
      keyboard={false}
    >
      <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
        <Form.Item label={t('执行位置')} required={canChooseLocation}>
          {canChooseLocation ? (
            <Radio.Group value={location} onChange={e => {
              resourceGeneration.current += 1;
              setPlans([]); setPlanDetailLoading(false);
              setLocation(e.target.value); setSelectedPlan(null); setPlansLoaded(false); setPlanCache({});
              form.setFieldValue('plan_id', undefined);
            }}>
              <Radio.Button value="local">{t('本机')}</Radio.Button>
              <Radio.Button value="cloud">{t('云端')}</Radio.Button>
            </Radio.Group>
          ) : (
            <span>{location === 'local' ? t('本机') : t('云端')}</span>
          )}
          <div style={{ marginTop: 8, color: 'var(--text-secondary)' }}>
            {location === 'local'
              ? t('在当前电脑执行；执行时电脑需开机且本机服务运行')
              : t('电脑关闭后仍可执行；无法直接访问本机文件')}
          </div>
        </Form.Item>
        <Form.Item label={t('关联项目')} help={localPath}>
          <Select allowClear value={projectId} onChange={setProjectId}
            placeholder={t('选择任务需要访问的项目')}
            options={projects.map(item => ({ value: item.project_id,
              label: item.name + ((item.kind as string) === 'local' ? ' · ' + t('本机') : ' · ' + t('云端')) }))} />
        </Form.Item>
        {locationError && <Alert type="error" showIcon title={t(locationError)} style={{ marginBottom: 16 }} />}
        <Form.Item label={t('任务类型')} required>
          <Radio.Group value={taskType} onChange={(e) => setTaskType(e.target.value)}>
            <Radio.Button value="prompt">{t('提示词')}</Radio.Button>
            <Radio.Button value="plan">{t('执行计划')}</Radio.Button>
          </Radio.Group>
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

        {taskType === 'prompt' && (
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
              rows={4}
              maxLength={5000}
              showCount
            />
          </Form.Item>
        )}

        {taskType === 'plan' && (
          <>
            <Form.Item
              label={t('选择计划')}
              name="plan_id"
              rules={[{ required: true, message: t('请选择一个计划') }]}
            >
              <Select
                placeholder={t('选择要定时执行的计划')}
                onFocus={loadPlans}
                onChange={handlePlanChange}
                loading={!plansLoaded && taskType === 'plan'}
                showSearch
                optionFilterProp="label"
                options={plans.map((p) => ({
                  value: p.plan_id,
                  label: p.title,
                  data: p,
                }))}
                optionRender={(opt) => {
                  const data = (opt.data as { data: typeof plans[number] }).data;
                  return (
                    <div className="jx-automation-planOption">
                      <span className="jx-automation-planOption-title">{data.title}</span>
                      <span className="jx-automation-planOption-steps">{t('{n} 步', { n: data.total_steps })}</span>
                    </div>
                  );
                }}
              />
            </Form.Item>

            <PlanPreviewFrame plan={selectedPlan} loading={planDetailLoading} />
          </>
        )}

        <Form.Item label={t('时区')} name="timezone" initialValue={defaultTimezone}
          rules={[{ required: true, message: t('请选择时区') }]}>
          <Select showSearch options={timezoneOptions} />
        </Form.Item>
        <Form.Item label={t('调度方式')} required>
          <ScheduleSelector value={schedule} onChange={setSchedule} timezone={selectedTimezone} />
        </Form.Item>

        <Form.Item label={t('描述')} name="description">
          <Input.TextArea placeholder={t('任务描述（可选）')} rows={2} maxLength={500} />
        </Form.Item>

        <Form.Item label={t('投递目标')} help={convs.length > 0
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
      </Form>
    </Modal>
  );
}

interface PlanPreviewFrameProps {
  plan: Plan | null;
  loading: boolean;
}

function PlanPreviewFrame({ plan, loading }: PlanPreviewFrameProps) {
  if (loading) {
    return (
      <div className="jx-automation-planFrame jx-automation-planFrame--loading">
        <LoadingOutlined />
        <span>{t('正在加载计划详情…')}</span>
      </div>
    );
  }
  if (!plan) return null;

  const agentNameMap =
    (plan as Plan & { agent_name_map?: Record<string, string> }).agent_name_map || undefined;

  return (
    <div className="jx-automation-planFrame">
      <div className="jx-automation-planFrame-label">{t('计划预览')}</div>
      <div className="jx-automation-planFrame-scroll">
        <PlanCard
          mode="preview"
          title={plan.title}
          description={plan.description}
          steps={toPlanStepData(plan)}
          agentNameMap={agentNameMap}
          className="jx-plan-card--embed"
          previewFooter={
            <div className="jx-automation-planFrame-hint">
              <ClockCircleOutlined />
              <span>{t('到期触发时将按以上步骤顺序重新执行')}</span>
            </div>
          }
        />
      </div>
    </div>
  );
}
