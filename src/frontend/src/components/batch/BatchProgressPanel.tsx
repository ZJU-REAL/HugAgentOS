import { useMemo, useRef, useState } from 'react';
import { Progress, Tag, Card, Empty, Button, Space, message } from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import { t } from '../../i18n';
import { CheckCircleFilled, CloseCircleFilled, LoadingOutlined } from '@ant-design/icons';
import { EASE } from '../../utils/motionTokens';
import { useBatchStore, useUIStore } from '../../stores';
import { cancelBatchPlan } from '../../api';
import { CitationMarkdownBlock } from '../citation';
import { ArtifactCardList, type ArtifactRef } from '../chat/ArtifactCardList';
import { ToolCallRow } from '../tool/ToolCallRow';
import { ToolProgressInline } from '../tool/ToolProgressInline';
import { buildHistorySegments } from '../../utils/segments';
import type { ToolCall, ChatMessage } from '../../types';

interface Props {
  planId: string;
}

const STATUS_LABEL: Record<string, string> = {
  awaiting_confirm: t('等待确认'),
  running: t('执行中'),
  done: t('已完成'),
  cancelled: t('已取消'),
  error: t('执行异常'),
};

type AntdProgressStatus = 'exception' | 'normal' | 'active' | 'success';

function deriveProgressStatus(planStatus: string, running: boolean): AntdProgressStatus {
  if (planStatus === 'error') return 'exception';
  if (planStatus === 'cancelled') return 'normal';
  return running ? 'active' : 'success';
}

/** Detect if a chunk of text uses any markdown syntax we render specially. */
function looksLikeMarkdown(text: string): boolean {
  if (!text) return false;
  return (
    text.includes('\n')
    || /^\s*#{1,6}\s/m.test(text)
    || /^\s*[-*+]\s/m.test(text)
    || /^\s*\d+\.\s/m.test(text)
    || text.includes('```')
    || /\*\*[^*]+\*\*/.test(text)
    || /!\[[^\]]*\]\([^)]+\)/.test(text)   // image
    || /\[[^\]]+\]\([^)]+\)/.test(text)    // link
    || /\|.+\|/.test(text)                  // table row
  );
}

/** Render a single batch item using the same primitives the chat list does:
 *  CitationMarkdownBlock for the markdown body (includes mermaid charts +
 *  inline images), buildHistorySegments to peel off any embedded `<think>`
 *  blocks, plus the existing tool-result handling for diverse outputs.
 */
function BatchItemBubble({ item }: {
  item: ReturnType<typeof useBatchStore.getState>['plans'][string]['results'][number];
}) {
  // Same global toggle the chat bubble reads — keeps the batch panel and
  // regular conversation in sync re: full tool cards vs collapsed summary.
  const dispatchProcessVisible = useUIStore((s) => s.dispatchProcessVisible);
  const content = item.status === 'success' ? (item.content || '') : '';
  // Strip any embedded `<think>` blocks the same way chat history does, then
  // detect markdown for the bubble class.
  const { visibleText, isMarkdown } = useMemo(() => {
    const { cleanContent } = buildHistorySegments(content, []);
    const text = cleanContent || content;
    return { visibleText: text, isMarkdown: looksLikeMarkdown(text) };
  }, [content]);

  return (
    <Card
      size="small"
      title={
        <Space size="small">
          {item.status === 'success' ? (
            <CheckCircleFilled style={{ color: '#52c41a' }} />
          ) : (
            <CloseCircleFilled style={{ color: '#cf1322' }} />
          )}
          <span>{t('第 {n} 项', { n: item.index + 1 })}</span>
          {item.retry_count > 0 && (
            <Tag color="orange">{t('重试 {n} 次', { n: item.retry_count })}</Tag>
          )}
          {item.status === 'skipped' && <Tag color="red">{t('已跳过')}</Tag>}
        </Space>
      }
    >
      {item.status === 'success' ? (
        <Space direction="vertical" size="small" style={{ width: '100%' }}>
          {item.tool_calls && item.tool_calls.length > 0 && (
            <ToolGroup
              rawTools={item.tool_calls}
              dispatchProcessVisible={dispatchProcessVisible}
            />
          )}
          {visibleText && (
            <div className={`jx-bubble jx-batch-item-bubble${isMarkdown ? ' jx-md' : ''}`}>
              <CitationMarkdownBlock
                text={visibleText}
                isMarkdown={isMarkdown}
                citations={item.citations || []}
                messageIsStreaming={false}
              />
            </div>
          )}
          {/* Per-item output documents (Word/Excel/PPT/image, etc.) — the
              backend pins workspace files into item.artifacts on every batch
              item, so this surfaces each row's generated documents the same
              way the regular chat bubble does. */}
          {Array.isArray(item.artifacts) && item.artifacts.length > 0 && (
            <ArtifactCardList artifacts={item.artifacts as unknown as ArtifactRef[]} />
          )}
        </Space>
      ) : (
        <div style={{ color: '#cf1322' }}>{item.error || t('执行失败')}</div>
      )}
    </Card>
  );
}

