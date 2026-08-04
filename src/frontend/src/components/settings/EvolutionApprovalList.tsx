import {
  CheckOutlined,
  EyeOutlined,
  InboxOutlined,
  LockOutlined,
  UpOutlined,
} from '@ant-design/icons';
import { Button, Spin, Tag, message } from 'antd';
import { useEffect, useState } from 'react';

import { approveMyEvolutionCandidate, getMyEvolutionCandidates } from '../../api';
import type { MyEvolutionCandidate } from '../../api';
import { t } from '../../i18n';
import { useChatStore } from '../../stores/chatStore';
import { useCatalogStore } from '../../stores/catalogStore';
import { TOOL_NAME_OVERRIDES } from '../../utils/constants';

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
  new: '新增',
  create: '新增',
  patch: '优化',
  update: '改写',
  reweight: '调权',
  deprecate: '停用',
  merge: '合并',
};

function displayToolName(tool: string, names: Record<string, string>): string {
  return TOOL_NAME_OVERRIDES[tool] || names[tool] || tool;
}

function humaniseFinding(summary: string): string {
  const sequenceFinding = summary.match(
    /^(\d+)\s*个\s*Episode\s*出现相同工具子序列且成功率\s*([^，,]+)[，,]每次仍在重新规划$/,
  );
  if (!sequenceFinding) return summary;
  return t('系统在 {n} 次历史执行中发现了相同做法（成功率 {rate}），建议保存下来，避免以后每次重新规划。', {
    n: sequenceFinding[1],
    rate: sequenceFinding[2],
  });
}

function candidateTitle(
  candidate: MyEvolutionCandidate,
  toolNames: Record<string, string>,
): string {
  const change = candidate.change;
  if (change && 'type' in change && change.type === 'skill_document' && change.display_name) {
    return change.display_name;
  }
  if (candidate.target_kind === 'skill' && candidate.tool_sequence?.length) {
    const steps = candidate.tool_sequence.map((tool) => displayToolName(tool, toolNames));
    return t('固定流程：{steps}', { steps: steps.join(' → ') });
  }
  return candidate.summary || t('能力候选');
}

interface ChangeDetailProps {
  candidate: MyEvolutionCandidate;
  toolNames: Record<string, string>;
}

function ChangeDetail({ candidate, toolNames }: ChangeDetailProps) {
  const [open, setOpen] = useState(false);
  const change = candidate.change as Record<string, unknown>;
  const isSkill = candidate.target_kind === 'skill';

  if (isSkill) {
    const isDocument = change?.type === 'skill_document';
    const isSequence = change?.type === 'skill_sequence';
    const tools = ((change?.allowed_tools as string[]) ?? candidate.tool_sequence ?? []);
    const steps = ((change?.steps as string[]) ?? candidate.tool_sequence ?? []);
    return (
      <div className="jx-evoApproval-change">
        <Button
          className="jx-evoApproval-toggle"
          type="text"
          size="small"
          icon={open ? <UpOutlined /> : <EyeOutlined />}
          onClick={() => setOpen(!open)}
        >
          {open ? t('收起技能详情') : t('查看技能详情')}
        </Button>

        {open && (
          <div className="jx-evoApproval-detailPanel">
            <section>
              <strong>{t('这个技能会做什么')}</strong>
              <p>
                {isDocument && change.description
                  ? String(change.description)
                  : t('遇到需要这些工具共同完成的任务时，智能体会直接复用这套已验证流程，减少重复规划。')}
              </p>
            </section>

            {tools.length > 0 && (
              <section>
                <strong>{isSequence ? t('执行步骤') : t('它会用到的工具')}</strong>
                <ol className="jx-evoApproval-detailSteps">
                  {(steps.length ? steps : tools).map((tool) => {
                    const label = displayToolName(tool, toolNames);
                    return (
                      <li key={tool}>
                        <span>{label}</span>
                        {label !== tool && <code>{tool}</code>}
                      </li>
                    );
                  })}
                </ol>
              </section>
            )}

            {isDocument && Boolean(change.content) && (
              <section>
                <strong>{t('完整技能正文')}</strong>
                <pre className="jx-evoApproval-doc">{String(change.content)}</pre>
              </section>
            )}

            <div className="jx-evoApproval-scope">
              <LockOutlined />
              <span>{t(candidate.action_effect || '作为你的私有技能安装，只对你生效')}</span>
            </div>
          </div>
        )}
      </div>
    );
  }

  if (change?.type === 'memory_ops') {
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
  const [loading, setLoading] = useState(true);
  const [items, setItems] = useState<MyEvolutionCandidate[]>([]);
  const [approving, setApproving] = useState<string>('');
  const toolDisplayNames = useChatStore((state) => state.toolDisplayNames);

  useEffect(() => {
    let active = true;
    getMyEvolutionCandidates()
      .then((data) => {
        if (active) setItems(data.candidates ?? []);
      })
      .catch(() => {
        if (active) setItems([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

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

  if (loading && items.length === 0) {
    return (
      <div className="jx-evoApproval-loading">
        <Spin size="small" />
        <span>{t('正在整理能力候选…')}</span>
      </div>
    );
  }

  if (items.length === 0) {
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
              {candidate.target_kind === 'skill' && ['new', 'create'].includes(candidate.operation)
                ? t('新技能')
                : `${t(KIND_LABEL[candidate.target_kind] ?? candidate.target_kind)}${candidate.operation ? ` · ${t(OP_LABEL[candidate.operation] ?? candidate.operation)}` : ''}`}
            </Tag>
            <div className="jx-evoApproval-identity">
              <span className="jx-evoApproval-title">{candidateTitle(candidate, toolDisplayNames)}</span>
              {candidate.target_kind === 'skill' && (
                <span className="jx-evoApproval-subtitle">
                  {t('把重复成功的做法保存成可复用流程')}
                </span>
              )}
            </div>
          </div>

          {candidate.summary && (
            <div className="jx-evoApproval-finding">
              <span>{t('为什么建议')}</span>
              <p>{humaniseFinding(candidate.summary)}</p>
            </div>
          )}

          {candidate.tool_sequence?.length > 0 && (
            <div className="jx-evoApproval-steps">
              {candidate.tool_sequence.map((tool, index) => (
                <span key={`${tool}-${index}`} className="jx-evoApproval-step">
                  {index > 0 && <span className="jx-evoApproval-arrow">→</span>}
                  <span className="jx-evoApproval-stepIndex">{index + 1}</span>
                  <span>{displayToolName(tool, toolDisplayNames)}</span>
                </span>
              ))}
            </div>
          )}

          <ChangeDetail candidate={candidate} toolNames={toolDisplayNames} />

          <div className="jx-evoApproval-meta">
            {/* Provenance and blast radius, so approval is an informed act. */}
            <span>
              {t('来自你的 {n} 次对话', { n: candidate.your_episodes })}
              {candidate.total_evidence > candidate.your_episodes
                ? ` · ${t('共 {n} 条证据', { n: candidate.total_evidence })}`
                : ''}
              {candidate.action_effect ? ` · ${t(candidate.action_effect)}` : ''}
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
