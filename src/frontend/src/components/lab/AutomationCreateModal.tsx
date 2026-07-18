import { useEffect, useState } from 'react';
import { Modal, Form, Input, Radio, Select, message } from 'antd';
import { LoadingOutlined, ClockCircleOutlined } from '@ant-design/icons';
import { createAutomation, listPlans, getPlanApi, listChannelConversations, type ChannelConversation } from '../../api';
import type { AutomationScheduleType, Plan } from '../../types';
import { PlanCard, type PlanStepData } from '../chat/PlanCard';
import { ScheduleSelector, type ScheduleValue } from './ScheduleSelector';
import { channelConversationLabel } from './automationUtils';
import { t } from '../../i18n';

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
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

export function AutomationCreateModal({ open, onClose, onCreated }: Props) {
  const [form] = Form.useForm();
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
    listChannelConversations().then(setConvs).catch(() => { /* Fail silently when there are no channel conversations */ });
  }, [open]);

  const loadPlans = async () => {
    if (plansLoaded) return;
    try {
      const result = await listPlans();
      setPlans(
        result.map((p) => ({
          plan_id: p.plan_id,
          title: p.title,
          total_steps: p.total_steps,
        })),
      );
      setPlansLoaded(true);
    } catch {
      message.error(t('加载计划列表失败'));
    }
  };

  const handlePlanChange = async (planId: string) => {
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
      const plan = await getPlanApi(planId);
      setPlanCache((prev) => ({ ...prev, [planId]: plan }));
      setSelectedPlan(plan);
    } catch {
      message.error(t('加载计划详情失败'));
    } finally {
      setPlanDetailLoading(false);
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
      setLoading(true);

      const target = channelTarget && channelTarget !== 'inapp'
        ? convs.find((c) => `${c.channel_id}|${c.conversation_id}` === channelTarget)
        : undefined;
      await createAutomation({
        task_type: taskType,
        prompt: taskType === 'prompt' ? values.prompt : undefined,
        plan_id: taskType === 'plan' ? values.plan_id : undefined,
        cron_expression: schedule.cron_expression,
        schedule_type: schedule.schedule_type as AutomationScheduleType,
        name: values.name || undefined,
        description: values.description || undefined,
        channel_id: target?.channel_id,
        conversation_id: target?.conversation_id,
      });

      message.success(t('自动化任务创建成功'));
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
      title={t('创建自动化任务')}
      open={open}
      onCancel={handleCancel}
      onOk={handleSubmit}
      confirmLoading={loading}
      okText={t('创建')}
      cancelText={t('取消')}
      width={620}
      destroyOnClose
      maskClosable={false}
      keyboard={false}
    >
      <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
        <Form.Item label={t('任务类型')} required>
          <Radio.Group value={taskType} onChange={(e) => setTaskType(e.target.value)}>
            <Radio.Button value="prompt">{t('提示词')}</Radio.Button>
            <Radio.Button value="plan">{t('执行计划')}</Radio.Button>
          </Radio.Group>
        </Form.Item>

        <Form.Item label={t('任务名称')} name="name">
          <Input placeholder={t('为任务取一个名称（可选）')} maxLength={200} />
        </Form.Item>

        {taskType === 'prompt' && (
          <Form.Item
            label={t('提示词')}
            name="prompt"
            rules={[{ required: true, message: t('请输入提示词') }]}
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

        <Form.Item label={t('调度方式')} required>
          <ScheduleSelector value={schedule} onChange={setSchedule} />
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
