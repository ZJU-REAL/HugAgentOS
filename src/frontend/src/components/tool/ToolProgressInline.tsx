import { useState } from 'react';
import { RightOutlined } from '@ant-design/icons';
import { useChatStore } from '../../stores';
import { resolveToolDisplayName } from '../../utils/toolMeta';
import { useCollapseHeight } from '../../hooks/useCollapseHeight';
import { BrandLoader, ElapsedTimer } from '../common';
import { ToolCallRow } from './ToolCallRow';
import { computeEffectiveStatus } from './renderers/utils';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';

interface ToolProgressInlineProps {
  message: ChatMessage;
  /** Segment-level tool calls (subset) — if provided, only these are shown */
  toolCalls?: NonNullable<ChatMessage['toolCalls']>;
}

/**
 * Collapsed tool batch shown when dispatchProcessVisible is off.
 *
 * The header line is unchanged — brand loader + quiet summary text + timer.
 * Clicking it unfolds the step list **in place** instead of pushing a card into
 * the right-hand Canvas, so reading one tool call no longer costs the reader
 * the whole answer column. The body is ToolRunShell's `.jx-trs-body`, so an
 * expanded batch is the same rows at the same density in both modes.
 */
export function ToolProgressInline({ message, toolCalls }: ToolProgressInlineProps) {
  const toolDisplayNames = useChatStore((s) => s.toolDisplayNames);
  const [open, setOpen] = useState(false);
  // Rows carry a JSON.parse of their output; a transcript's worth of never-opened
  // folds should not pay for it, so the body only mounts once it is first opened.
  const [mounted, setMounted] = useState(false);
  const collapseRef = useCollapseHeight(open);
  const tools = toolCalls ?? message.toolCalls ?? [];
  if (tools.length === 0) return null;

  // Converted via computeEffectiveStatus (when the message isn't streaming, running is treated as success) —
  // reading tool.status directly would leave a leftover running state spinning forever after an abort/abnormal interruption.
  const isRunning = (tool: (typeof tools)[number]) =>
    computeEffectiveStatus(tool, message.isStreaming) === 'running';
  const anyRunning = tools.some(isRunning);
  const runningTs = tools
    .filter(t => isRunning(t) && typeof t.timestamp === 'number')
    .map(t => t.timestamp as number);
  const startTs = runningTs.length > 0 ? Math.min(...runningTs) : message.ts;
  const names = tools
    .map(t => resolveToolDisplayName(t, toolDisplayNames))
    .filter((v, i, a) => a.indexOf(v) === i)   // dedupe
    .slice(0, 3);
  const label = names.join('、') + (tools.length > 3 ? t('等{n}项', { n: tools.length }) : '');

  const toggle = () => { setMounted(true); setOpen(v => !v); };

  return (
    <div className="jx-inlineFold">
      <div className="jx-inlineSummary" role="button" tabIndex={0} aria-expanded={open} onClick={toggle}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } }}>
        <BrandLoader done={!anyRunning} label={anyRunning ? t('正在调用工具') : t('工具调用完成')} />
        {/* jx-anim-keep：减弱动效下流光不能被冻住——它是靠 background-clip:text 抠出来的字，
            停在任意一帧就变成一段忽深忽浅的灰，反而比动起来更难读（motion.css 的白名单） */}
        <span className={`jx-inlineSummaryText${anyRunning ? ' jx-inlineSummaryText--live jx-anim-keep' : ''}`}>
          {anyRunning ? t('正在调用 {label}', { label }) : t('已调用 {label}', { label })}
        </span>
        {/* 长工具（批量作业）的实时进度：这条摘要行在折叠态是用户唯一的信息源 */}
        {anyRunning && (() => {
          const note = tools.find(tc => isRunning(tc) && tc.progressNote)?.progressNote;
          return note ? <span className="jx-trs-note">{note}</span> : null;
        })()}
        {anyRunning && <ElapsedTimer startTs={startTs} className="jx-inlineSummaryTimer" />}
        <RightOutlined className="jx-inlineSummaryArrow" rotate={open ? 90 : 0} />
      </div>

      <div ref={collapseRef} className={`jx-collapse${open ? ' jx-collapse--open' : ''}`}>
        <div className="jx-trs-body">
          {mounted && tools.map((tool, idx) => (
            <ToolCallRow
              key={tool.id || `${message.ts}-tool-${idx}`}
              tool={tool}
              isStreaming={message.isStreaming}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
