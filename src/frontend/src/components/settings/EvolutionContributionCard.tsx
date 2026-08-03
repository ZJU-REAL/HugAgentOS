import { CheckCircleFilled, ClockCircleOutlined, DatabaseOutlined, NodeIndexOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { Modal, Spin, Tag } from 'antd';
import type { ReactNode } from 'react';
import { useEffect, useState } from 'react';

import { getEvolutionContributions } from '../../api';
import type { EvolutionContributions, EvolutionContributionItem } from '../../api';
import { t } from '../../i18n';

interface Props {
  open: boolean;
  onClose: () => void;
}

const KIND_META: Record<string, { label: string; icon: ReactNode }> = {
  memory: { label: '记忆生长', icon: <DatabaseOutlined /> },
  skill: { label: '技能沉淀', icon: <ThunderboltOutlined /> },
  workflow: { label: '编排修正', icon: <NodeIndexOutlined /> },
  ontology: { label: '领域约束', icon: <CheckCircleFilled /> },
  prompt: { label: '提示词', icon: <CheckCircleFilled /> },
};

/**
 * Status → text and colour.
 *
 * Mirrors the in-chat card exactly: a draft reads "awaiting review", and
 * "published" appears only once a version pointer really moved. Two surfaces
 * describing the same candidate differently would be worse than either one
 * being wrong on its own.
 */
const STATUS_META: Record<string, { label: string; color: string }> = {
  draft: { label: '候选待审', color: 'blue' },
  replay_passed: { label: '回放通过', color: 'geekblue' },
  shadow: { label: '影子运行', color: 'purple' },
  canary: { label: '灰度中', color: 'orange' },
  active: { label: '已上架', color: 'green' },
  retired: { label: '已退役', color: 'default' },
  rejected: { label: '已拒绝', color: 'red' },
  rolled_back: { label: '已回滚', color: 'red' },
};

function ChainRow({ item }: { item: EvolutionContributionItem }) {
  const kind = KIND_META[item.target_kind] ?? { label: item.target_kind, icon: null };
  const status = STATUS_META[item.status] ?? { label: item.status, color: 'default' };

  return (
    <div className="jx-evoContrib-row">
      <div className="jx-evoContrib-rowHead">
        <span className="jx-evoContrib-rowIcon">{kind.icon}</span>
        <span className="jx-evoContrib-rowKind">{t(kind.label)}</span>
        <Tag color={status.color}>{t(status.label)}</Tag>
      </div>
      <p className="jx-evoContrib-rowSummary">{item.summary || t('（无说明）')}</p>
      <div className="jx-evoContrib-chain">
        {/* The provenance chain, shown as a chain rather than a count: the point
            is that a capability came from specific conversations. */}
        <span className="jx-evoContrib-chainNode">
          {t('你的 {n} 次对话', { n: item.your_episodes })}
        </span>
        <span className="jx-evoContrib-chainArrow">→</span>
        <span className="jx-evoContrib-chainNode">
          {t('共 {n} 条证据', { n: item.total_evidence })}
        </span>
        <span className="jx-evoContrib-chainArrow">→</span>
        <span className="jx-evoContrib-chainNode is-terminal">{t(status.label)}</span>
      </div>
    </div>
  );
}

export function EvolutionContributionCard({ open, onClose }: Props) {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<EvolutionContributions | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    getEvolutionContributions()
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch(() => {
        if (!cancelled) setError(t('进化详情暂时无法加载'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const candidates = data?.candidates ?? [];

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={640}
      title={t('本账号促成的能力进化')}
      className="jx-evoContrib-modal"
    >
      {loading && (
        <div className="jx-evoContrib-loading">
          <Spin />
        </div>
      )}

      {!loading && error && <p className="jx-evoContrib-error">{error}</p>}

      {!loading && !error && data && (
        <>
          <div className="jx-evoContrib-stats">
            <div className="jx-evoContrib-stat">
              <strong>{data.contributed_episodes}</strong>
              <span>{t('已贡献证据')}</span>
            </div>
            <div className="jx-evoContrib-stat">
              <strong>{data.private_episodes}</strong>
              <span>{t('仅自己可见')}</span>
            </div>
            <div className="jx-evoContrib-stat">
              <strong>{data.memory_written}</strong>
              <span>{t('记忆条目')}</span>
            </div>
            <div className="jx-evoContrib-stat">
              <strong>{candidates.length}</strong>
              <span>{t('促成的能力')}</span>
            </div>
          </div>

          {candidates.length === 0 ? (
            <div className="jx-evoContrib-empty">
              <ClockCircleOutlined />
              {/* Honest empty state: nothing has been distilled yet is a normal
                  outcome, not a failure — evidence aggregates before it acts. */}
              <strong>{t('还没有由你的对话促成的能力')}</strong>
              <span>
                {t('能力需要在多次相似任务中反复出现才会被沉淀，单次对话不会立刻产生候选。')}
              </span>
            </div>
          ) : (
            <div className="jx-evoContrib-list">
              {candidates.map((item) => (
                <ChainRow key={item.candidate_id} item={item} />
              ))}
            </div>
          )}

          <p className="jx-evoContrib-note">
            {t('候选需经审核与验证才会正式生效；关闭参与后，此前已贡献的证据不会自动撤回。')}
          </p>
        </>
      )}
    </Modal>
  );
}
