import React, { useCallback, type ReactNode } from 'react';
import { BulbOutlined, BulbFilled, DownOutlined, CheckCircleFilled } from '@ant-design/icons';
import { useConversationCitations } from '../../hooks/useConversationCitations';
import { groupExecutionRuns } from '../../utils/executionRuns';
import { isForkedHistory } from '../../utils/forkHistory';
import { ToolRunShell } from '../tool/ToolRunShell';
import { ToolProgressInline } from '../tool/ToolProgressInline';
import { anyToolRunning } from '../tool/renderers/utils';
import { ThinkingInline } from './ThinkingInline';
import { StreamWaitIndicator } from './StreamWaitIndicator';
import { TurnStatusIndicator } from './TurnStatusIndicator';
import { AgentCursor } from './AgentCursor';
import type { AgentMarkState } from '../common/AgentMark';
import { InvocationBadges } from './InvocationBadges';
import { PlanCard } from './PlanCard';
import { MessageInheritedPlan } from './MessageInheritedPlan';
import { UserMarkdownBlock } from './UserMarkdownBlock';
import { CitationMarkdownBlock } from '../citation';
import { useChatStore, useUIStore } from '../../stores';
import { updatePlanApi } from '../../api';
import { quoteSourceLabel } from '../../utils/quotedFollowUp';
import { markPlanDecision } from '../../hooks/usePlanMode';
import { useActivityAge } from '../../hooks/useActivityAge';
import { ACTIVITY_THRESHOLDS, TAIL_WAIT_MS, WAIT_CLOCK_MS, agentStateForPhase, runPhase, waitLabel } from '../../utils/turnActivity';
import { useMessageCitation } from './useMessageCitation';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';
interface MessageBodyProps {
  m: ChatMessage;
  showProcess?: boolean;
  messageIndex: number;
  currentChatId: string;
  send: (text?: string) => void;
}
export function MessageBody({ m, messageIndex, currentChatId, send, showProcess }: MessageBodyProps) {
  const handleCitationAction = useMessageCitation(m);
  const expandedThinking = useChatStore((s) => s.expandedThinking);
  const toggleThinking = useChatStore((s) => s.toggleThinking);
  const chatMode = useChatStore((s) => s.chatMode);
  const processPreference = useUIStore((s) => s.dispatchProcessVisible);
  const dispatchProcessVisible = showProcess ?? processPreference;
  const conversationCitations = useConversationCitations(currentChatId, m, messageIndex);
  // 视觉桥正在读图（模型还没开口）→ 轮级状态换成「图像理解中」，别让这几秒看起来
  // 像模型在发呆。识图结束由 chatStream 清空。
  const visionReadingCount = useChatStore(state => state.visionReading[currentChatId] ?? 0);
  const turnStatusLabel = visionReadingCount > 0
    ? (visionReadingCount > 1
        ? t('图像理解中（{n} 张）…', { n: visionReadingCount })
        : t('图像理解中…'))
    : undefined;
  // ── Plan preview approval (buttons on the plan card footer) ──
  const onPlanConfirm = useCallback(() => {
    if (isForkedHistory(m)) return;
    // Send the literal confirm phrase (not translated) — sendPlanMode's
    // isConfirm regex matches on it; ensure plan-mode routing is on in case
    // the toggle was flipped off after generation.
    useChatStore.getState().setPlanMode(true);
    send('确认执行');
  }, [m, send]);

  const onPlanDiscard = useCallback((planId: string) => {
    if (isForkedHistory(m)) return;
    markPlanDecision(currentChatId, planId, 'cancelled');
    const { currentPlanId, setCurrentPlanId } = useChatStore.getState();
    if (currentPlanId === planId) setCurrentPlanId(null);
    updatePlanApi(planId, { status: 'cancelled' }, currentChatId).catch(() => { /* best-effort; the card is already marked */ });
  }, [currentChatId, m]);
  // Track stream activity for the startup clock and output dots.
  const streamedArgsLength = (m.toolCalls || []).reduce(
    (total, tool) => total + (tool.inputText?.length || 0),
    0,
  );
  // Streaming reasoning is activity too. Only the *first* thinking chunk pushes
  // a segment; every later chunk appends to the same block's `content`, which
  // no other term of this signature reads. Without this the signature freezes
  // for the whole reasoning phase and the turn indicator claims the run stalled
  // while thinking is visibly streaming — most obvious on tool-calling turns,
  // where the model reasons for seconds before the first tool call.
  const streamedThinkingLength = (m.thinking || []).reduce(
    (total, block) => total + (block.content?.length || 0),
    0,
  );
  const stallSignature = `${(m.content || '').length}|${m.toolCalls?.length ?? 0}|${streamedArgsLength}|${streamedThinkingLength}|${m.segments?.length ?? 0}`;
  // The startup clock anchors to the persisted activity stamp, so it keeps
  // counting from the real start across remounts.
  const turnStartTs = m.lastActivityTs ?? m.ts;
  // A tool finishing is activity too — it is what starts the "analysing results" wait.
  const settledTools = (m.toolCalls || []).filter((tool) => tool.status && tool.status !== 'running').length;
  const activity = useActivityAge(
    `${stallSignature}|${settledTools}`,
    !!m.isStreaming && dispatchProcessVisible,
    ACTIVITY_THRESHOLDS,
  );

  /** Render a settled thinking block. Live thinking is rendered by ThinkingInline. */
  const renderThinkingBlock = (content: string, thinkKey: string) => {
    const isExpanded = expandedThinking.has(thinkKey);
    const toggleThink = () => toggleThinking(thinkKey);
    return (
      <div key={thinkKey} className="jx-thinkingBlock">
        <div className="jx-thinkingBlockHeader"
          role="button" tabIndex={0} onClick={toggleThink}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleThink(); } }}>
          <div className="jx-thinkingHeaderLeft">
            <BulbFilled className="jx-thinkingIcon" />
            <span className="jx-thinkingLabel">{t('思考过程')}</span>
          </div>
          <DownOutlined className={`jx-expandIcon${isExpanded ? ' jx-expandIcon--open' : ''}`} />
        </div>
        <div className={`jx-expandWrap${isExpanded ? ' jx-expandWrap--open' : ''}`}>
          <div className="jx-thinkingContent">{isExpanded && content}</div>
        </div>
      </div>
    );
  };

  const renderUserQuote = () => {
    if (m.role !== 'user' || !m.quotedFollowUp?.text) return null;
    const source = quoteSourceLabel(m.quotedFollowUp.source);
    return (
      <div className="jx-userQuote" title={m.quotedFollowUp.text}>
        <span className="jx-userQuoteLabel">{source ? t('引用自 {source}', { source }) : t('引用')}</span>
        <span className="jx-userQuoteText">{m.quotedFollowUp.text}</span>
      </div>
    );
  };

  const renderChipBadges = () => {
    if (m.role !== 'user') return null;
    return (
      <InvocationBadges
        mentionName={m.mentionName}
        skillName={m.skillName}
        pluginName={m.pluginName}
        connectorName={m.connectorName}
        chatRefs={m.referencedChats}
      />
    );
  };

  return <>
        {m.segments && m.segments.length > 0 ? (
          /* Segment-based rendering */
          <>
            {(() => {
              // Group thinking + tool calls into a single "agent run" shell so
              // the chat flow shows one card per phase instead of separate
              // thinking / tool-call entries.
              //
              // A run is a maximal contiguous sequence of {thinking, tool,
              // empty-text} segments. Non-empty text breaks the run. Pure
              // empty-text only chunks are absorbed but never anchor a run.
              //
              // OFF mode (dispatchProcessVisible=false) keeps the existing
              // ToolProgressInline for tool batches; the unified shell takes over in ON mode.
              const segs = m.segments!;
              const { runs, suppressedIdx } = groupExecutionRuns(m, dispatchProcessVisible);

              // ON mode gives every moment of a live reply one owner: the tool run
              // still in flight, or a tail row under the text. The reply's single
              // agent mark parks on that owner, so a pause never reads as a hang —
              // except a run with tools, whose lit tool icons already say what is
              // happening; the mark steps away and comes back on the next owner.
              const liveTurn = !!m.isStreaming && dispatchProcessVisible;
              const lastRun = runs[runs.length - 1];
              const tailRun = liveTurn && lastRun
                && !segs.slice(lastRun.endIdx + 1).some(s => !(s.type === 'text' && !(s.content || '').trim()))
                ? lastRun : null;
              // A reasoning block outside a run is already on screen (OFF mode only).
              const hasVisibleProgress = runs.length > 0 || segs.some(
                s => (s.type === 'text' || s.type === 'thinking') && (s.content || '').trim().length > 0,
              );
              const tailSlot = `${m.uid}-tail`;
              let tail: { label: string; startTs: number; clockAfterMs?: number } | null = null;
              let agent: { slot: string; state: AgentMarkState } | null = null;
              if (liveTurn && tailRun) {
                if (tailRun.tools.length === 0) {
                  const phase = runPhase(tailRun.steps, { isStreaming: true, isTail: true, idleMs: activity.idleMs });
                  agent = { slot: `${m.uid}-seg-${tailRun.anchor}`, state: agentStateForPhase(phase) };
                }
              } else if (liveTurn && !hasVisibleProgress) {
                // The genuine dead window: the bubble exists and nothing has streamed yet.
                tail = { label: turnStatusLabel ?? t('正在处理…'), startTs: turnStartTs };
                agent = { slot: tailSlot, state: 'work' };
              } else if (liveTurn && activity.idleMs >= TAIL_WAIT_MS && activity.since !== null) {
                // While words arrive the text speaks for itself — nothing under it.
                // Once it pauses, say that the next step is being worked out.
                tail = {
                  label: waitLabel(activity.idleMs, t('正在思考下一步…')),
                  startTs: activity.since,
                  clockAfterMs: WAIT_CLOCK_MS,
                };
                agent = { slot: tailSlot, state: 'think' };
              }
              // The first run takes over from the startup row only if nothing showed before it.
              const firstRun = runs[0];
              const takeOver = liveTurn && firstRun
                && !segs.slice(0, firstRun.anchor).some(s => (s.content || '').trim())
                ? firstRun : null;

              const runByAnchor = new Map<number, (typeof runs)[number]>();
              runs.forEach((r) => runByAnchor.set(r.anchor, r));

              const rendered = segs.map((seg, segIdx) => {
                const isLastSeg = segIdx === segs.length - 1;
                const segKey = `${m.uid}-seg-${segIdx}`;

                if (runByAnchor.has(segIdx)) {
                  const run = runByAnchor.get(segIdx)!;
                  if (dispatchProcessVisible || run.steps.some(step => step.kind === 'compaction')) {
                    return (
                      <ToolRunShell
                        key={segKey}
                        steps={run.steps}
                        isStreaming={m.isStreaming}
                        isTail={liveTurn ? run === tailRun : undefined}
                        idleMs={run === tailRun ? activity.idleMs : undefined}
                        agentSlotKey={agent?.slot === segKey ? segKey : undefined}
                        takeOverFrom={run === takeOver ? (turnStatusLabel ?? t('正在处理…')) : undefined}
                      />
                    );
                  }
                  if (run.tools.length > 0) {
                    return <ToolProgressInline key={segKey} message={m} toolCalls={run.tools} />;
                  }
                  return null;
                }

                if (suppressedIdx.has(segIdx)) return null;

                if (seg.type === 'tool') {
                  return null;
                }

                if (seg.type === 'plan' && seg.planData) {
                  const planData = seg.planData;
                  if (isForkedHistory(m)) return <MessageInheritedPlan key={segKey} plan={planData} />;
                  // Preview approval footer: confirm/discard buttons until a
                  // decision is made (typed "确认执行" still works as fallback —
                  // sendPlanMode marks the decision on that path too).
                  let previewFooter: ReactNode | undefined;
                  if (planData.mode === 'preview' && !isForkedHistory(m)) {
                    if (planData.decided === 'confirmed') {
                      previewFooter = (
                        <div className="jx-plan-footerTip jx-plan-footerTip--decided">
                          <CheckCircleFilled style={{ color: 'var(--color-success)' }} /> {t('已确认，开始执行')}
                        </div>
                      );
                    } else if (planData.decided === 'cancelled') {
                      previewFooter = (
                        <div className="jx-plan-footerTip jx-plan-footerTip--decided">
                          {t('已放弃此计划。可继续描述需求，重新生成计划。')}
                        </div>
                      );
                    } else if (planData.planId) {
                      previewFooter = (
                        <div className="jx-plan-approve">
                          <span className="jx-plan-approveHint">{t('确认后将按步骤执行此计划；也可回复文字修改需求。')}</span>
                          <div className="jx-plan-approveBtns">
                            <button
                              type="button"
                              className="jx-plan-approveBtn jx-plan-approveBtn--ghost"
                              onClick={() => onPlanDiscard(planData.planId!)}
                            >
                              {t('放弃')}
                            </button>
                            <button
                              type="button"
                              className="jx-plan-approveBtn jx-plan-approveBtn--primary"
                              onClick={() => onPlanConfirm()}
                            >
                              {t('确认执行')}
                            </button>
                          </div>
                        </div>
                      );
                    }
                  }
                  return (
                    <PlanCard
                      key={segKey}
                      mode={planData.mode}
                      title={planData.title}
                      description={planData.description}
                      steps={planData.steps}
                      completedSteps={planData.completedSteps}
                      totalSteps={planData.totalSteps}
                      resultText={planData.resultText}
                      isStreaming={m.isStreaming}
                      agentNameMap={planData.agentNameMap}
                      previewFooter={isForkedHistory(m) ? null : previewFooter}
                      cancelled={planData.cancelled}
                    />
                  );
                }

                if (seg.type === 'thinking') {
                  // OFF mode: thinking that wasn't folded into a tool run still
                  // renders as its inline summary.
                  const isActiveThinking = !!(m.isStreaming && !m.segments!.slice(segIdx + 1).some(s => s.type === 'text'));
                  return <ThinkingInline key={segKey} content={seg.content || ''} isActive={isActiveThinking} />;
                }

                if (seg.type === 'text') {
                  const textContent = seg.content || '';
                  if (!textContent && (!m.isStreaming || !isLastSeg)) return null;
                  const effectiveCitations = conversationCitations;
                  // OFF mode keeps the inline StreamWaitIndicator under the
                  // text bubble; ON mode handles waits inside the shell so
                  // we suppress the indicator entirely.
                  const showInlineWait = !dispatchProcessVisible && m.isStreaming && isLastSeg;
                  return (
                    <React.Fragment key={segKey}>
                      <div className={`jx-bubble ${m.role === 'user' ? 'user' : ''} jx-md ${m.isStreaming && isLastSeg ? 'streaming' : ''}`}>
                        {segIdx === 0 && renderUserQuote()}
                        {segIdx === 0 && renderChipBadges()}
                        {m.role === 'user' ? <UserMarkdownBlock className="jx-msgText" text={textContent} /> : <CitationMarkdownBlock
                          className="jx-msgText"
                          text={textContent}
                          isMarkdown
                          citations={effectiveCitations}
                          messageIsStreaming={m.isStreaming}
                          streamingSnapshot={m.inFlight?.textSnapshotOffset}
                          onCitationAction={handleCitationAction}
                        />}
                      </div>
                      {showInlineWait && (
                        <StreamWaitIndicator
                          signature={stallSignature}
                          suppressed={anyToolRunning(m.toolCalls || [])}
                          anchorTs={m.lastActivityTs}
                        />
                      )}
                    </React.Fragment>
                  );
                }
                return null;
              });

              if (tail) {
                rendered.push(
                  <TurnStatusIndicator
                    key={tailSlot}
                    startTs={tail.startTs}
                    label={tail.label}
                    clockAfterMs={tail.clockAfterMs}
                    agentSlotKey={tailSlot}
                  />,
                );
              }
              if (agent) {
                // The startup row (rendered before any segment exists) already played the
                // brand intro; the travelling mark only plays it if it is the first mark shown.
                rendered.push(<AgentCursor key={`${m.uid}-agent`} slotKey={agent.slot} state={agent.state} intro={!hasVisibleProgress} />);
              }

              return rendered;
            })()}
          </>
        ) : (
          /* Legacy rendering path */
          <>
            {m.toolCalls && m.toolCalls.length > 0 && dispatchProcessVisible && (
              <div className="jx-toolCallsList">
                <ToolRunShell
                  steps={m.toolCalls.map((tool, idx) => ({ kind: 'tool' as const, tool, key: `${m.uid}-legacy-${idx}` }))}
                  isStreaming={m.isStreaming}
                />
              </div>
            )}
            {m.thinking && m.thinking.length > 0 && chatMode !== 'fast' && (
              <div className="jx-thinkingSection">
                <div className="jx-sectionHeader">
                  <BulbOutlined className="jx-sectionIcon" />
                  <span className="jx-sectionTitle">{t('思考过程 ({n})', { n: m.thinking.length })}</span>
                </div>
                <div className="jx-thinkingList">
                  {m.thinking.map((think, idx) => renderThinkingBlock(think.content, `${m.uid}-think-${idx}`))}
                </div>
              </div>
            )}
            {m.isStreaming && !m.content ? (
              dispatchProcessVisible && !m.toolCalls?.length && !m.thinking?.some(block => block.content?.trim()) ? (
                <TurnStatusIndicator startTs={turnStartTs} label={turnStatusLabel} />
              ) : dispatchProcessVisible ? null : (
                <ThinkingInline content="" isActive={true} />
              )
            ) : (
            <div className={`jx-bubble ${m.role === 'user' ? 'user' : ''} jx-md ${m.isStreaming ? 'streaming' : ''}`}>
              {renderUserQuote()}
              {renderChipBadges()}
              {m.role === 'user' ? <UserMarkdownBlock className="jx-msgText" text={m.content} /> : <CitationMarkdownBlock
                className="jx-msgText"
                text={m.content}
                isMarkdown
                citations={conversationCitations}
                messageIsStreaming={m.isStreaming}
                streamingSnapshot={m.inFlight?.textSnapshotOffset}
                onCitationAction={handleCitationAction}
              />}
              {m.isStreaming && (
                <span className="jx-streamingIndicator" aria-hidden="true">
                  <span className="jx-streamingDot" /><span className="jx-streamingDot" /><span className="jx-streamingDot" />
                </span>
              )}
            </div>
            )}
          </>
        )}

  </>;
}
