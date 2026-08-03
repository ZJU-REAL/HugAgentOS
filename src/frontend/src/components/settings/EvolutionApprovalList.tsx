import { CheckOutlined, DownOutlined, InboxOutlined, UpOutlined } from '@ant-design/icons';
import { Button, Tag, message } from 'antd';
import { useEffect, useState } from 'react';

import { approveMyEvolutionCandidate, getMyEvolutionCandidates } from '../../api';
import type { MyEvolutionCandidate } from '../../api';
import { t } from '../../i18n';
import { useCatalogStore } from '../../stores/catalogStore';

/**
 * Capability changes the signed-in user can decide on for themselves.
 *
 * Two rules make this safe to sit in a settings panel rather than behind an
 * administrator, and both are enforced server-side:
 *
 * 1. only candidates distilled mostly from this user's own conversations appear;
 * 2. only changes that *have* a personal form appear — a private skill, or an
 *    adjustment to this user's own memories. Orchestration profiles and
 *    retirements change what everyone's agent does and are not offered here.
 *
 * Every row states the concrete change before asking for a decision. A row that
 * showed only a diagnosis ("opened 6 times, 33% success") under a button
 * labelled "enable for me" asked the user to approve something the page never
 * described — and, for kinds with no personal path, the click could only fail.
 */
const KIND_LABEL: Record<string, string> = {
  skill: '技能',
  memory: '记忆',
};

const OP_LABEL: Record<string, string> = {
  create: '新增',
  update: '改写',
  reweight: '调权',
  deprecate: '停用',
  merge: '合并',
};

function ChangeDetail({ candidate }: { candidate: MyEvolutionCandidate }) {
  const [open, setOpen] = useState(false);
  const change = candidate.change as Record<string, unknown>;

  if (!change || !change.type) return null;

  if (change.type === 'skill_document') {
    const tools = (change.allowed_tools as string[]) ?? [];
    return (
      <div className="jx-evoApproval-change">
        <button type="button" className="jx-evoApproval-toggle" onClick={() => setOpen(!open)}>
          {open ? <UpOutlined /> : <DownOutlined />}
          <span>{open ? t('收起技能正文') : t('查看将要安装的技能正文')}</span>
        </button>
        {(change.description as string) && (
          <p className="jx-evoApproval-desc">{change.description as string}</p>
        )}
        {tools.length > 0 && (
          <div className="jx-evoApproval-tools">
            <span>{t('它会用到的工具')}：</span>
            {tools.map((tool) => (
              <code key={tool}>{tool}</code>
            ))}
          </div>
        )}
        {open && <pre className="jx-evoApproval-doc">{change.content as string}</pre>}
      </div>
    );
  }

  if (change.type === 'memory_ops') {
    const ops = (change.operations as Array<Record<string, string>>) ?? [];
    return (
      <div className="jx-evoApproval-change">
        <ul className="jx-evoApproval-ops">
          {ops.map((op, i) => (
            <li key={i}>
              <Tag>{t(OP_LABEL[op.operation] ?? op.operation)}</Tag>
              <span className="jx-evoApproval-opText">{op.text}</span>
              {op.reason && <em>{op.reason}</em>}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return null;
}

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
        message.success(
          candidate.action === 'apply_memory'
            ? t('已应用该记忆调整')
            : t('已为你启用该能力，可在「能力中心 → 技能」查看'),
        );
        // Drop it locally rather than refetching: the row is gone either way,
        // and a spinner over a list the user just acted on reads as uncertainty.
        setItems((prev) => prev.filter((c) => c.candidate_id !== candidate.candidate_id));
        if (candidate.action !== 'apply_memory') {
          // The new private skill lives in the capability catalog; without a
          // refetch the cached store keeps showing the pre-approval list until
          // the next full page load.
          void useCatalogStore.getState().fetchCatalog();
        }
      })
      .catch((err: unknown) => {
        // The backend explains refusals ("你已启用过…" / "需管理员审批") — a
        // generic retry toast would hide the one line that resolves them.
        const detail = err instanceof Error ? err.message : '';
        message.error(detail && !detail.startsWith('API Error') ? detail : t('操作失败，请稍后重试'));
      })
      .finally(() => setApproving(''));
  };

  if (!loading && items.length === 0) {
    return (
      <div className="jx-evoApproval-empty">
        <InboxOutlined />
        <strong>{t('暂无待你决定的变更')}</strong>
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
            <Tag color={candidate.target_kind === 'memory' ? 'cyan' : 'geekblue'}>
              {t(KIND_LABEL[candidate.target_kind] ?? candidate.target_kind)}
              {candidate.operation ? ` · ${t(OP_LABEL[candidate.operation] ?? candidate.operation)}` : ''}
            </Tag>
            <span className="jx-evoApproval-title">{candidate.summary || t('能力候选')}</span>
          </div>

          <ChangeDetail candidate={candidate} />

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
            {/* Provenance and blast radius, so approval is an informed act. */}
            <span>
              {t('来自你的 {n} 次对话', { n: candidate.your_episodes })}
              {candidate.total_evidence > candidate.your_episodes
                ? ` · ${t('共 {n} 条证据', { n: candidate.total_evidence })}`
                : ''}
              {candidate.action_effect ? ` · ${candidate.action_effect}` : ''}
            </span>
            <Button
              type="primary"
              size="small"
              icon={<CheckOutlined />}
              loading={approving === candidate.candidate_id}
              onClick={() => approve(candidate)}
            >
              {candidate.action_label || t('为我启用')}
            </Button>
          </div>
        </div>
      ))}
      <p className="jx-evoApproval-note">
        {t('这里只列出你能自己决定的变更；影响全体成员的编排调整与能力退役由管理员审批。')}
      </p>
    </div>
  );
}
