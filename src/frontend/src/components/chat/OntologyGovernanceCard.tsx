import {
  CheckCircleFilled,
  ExclamationCircleFilled,
  LoadingOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';

import { t } from '../../i18n';
import type { OntologyGovernanceSummary } from '../../types';

interface OntologyGovernanceCardProps {
  governance: OntologyGovernanceSummary;
}

const sourceLabel = (source?: string) => {
  if (source === 'tool') return t('工具');
  if (source === 'skill') return t('技能');
  if (source === 'subagent') return t('子智能体');
  return t('问题文本');
};

export function OntologyGovernanceCard({ governance }: OntologyGovernanceCardProps) {
  const { activations = [], gates = [], review = {} } = governance;
  const denied = gates.filter((gate) => gate.decision === 'deny').length;
  const running = review.status === 'running';
  const completed = review.status === 'completed' || review.status === 'skipped';
  const verdict = review.verdict || '';
  const verdictLabel = review.status === 'skipped'
    ? t('校验完成')
    : verdict === 'revise'
    ? t('已修订答案')
    : verdict === 'escalate'
      ? t('转人工复核')
      : t('评审通过');
  const reviewLabel = review.level === 'committee'
    ? t('评审委员会 · {count} 位委员', { count: review.committee_size || 3 })
    : t('领域本体评审');

  return (
    <section className={`jx-ontologyGovernance${running ? ' is-running' : ''}${denied ? ' has-denial' : ''}`}>
      <div className="jx-ontologyGovernance-head">
        <div className="jx-ontologyGovernance-title">
          <SafetyCertificateOutlined />
          <span>{t('领域本体治理')}</span>
        </div>
        <span className={`jx-ontologyGovernance-status ${running ? 'is-running' : denied || verdict === 'escalate' ? 'is-warning' : 'is-success'}`}>
          {running ? <LoadingOutlined spin /> : denied || verdict === 'escalate' ? <ExclamationCircleFilled /> : <CheckCircleFilled />}
          {running ? t('评审中') : completed ? verdictLabel : t('校验中')}
        </span>
      </div>

      <div className="jx-ontologyGovernance-summary">
        <span>{t('激活工作流')} <strong>{activations.length}</strong></span>
        <span>{t('工具门禁')} <strong>{gates.length}</strong>{denied ? ` · ${t('{count} 次拦截', { count: denied })}` : ''}</span>
        <span>{reviewLabel}</span>
        {typeof review.latency_ms === 'number' && (
          <span>{t('用时 {sec}s', { sec: (review.latency_ms / 1000).toFixed(1) })}</span>
        )}
      </div>

      {activations.length > 0 && (
        <div className="jx-ontologyGovernance-workflows">
          {activations.map((activation, index) => (
            <span key={`${activation.pack_id || ''}:${activation.workflow_id || index}`}>
              {activation.workflow_name || activation.workflow_id || t('领域工作流')}
              <small>{sourceLabel(activation.source)}</small>
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
