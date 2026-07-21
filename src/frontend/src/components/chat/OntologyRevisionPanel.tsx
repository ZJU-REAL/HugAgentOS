import {
  ArrowLeftOutlined,
  CheckCircleFilled,
  CheckOutlined,
  FileSearchOutlined,
  LoadingOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { Button, message } from 'antd';
import { useCallback, useState } from 'react';

import { authFetch } from '../../api';
import { t } from '../../i18n';
import { useChatStore } from '../../stores';
import type { ChatMessage, MessageSegment, OntologyGovernanceSummary } from '../../types';
import { CitationMarkdownBlock } from '../citation';
import { ToolProgressInline, ToolRunShell } from '../tool';
import type { ShellStep } from '../tool/ToolRunShell';
import { ThinkingInline } from './ThinkingInline';

interface OntologyRevisionPanelProps {
  governance: OntologyGovernanceSummary;
  message: ChatMessage;
  chatId: string;
  dispatchProcessVisible: boolean;
}

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

function hasSubstantiveCandidate(value: string): boolean {
  return (value.match(/[A-Za-z0-9\u3400-\u9fff]/g) || []).length >= 8;
}

export function OntologyRevisionPanel({
  governance,
  message: chatMessage,
  chatId,
  dispatchProcessVisible,
}: OntologyRevisionPanelProps) {
  const [accepting, setAccepting] = useState(false);
  const revision = governance.revision;
  // Persisted chats and live SSE frames can contain an older or partial
  // governance shape. Treat every collection as untrusted at this rendering
  // boundary so an incomplete frame cannot crash the whole conversation.
  const review = governance.review && typeof governance.review === 'object'
    ? governance.review
    : {};
  const activations = Array.isArray(governance.activations) ? governance.activations : [];
  const gates = Array.isArray(governance.gates) ? governance.gates : [];
  const revisionToolCalls = Array.isArray(revision?.toolCalls) ? revision.toolCalls : [];
  const revisionThinkingItems = Array.isArray(revision?.thinking) ? revision.thinking : [];
  const manualReview = review.manual_review && typeof review.manual_review === 'object'
    ? review.manual_review
    : undefined;
  const manualReviewItems = Array.isArray(manualReview?.items) ? manualReview.items : [];
  const reviewEvidence = Array.isArray(review.evidence) ? review.evidence : [];
  const newTools = Array.isArray(review.new_tools) ? review.new_tools : [];
  const candidate = typeof revision?.content === 'string'
    ? revision.content
    : typeof review.candidate_answer === 'string'
      ? review.candidate_answer
      : '';
  const hasCandidateContent = candidate.length > 0;
  const hasCandidate = hasSubstantiveCandidate(candidate);
  const accepted = review.accepted === true && hasCandidate;
  const isRevisionStreaming = revision?.status === 'streaming';
  const isReviewRunning = review.status === 'running';
  const isPassWithoutChanges = review.status === 'completed'
    && review.verdict === 'pass'
    && review.revised !== true
    && !hasCandidate;
  const isReviewFailed = review.status === 'failed';
  const canReplaceOriginal = hasCandidate && review.revised === true;
  const hasManualReview = !!manualReview
    && (manualReview.required || manualReviewItems.length > 0);
  const workflowCount = new Set(
    activations.map((item) => `${item.pack_id || ''}:${item.workflow_id || ''}`),
  ).size;
  const gatePassCount = gates.filter((item) => item.decision !== 'deny').length;
  const gateDeniedCount = gates.filter((item) => item.decision === 'deny').length;
  const reviewMode = review.level === 'committee'
    ? t('委员会复核 · {count} 位', { count: review.committee_size || 3 })
    : t('独立核验');
  const latencyLabel = typeof review.latency_ms === 'number'
    ? review.latency_ms >= 1000
      ? `${(review.latency_ms / 1000).toFixed(1)}s`
      : `${review.latency_ms}ms`
    : '—';
  const revisionThinking = revisionThinkingItems
    .map((item) => (item && typeof item.content === 'string' ? item.content : ''))
    .filter(Boolean)
    .join('\n\n');
  const revisionRunSteps: ShellStep[] = [];
  if (revisionThinking) {
    revisionRunSteps.push({
      kind: 'thinking',
      content: revisionThinking,
      active: isRevisionStreaming && !hasCandidateContent,
      key: `${chatMessage.ts}-ontology-thinking`,
    });
  }
  revisionToolCalls.forEach((tool, index) => {
    revisionRunSteps.push({ kind: 'tool', tool, key: `${chatMessage.ts}-ontology-tool-${tool.id || index}` });
  });
  if (revision?.toolPending) {
    revisionRunSteps.push({
      kind: 'pending',
      startTs: chatMessage.lastActivityTs || chatMessage.ts,
      key: `${chatMessage.ts}-ontology-pending`,
    });
  }

  const handleAccept = useCallback(async () => {
    if (!candidate.trim() || accepting || accepted) return;
    setAccepting(true);
    try {
      if (chatMessage.messageId) {
        const response = await authFetch(
          `${apiBaseUrl}/v1/chats/messages/${chatMessage.messageId}/ontology-revision/accept`,
          { method: 'POST' },
        );
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(String(payload?.detail || payload?.message || t('替换失败')));
        }
      }
      useChatStore.getState().updateStore((prev) => {
        const chat = prev.chats[chatId];
        if (!chat) return prev;
        const messages = chat.messages.map((item) => {
          if (item.ts !== chatMessage.ts) return item;
          const preservedSegments = (item.segments || []).filter((segment) => segment.type !== 'text');
          const segments: MessageSegment[] = candidate
            ? [...preservedSegments, { type: 'text', content: candidate }]
            : preservedSegments;
          return {
            ...item,
            content: candidate,
            isMarkdown: true,
            segments,
            ontologyGovernance: item.ontologyGovernance
              ? {
                  ...item.ontologyGovernance,
                  review: { ...item.ontologyGovernance.review, accepted: true },
                }
              : item.ontologyGovernance,
          };
        });
        return {
          ...prev,
          chats: {
            ...prev.chats,
            [chatId]: { ...chat, messages, updatedAt: Date.now() },
          },
        };
      });
      message.success(t('已用本体优化稿替换原文'));
    } catch (error) {
      message.error(error instanceof Error ? error.message : t('替换失败'));
    } finally {
      setAccepting(false);
    }
  }, [accepted, accepting, candidate, chatId, chatMessage.messageId, chatMessage.ts]);

  if (!hasCandidate && !isRevisionStreaming && !isReviewRunning
      && review.status !== 'completed' && !isReviewFailed && !hasManualReview) return null;

  const panelTitle = isReviewRunning
    ? t('本体校验')
    : hasCandidateContent || isRevisionStreaming
      ? t('本体校验结果 · 优化稿')
      : t('本体校验结果');
  const stateLabel = isRevisionStreaming
    ? t('生成中')
    : isReviewRunning
      ? t('评审中')
      : accepted
        ? t('已替换')
        : isPassWithoutChanges
          ? t('校验通过')
          : isReviewFailed || manualReview?.required
            ? t('需人工复核')
            : canReplaceOriginal
              ? t('待确认')
              : t('校验完成');
  const stateClass = isRevisionStreaming || isReviewRunning
    ? ' is-streaming'
    : accepted || isPassWithoutChanges
      ? ' is-accepted'
      : isReviewFailed || manualReview?.required
        ? ' is-warning'
        : '';

  return (
    <aside className="jx-ontologyRevision" aria-label={panelTitle}>
      <header className="jx-ontologyRevision-head">
        <div>
          <span className="jx-ontologyRevision-kicker">ONTOLOGY VALIDATION</span>
          <h3>{panelTitle}</h3>
        </div>
        <span className={`jx-ontologyRevision-state${stateClass}`}>
          {stateLabel}
        </span>
      </header>

      {isReviewRunning && !isRevisionStreaming && (
        <section className="jx-ontologyReviewProgress" aria-live="polite">
          <div className="jx-ontologyReviewProgress-icon"><LoadingOutlined spin /></div>
          <div>
            <strong>{t('正在进行本体校验')}</strong>
            <p>{t('正在核对领域约束与工具证据，完成前不会覆盖原文。')}</p>
          </div>
        </section>
      )}

      {isPassWithoutChanges && (
        <section className="jx-ontologyPassResult">
          <div className="jx-ontologyPassResult-title">
            <CheckCircleFilled />
            <div>
              <h4>{t('本体校验通过 · 原文无需修改')}</h4>
              <p>{t('已完成领域约束、工具证据与输出结构核验。')}</p>
            </div>
          </div>
          <div className="jx-ontologyPassResult-summary" aria-label={t('结构化核验摘要')}>
            <div><span>{t('命中工作流')}</span><strong>{workflowCount}</strong></div>
            <div>
              <span>{t('工具门禁')}</span>
              <strong>
                {gatePassCount}{gateDeniedCount ? ` / ${t('{count} 次拦截', { count: gateDeniedCount })}` : ''}
              </strong>
            </div>
            <div><span>{t('评审方式')}</span><strong>{reviewMode}</strong></div>
            <div><span>{t('评审耗时')}</span><strong>{latencyLabel}</strong></div>
          </div>
          {reviewEvidence.length > 0 && (
            <div className="jx-ontologyPassResult-evidence">
              <span><SafetyCertificateOutlined /> {t('证据摘要')}</span>
              <ul>{reviewEvidence.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>
            </div>
          )}
        </section>
      )}

      {dispatchProcessVisible ? (
        revisionRunSteps.length > 0 && (
          <ToolRunShell
            steps={revisionRunSteps}
            isStreaming={isRevisionStreaming}
            holdOpenUntilText={isRevisionStreaming && !hasCandidateContent}
          />
        )
      ) : (
        <>
          {(revisionThinking || isRevisionStreaming) && (
            <ThinkingInline
              content={revisionThinking}
              thinkKey={`${chatMessage.ts}-ontology-revision`}
              isActive={isRevisionStreaming && !hasCandidateContent}
            />
          )}
          {revisionToolCalls.length > 0 && (
            <ToolProgressInline
              message={{ ...chatMessage, toolCalls: revisionToolCalls, isStreaming: isRevisionStreaming }}
              toolCalls={revisionToolCalls}
              panelKey={`__progress_timeline__-${chatMessage.ts}-ontology`}
            />
          )}
        </>
      )}

      {(newTools.length || review.new_citation_count) ? (
        <div className="jx-ontologyRevision-evidence">
          <FileSearchOutlined />
          <span>
            {newTools.length
              ? t('新增调用：{tools}', { tools: newTools.join('、') })
              : t('已复用现有证据')}
            {review.new_citation_count ? ` · ${t('新增 {count} 条引用', { count: review.new_citation_count })}` : ''}
          </span>
        </div>
      ) : null}

      {(hasCandidateContent || isRevisionStreaming) && (
        <div className={`jx-ontologyRevision-document${isRevisionStreaming ? ' is-streaming' : ''}`}>
          {hasCandidateContent && (
            <div className="jx-ontologyRevision-documentHead">
              <strong>{t('修订后内容')}</strong>
              <span>{t('原文不会自动覆盖')}</span>
            </div>
          )}
          {hasCandidateContent ? (
          <CitationMarkdownBlock
            className="jx-ontologyRevision-text"
            text={candidate}
            isMarkdown
            citations={chatMessage.citations || []}
            messageIsStreaming={isRevisionStreaming}
          />
        ) : (
          <div className="jx-ontologyRevision-placeholder">{t('正在生成优化稿…')}</div>
        )}
        </div>
      )}

      {hasManualReview && (
        <section className={`jx-ontologyManualReview${manualReview?.required ? ' is-required' : ''}`}>
          <div className="jx-ontologyManualReview-head">
            <FileSearchOutlined />
            <div>
              <strong>{manualReview?.title || t('领域本体人工复核')}</strong>
              <p>{manualReview?.summary}</p>
            </div>
          </div>
          <div className="jx-ontologyManualReview-grid">
            {manualReviewItems.map((item, index) => (
              <article key={`${item.rule_id}-${index}`} className="jx-ontologyManualReview-card">
                <span className="jx-ontologyManualReview-index">{String(index + 1).padStart(2, '0')}</span>
                <strong>{item.quote}</strong>
                <dl>
                  <div><dt>{t('规则')}</dt><dd>{item.rule_id}</dd></div>
                  <div><dt>{t('风险')}</dt><dd>{item.risk}</dd></div>
                  <div><dt>{t('复核动作')}</dt><dd>{item.manual_check}</dd></div>
                </dl>
              </article>
            ))}
          </div>
        </section>
      )}

      {canReplaceOriginal && (
        <footer className="jx-ontologyRevision-actions">
          <span>{accepted ? t('原文已替换，优化稿已成为当前正文') : t('原文保持不变，由你决定是否采用')}</span>
          <Button
            type="primary"
            icon={accepted ? <CheckOutlined /> : <ArrowLeftOutlined />}
            loading={accepting}
            disabled={accepted || isRevisionStreaming}
            onClick={() => { void handleAccept(); }}
          >
            {accepted ? t('已替换') : t('替换原文')}
          </Button>
        </footer>
      )}
    </aside>
  );
}