/** Normalize the backend's snake_case tool-call shape to the frontend
 *  ToolCall shape, then render via the SAME components the regular chat
 *  bubble uses (ToolCallRow when "show dispatch process" is on, ToolProgressInline
 *  summary otherwise). */
function ToolGroup({ rawTools, dispatchProcessVisible }: {
  rawTools: NonNullable<ReturnType<typeof useBatchStore.getState>['plans'][string]['results'][number]['tool_calls']>;
  dispatchProcessVisible: boolean;
}) {
  const tools: ToolCall[] = rawTools.map((raw, idx) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const r = raw as any;
    return {
      id: r.tool_id || r.id || `tc-${idx}`,
      name: r.tool_name || r.name || t('工具调用'),
      displayName: r.tool_display_name || r.displayName,
      input: r.tool_args ?? r.input,
      output: r.output ?? r.result,
      status: (r.status as 'pending' | 'running' | 'success' | 'error' | undefined) || 'success',
      timestamp: r.timestamp,
    };
  });

  if (dispatchProcessVisible) {
    return (
      <>
        {tools.map((t, idx) => (
          <ToolCallRow key={t.id || `tc-${idx}`} tool={t} isStreaming={false} />
        ))}
      </>
    );
  }

  // Hidden mode — single inline summary row, same as chat bubble.
  // ToolProgressInline expects a ChatMessage; we pass a minimal stub.
  const fakeMsg: ChatMessage = {
    role: 'assistant',
    content: '',
    isMarkdown: false,
    ts: Date.now(),
    toolCalls: tools,
  };
  return <ToolProgressInline message={fakeMsg} toolCalls={tools} />;
}

/** Renders inline inside the assistant message bubble whenever a batch
 *  execution is associated with that turn. Reuses chat-list rendering
 *  primitives (CitationMarkdownBlock) so each item supports markdown,
 *  mermaid, embedded images, code blocks, citations, etc. — same set of
 *  output formats the regular chat bubble handles. Updates live as new
 *  `batch_item_done` events arrive.
 */
