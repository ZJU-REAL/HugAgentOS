import { CheckOutlined, InboxOutlined } from '@ant-design/icons';
import { Button, Tag, message } from 'antd';
import { useEffect, useState } from 'react';

import { approveMyEvolutionCandidate, getMyEvolutionCandidates } from '../../api';
import type { MyEvolutionCandidate } from '../../api';
import { t } from '../../i18n';

/**
 * Candidates the signed-in user may accept for themselves.
 *
 * Only those distilled mostly from their own conversations appear here, and
 * accepting one installs it as a *private* skill. That scoping is what makes
 * personal approval safe to expose in a settings panel: no click here can
 * change what anyone else's agent does. Capabilities learned mainly from other
 * people's evidence are a fleet-wide decision and stay with an administrator.
 */
export function EvolutionApprovalList() {
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<MyEvolutionCandidate[]>([]);
  const [approving, setApproving] = useState<string>('');

  const load = () => {
    setLoading(true);
    getMyEvolutionCandidates()
      .then((data) => setItems(data.candidates ?? []))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const approve = (candidate: MyEvolutionCandidate) => {
    setApproving(candidate.candidate_id);
    approveMyEvolutionCandidate(candidate.candidate_id)
      .then(() => {
        message.success(t('已为你启用该能力'));
        // Drop it locally rather than refetching: the row is gone either way,
        // and a spinner over a list the user just acted on reads as uncertainty.
        setItems((prev) => prev.filter((c) => c.candidate_id !== candidate.candidate_id));
      })
      .catch(() => message.error(t('启用失败，请稍后重试')))
      .finally(() => setApproving(''));
  };

  if (!loading && items.length === 0) {
    return (
      <div className="jx-evoApproval-empty">
        <InboxOutlined />
        <strong>{t('暂无待批准的能力')}</strong>
        {/* Nothing pending is the normal state, not a failure — say why. */}
        <span>{t('当同类任务反复出现，系统会把稳定的做法整理成能力候选，等你确认。')}</span>
      </div>
    );
  }

  return (
    <div className="jx-evoApproval">
      {items.map((candidate) => (
        <div key={candidate.candidate_id} className="jx-evoApproval-item">
          <div className="jx-evoApproval-head">
            <span className="jx-evoApproval-title">{candidate.summary || t('能力候选')}</span>
            <Tag color="blue">{t('待批准')}</Tag>
          </div>

          {candidate.tool_sequence?.length > 0 && (
            <div className="jx-evoApproval-steps">
              {candidate.tool_sequence.map((tool, index) => (
                <span key={`${tool}-${index}`} className="jx-evoApproval-step">
                  {index > 0 && <span className="jx-evoApproval-arrow">→</span>}
                  <code>{tool}</code>
                </span>
              ))}
            </div>
          )}

          <div className="jx-evoApproval-meta">
            {/* Provenance, so approval is an informed act rather than a habit. */}
            <span>
              {t('来自你的 {n} 次对话', { n: candidate.your_episodes })}
              {candidate.total_evidence > candidate.your_episodes
                ? ` · ${t('共 {n} 条证据', { n: candidate.total_evidence })}`
                : ''}
            </span>
            <Button
              type="primary"
              size="small"
              icon={<CheckOutlined />}
              loading={approving === candidate.candidate_id}
              onClick={() => approve(candidate)}
            >
              {t('为我启用')}
            </Button>
          </div>
        </div>
      ))}
      <p className="jx-evoApproval-note">
        {t('启用后仅对你生效，不影响其他成员；随时可在能力中心停用。')}
      </p>
    </div>
  );
}
