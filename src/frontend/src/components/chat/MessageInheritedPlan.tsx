import { OrderedListOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import type { MessageSegment } from '../../types';

interface MessageInheritedPlanProps {
  plan: NonNullable<MessageSegment['planData']>;
}

const STEP_STATUS: Record<string, string> = {
  pending: '待执行', running: '执行中', success: '已完成', failed: '执行失败', skipped: '已跳过',
};

/** Display the state observed at the fork boundary without mounting execution controls or timers. */
export function MessageInheritedPlan({ plan }: MessageInheritedPlanProps) {
  const state = plan.cancelled ? t('已中断')
    : plan.mode === 'preview' ? t('待确认')
      : plan.mode === 'executing' ? t('执行中') : t('已完成');
  return (
    <section className="jx-plan-card">
      <div className="jx-plan-header">
        <div className="jx-plan-headerIcon"><OrderedListOutlined /></div>
        <div className="jx-plan-headerText">
          <h3 className="jx-plan-title">{plan.title}</h3>
          <p className="jx-plan-desc">{t('历史计划（只读）')} · {t('分叉时')}：{state}</p>
          {plan.description && <p className="jx-plan-desc">{plan.description}</p>}
        </div>
      </div>
      <div className="jx-plan-steps">
        {plan.steps.map((step, index) => (
          <details className="jx-plan-step" key={index}>
            <summary className="jx-plan-stepHeader">
              <span className="jx-plan-stepNum">{index + 1}</span>
              <span className="jx-plan-stepTitle">{step.title}</span>
              {step.status && <span className="jx-plan-badge">{t('分叉时')}：{t(STEP_STATUS[step.status] ?? step.status)}</span>}
            </summary>
            {step.description && <p className="jx-plan-stepDesc">{step.description}</p>}
            {step.expected_tools?.length ? <p>{t('连接器')}：{step.expected_tools.join('、')}</p> : null}
            {step.expected_skills?.length ? <p>{t('技能')}：{step.expected_skills.join('、')}</p> : null}
            {step.expected_agents?.length ? <p>{t('智能体')}：{step.expected_agents.map(id => plan.agentNameMap?.[id] ?? id).join('、')}</p> : null}
            {step.acceptance_criteria && <p>{t('验收标准')}：{step.acceptance_criteria}</p>}
            {step.summary && <p className="jx-plan-stepSummary">{step.summary}</p>}
            {step.text && <p className="jx-plan-stepDesc">{step.text}</p>}
          </details>
        ))}
      </div>
      {/* The result text is rendered once in the following message segment. */}
    </section>
  );
}