export function BatchProgressPanel({ planId }: Props) {
  const plan = useBatchStore((s) => s.plans[planId]);
  const cancel = useBatchStore((s) => s.cancel);
  const disconnectStream = useBatchStore((s) => s.disconnectStream);

  // ── History exemption (hydratePlan / remount on chat switch) ─────────────
  // Snapshot how many results and which status existed at mount (same
  // bornRunning pattern as ShellTimer): only items appended *after* mount on
  // a live (running) plan animate in, and the status Tag only settles when
  // the status actually flips post-mount. Hydrated/completed plans render
  // fully static.
  const [mountSnapshot] = useState(() => ({
    count: plan?.results.length ?? 0,
    status: plan?.status ?? null,
  }));

  // Tail boundary for "was stream-appended while running" (only grows, never shrinks): only items in the
  // [preMounted, liveTail) range enter with motion.div; the rest (possibly hundreds of hydrated items) render as bare divs,
  // never instantiating a motion visual element. Freeze the boundary with a ref so already-animated items after the run ends
  // keep their motion elements, avoiding a full-list remount from div/motion switching (which would drop tool-row expanded state).
  const liveTailRef = useRef(mountSnapshot.count);
  if (plan?.status === 'running' && plan.results.length > liveTailRef.current) {
    liveTailRef.current = plan.results.length;
  }

  if (!plan) return null;

  const total = plan.meta.total || 0;
  const done = plan.results.length;
  const success = plan.results.filter((r) => r.status === 'success').length;
  const failed = plan.results.filter((r) => r.status === 'skipped').length;
  const running = plan.status === 'running';
  const preMounted = mountSnapshot.count;
  const statusChanged = mountSnapshot.status !== null && plan.status !== mountSnapshot.status;
  // Live session = animations allowed; a hydrated, already-finished plan
  // (mounted with its final status, no new results) stays still.
  const animEnabled = running || statusChanged;

  const handleCancel = async () => {
    try {
      await cancelBatchPlan(planId);
      // Abort the SSE fetch immediately so we stop receiving new events
      // even if the backend's current item hasn't finished yet.
      disconnectStream(planId);
      cancel(planId);
      message.success(t('已请求取消批量任务'));
    } catch (e) {
      message.error(t('取消失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const tagColor =
    running ? 'processing' : plan.status === 'error' ? 'red' : 'default';

  return (
    <div className="jx-batch-panel" style={{ margin: '12px 0' }}>
      <Space size="small" style={{ width: '100%', justifyContent: 'space-between', marginBottom: 8 }}>
        <Space size="small">
          <span style={{ fontWeight: 600 }}>{t('批量执行')}</span>
          {/* keyed remount → settle animation when status flips; static on hydrate */}
          <Tag
            key={animEnabled ? plan.status : 'tag'}
            className={statusChanged ? 'jx-anim-statusIn' : undefined}
            color={tagColor}
          >
            {STATUS_LABEL[plan.status] || plan.status}
          </Tag>
          {/* stats numbers keyed by value fade in; static on hydrate */}
          <span
            key={animEnabled ? `${done}-${success}-${failed}` : 'stats'}
            className={animEnabled ? 'jx-anim-fadeIn' : undefined}
            style={{ color: '#666', fontSize: 12 }}
          >
            {t('{done}/{total}（成功 {success}，跳过 {failed}）', { done, total, success, failed })}
          </span>
        </Space>
        {running && (
          <Button size="small" danger onClick={handleCancel}>
            {t('取消')}
          </Button>
        )}
      </Space>

      <Progress
        percent={total > 0 ? Math.round((done / total) * 100) : 0}
        status={deriveProgressStatus(plan.status, running)}
        size="small"
      />

      {plan.errorMsg && (
        <div style={{ color: '#cf1322', fontSize: 12, marginTop: 6 }}>
          {t('错误：{msg}', { msg: plan.errorMsg })}
        </div>
      )}

      <Space direction="vertical" size="small" style={{ width: '100%', marginTop: 12 }}>
        {plan.results.length === 0 && !running && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('暂无结果')} />
        )}

        {/* Result card entrance: only tail items stream-appended after mount use motion.div animation;
            items already present on hydrate/remount render as bare divs (static). No full-list layout (possibly hundreds). */}
        {plan.results.map((item, idx) => (
          idx >= preMounted && idx < liveTailRef.current ? (
            <motion.div
              key={item.index}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, ease: EASE.brandOut }}
            >
              <BatchItemBubble item={item} />
            </motion.div>
          ) : (
            <div key={item.index}>
              <BatchItemBubble item={item} />
            </div>
          )
        ))}

        <AnimatePresence>
          {running && plan.results.length < total && (
            <motion.div
              key="batch-placeholder"
              layout="position"
              exit={{ opacity: 0, transition: { duration: 0.1, ease: EASE.exit } }}
            >
              <Card size="small">
                <Space>
                  <LoadingOutlined />
                  {/* Placeholder text flip: old item fades out over 0.1s → new item floats up and fades in over 0.15s */}
                  <AnimatePresence mode="wait" initial={false}>
                    <motion.span
                      key={plan.results.length}
                      style={{ display: 'inline-block' }}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, transition: { duration: 0.1, ease: EASE.exit } }}
                      transition={{ duration: 0.15, ease: EASE.standard }}
                    >
                      {t('正在处理第 {n} 项…', { n: plan.results.length + 1 })}
                    </motion.span>
                  </AnimatePresence>
                </Space>
              </Card>
            </motion.div>
          )}
        </AnimatePresence>
      </Space>
    </div>
  );
}
