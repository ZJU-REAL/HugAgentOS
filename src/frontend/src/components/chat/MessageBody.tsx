import React, { useCallback, type ReactNode } from 'react';
import { BulbOutlined, BulbFilled, DownOutlined, CheckCircleFilled } from '@ant-design/icons';
import { useShallow } from 'zustand/react/shallow';
import { groupExecutionRuns } from '../../utils/executionRuns';
import { resolveConversationCitations } from '../../utils/citations';
import { isForkedHistory } from '../../utils/forkHistory';
import { ToolRunShell } from '../tool/ToolRunShell';
import { ToolProgressInline } from '../tool/ToolProgressInline';
import { anyToolRunning } from '../tool/renderers/utils';
import { ThinkingInline } from './ThinkingInline';
import { StreamWaitIndicator } from './StreamWaitIndicator';
import { TurnStatusIndicator } from './TurnStatusIndicator';
import { InvocationBadges } from './InvocationBadges';
import { PlanCard } from './PlanCard';
import { MessageInheritedPlan } from './MessageInheritedPlan';
import { UserMarkdownBlock } from './UserMarkdownBlock';
import { CitationMarkdownBlock } from '../citation';
import { useChatStore, useUIStore } from '../../stores';
import { updatePlanApi } from '../../api';
import { markPlanDecision } from '../../hooks/usePlanMode';
import { useStallDetector } from '../../hooks/useStallDetector';
import { useMessageCitation } from './useMessageCitation';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';
interface MessageBodyProps {
  m: ChatMessage;
  messageIndex: number;
  currentChatId: string;
  send: (text?: string) => void;
}
export function MessageBody({ m, messageIndex, currentChatId, send }: MessageBodyProps) {
  const handleCitationAction = useMessageCitation(m);
  const expandedThinking = useChatStore((s) => s.expandedThinking);
  const toggleThinking = useChatStore((s) => s.toggleThinking);
  const chatMode = useChatStore((s) => s.chatMode);
  const dispatchProcessVisible = useUIStore((s) => s.dispatchProcessVisible);
  const chatMessages = useChatStore(
    useShallow((state) => (state.store.chats[currentChatId]?.messages ?? []).slice(0, messageIndex + 1)),
  );
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
  // Drives the "正在准备调用工具" pending step inside the ToolRunShell — the
  // Some LLM providers still buffer tool-call args server-side, so when the message is
  // streaming and goes silent (or backend has fired `tool_pending`) we want
  // *some* signal that work is still happening. Replaces the old free-floating
  // StreamWaitIndicator below the text bubble.
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
  // Anchor the stall clock to the message's persisted `lastActivityTs` so the
  // "正在准备调用工具…" timer keeps counting from the real start even after a
  // session switch or page refresh remounts this component.
  const stall = useStallDetector(stallSignature, 2500, m.lastActivityTs, !!m.isStreaming);
  const noToolRunning = !anyToolRunning(m.toolCalls || []);
  const pendingWaiting = !!m.isStreaming && noToolRunning && (!!m.toolPending || stall.waiting);

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
    return (
      <div className="jx-userQuote" title={m.quotedFollowUp.text}>
        <span className="jx-userQuoteLabel">{t('引用')}</span>
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
              // Group thinking + tool calls + tool-prepare waits into a single
              // "agent run" shell so the chat flow shows one card per phase
              // instead of separate thinking / tool-call / preparing entries.
              //
              // A run is a maximal contiguous sequence of {thinking, tool,
              // empty-text} segments. Non-empty text breaks the run. Pure
              // empty-text only chunks are absorbed but never anchor a run.
              //
              // OFF mode (dispatchProcessVisible=false) keeps the existing
              // ToolProgressInline for tool batches; pending state still
              // surfaces, but the unified shell only takes over in ON mode.
              const segs = m.segments!;
              const isEmptyText = (i: number): boolean => {
                const s = segs[i];
                return !!s && s.type === 'text' && !(s.content || '').trim();
              };
              const { runs, suppressedIdx } = groupExecutionRuns(m, dispatchProcessVisible);

              // Attach a pending step where the wait state should appear.
              // - In ON mode + an active run that runs to the end of segments
              //   (no text after) → append to that run's steps.
              // - In ON mode otherwise (no run yet, or finished with text) →
              //   render a separate "virtual" mini-shell below the last
              //   segment so the user sees *some* progress signal.
              // - In OFF mode → fall through to the per-text-segment
              //   StreamWaitIndicator (preserves existing inline behavior).
              const hasVisibleTextProgress = segs.some(s => s.type === 'text' && (s.content || '').trim().length > 0);
              // A thinking segment that is not folded into a run renders as its
              // own ThinkingInline below, so the reasoning is already on screen.
              // Counting only `text` here made the turn indicator sit under a
              // visibly streaming reasoning block for the whole pre-tool phase —
              // several seconds on any turn that calls a tool, which is exactly
              // where "深度拥抱中…" was never meant to appear. It stays for the
              // genuine dead window: after the bubble exists and before the
              // model has emitted anything at all.
              const hasVisibleThinkingProgress = segs.some(
                s => s.type === 'thinking' && (s.content || '').trim().length > 0,
              );
              const startupPending =
                !!m.isStreaming &&
                dispatchProcessVisible &&
                runs.length === 0 &&
                !hasVisibleTextProgress &&
                !hasVisibleThinkingProgress;
              const showPendingInShell = pendingWaiting && dispatchProcessVisible;
              let virtualPending: { startTs: number; key: string } | null = null;
              if (showPendingInShell || startupPending) {
                const lastRun = runs.length ? runs[runs.length - 1] : null;
                const lastRunReachesEnd = !!lastRun && (() => {
                  for (let j = lastRun.endIdx + 1; j < segs.length; j++) {
                    if (!isEmptyText(j)) return false;
                  }
                  return true;
                })();
                if (lastRun && lastRunReachesEnd) {
                  lastRun.steps.push({ kind: 'pending', startTs: stall.since, key: `${m.uid}-pending` });
                } else {
                  virtualPending = { startTs: stall.since, key: `${m.uid}-pending-virtual` };
                }
              }

              const runByAnchor = new Map<number, (typeof runs)[number]>();
              runs.forEach((r) => runByAnchor.set(r.anchor, r));

              const rendered = segs.map((seg, segIdx) => {
                const isLastSeg = segIdx === segs.length - 1;
                const segKey = `${m.uid}-seg-${segIdx}`;

                if (runByAnchor.has(segIdx)) {
                  const run = runByAnchor.get(segIdx)!;
                  if (dispatchProcessVisible) {
                    return (
                      <ToolRunShell
                        key={segKey}
                        steps={run.steps}
                        isStreaming={m.isStreaming}
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
                  if (!textContent && !m.isStreaming) return null;
                  const effectiveCitations = resolveConversationCitations(textContent, m.citations ?? [], chatMessages, m.uid);
                  // OFF mode keeps the inline StreamWaitIndicator under the
                  // text bubble; ON mode handles waits inside the shell so
                  // we suppress the indicator entirely.
                  const showInlineWait = !dispatchProcessVisible && m.isStreaming && isLastSeg;
                  return (
                    <React.Fragment key={segKey}>
                      <div className={`jx-bubble ${m.role === 'user' ? 'user' : ''} ${m.role === 'user' || m.isMarkdown ? 'jx-md' : ''} ${m.isStreaming && isLastSeg ? 'streaming' : ''}`}>
                        {segIdx === 0 && renderUserQuote()}
                        {segIdx === 0 && renderChipBadges()}
                        {m.role === 'user' ? <UserMarkdownBlock className="jx-msgText" text={textContent} /> : <CitationMarkdownBlock
                          className="jx-msgText"
                          text={textContent}
                          isMarkdown={m.isMarkdown ?? false}
                          citations={effectiveCitations}
                          messageIsStreaming={m.isStreaming}
                          onCitationAction={handleCitationAction}
                        />}
                      </div>
                      {showInlineWait && (
                        <StreamWaitIndicator
                          signature={stallSignature}
                          forceWait={!!m.toolPending}
                          suppressed={anyToolRunning(m.toolCalls || [])}
                          anchorTs={m.lastActivityTs}
                        />
                      )}
                    </React.Fragment>
                  );
                }
                return null;
              });

              if (virtualPending) {
                // No real tool run to attach to (turn start, or the model went
                // silent again after finishing a run + text) — a light turn-level
                // shimmer label beats a hollow "执行中" shell card here: we don't
                // even know yet whether a tool will be called.
                rendered.push(
                  <TurnStatusIndicator
                    key={virtualPending.key}
                    startTs={virtualPending.startTs}
                    label={turnStatusLabel}
                  />,
                );
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
              dispatchProcessVisible ? (
                <TurnStatusIndicator startTs={stall.since} label={turnStatusLabel} />
              ) : (
                <ThinkingInline content="" isActive={true} />
              )
            ) : (
            <div className={`jx-bubble ${m.role === 'user' ? 'user' : ''} ${m.role === 'user' || m.isMarkdown ? 'jx-md' : ''} ${m.isStreaming ? 'streaming' : ''}`}>
              {renderUserQuote()}
              {renderChipBadges()}
              {m.role === 'user' ? <UserMarkdownBlock className="jx-msgText" text={m.content} /> : <CitationMarkdownBlock
                className="jx-msgText"
                text={m.content}
                isMarkdown={m.isMarkdown ?? false}
                citations={resolveConversationCitations(m.content, m.citations ?? [], chatMessages, m.uid)}
                messageIsStreaming={m.isStreaming}
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
