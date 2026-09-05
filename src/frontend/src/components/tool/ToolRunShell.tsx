import { useEffect, useState } from 'react';
import type { ToolCall } from '../../types';
import { BrandLoader } from '../common';
import { ToolCallRow } from './ToolCallRow';
import { ThinkingStepRow } from './ThinkingStepRow';
import { PendingStepRow } from './PendingStepRow';
import { computeEffectiveStatus } from './renderers/utils';
import { t } from '../../i18n';
import { getDoneActionLabel, getRunningActionLabel } from '../../utils/toolActionLabel';
import { resolveToolDisplayName } from '../../utils/toolMeta';
import { useChatStore } from '../../stores';
import { useCollapseHeight } from '../../hooks/useCollapseHeight';

/** Aggregate status of a contiguous step batch. */
type ShellStatus = 'running' | 'success';


/**
 * A single entry in the shell timeline. Tool calls, thinking blocks, and
 * tool-call prepare waits all render as steps in stream order, so the user
 * sees one combined "agent run" card instead of several inline indicators.
 */
export type ShellStep =
  | { kind: 'tool'; tool: ToolCall; key: string }
  | { kind: 'thinking'; content: string; active: boolean; key: string }
  | { kind: 'pending'; startTs: number; key: string };

function formatDuration(totalSec: number): string {
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m <= 0) return t('{n}秒', { n: s });
  return t('{m}分{s}秒', { m, s: String(s).padStart(2, '0') });
}

/**
 * Total-elapsed counter for a step batch.
 *
 * While running it ticks live from the batch start alongside streamed tool
 * arguments and results when the selected model/provider exposes them.
 * Once done it shows a stable span derived from the first→last tool
 * timestamps, so a reloaded/historical message renders the same value every
 * time instead of drifting with a frozen wall clock.
 */
function ShellTimer({
  startTs,
  endTs,
  running,
}: {
  startTs: number;
  endTs: number;
  running: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  const [bornRunning] = useState(running);

  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [running]);

  const sec = bornRunning
    ? Math.max(0, Math.floor((now - startTs) / 1000))
    : Math.max(0, Math.floor((endTs - startTs) / 1000));

  if (!running && sec === 0) return null;
  // One-shot settle scale the moment a live run completes; bornRunning keeps
  // reloaded/historical messages static (they mount with running=false).
  const settled = bornRunning && !running;
  return (
    <>
      <span className="jx-trs-sep">·</span>
      <span className={`jx-trs-dur${settled ? ' jx-trs-dur--settled' : ''}`}>
        {formatDuration(sec)}
      </span>
    </>
  );
}

interface ToolRunShellProps {
  /** Mixed steps (tool calls + thinking) belonging to one contiguous batch. */
  steps: ShellStep[];
  isStreaming?: boolean;
}

/**
 * Minimal collapsible shell wrapping a contiguous batch of steps.
 *
 * 始终折叠成一行，点击才展开——运行中也不例外。那一行报的是实际动作
 * （"正在执行命令" → "运行了命令"）加耗时、步骤数，信息量足够，不必靠展开
 * 让用户确认它还活着。状态只靠图标形状表达，不用颜色，保持流里的安静。
 *
 * Thinking segments adjacent to a tool batch are folded in as additional
 * steps so the user sees one unified "agent run" card instead of separate
 * "thinking process / tool call" entries in the message flow.
 */
export function ToolRunShell({ steps, isStreaming }: ToolRunShellProps) {
  const [mountTs] = useState(() => Date.now());
  // 过程默认收起，跑起来也不自动展开——折叠那一行的文案已经说明了在做什么。
  const [open, setOpen] = useState(false);
  const toolDisplayNames = useChatStore((s) => s.toolDisplayNames);
  const collapseRef = useCollapseHeight(open);

  const tools = steps.flatMap((s) => (s.kind === 'tool' ? [s.tool] : []));
  const toolStatuses = tools.map((t) => computeEffectiveStatus(t, isStreaming));
  const anyToolRunning = toolStatuses.includes('running');
  const anyThinkingActive = steps.some((s) => s.kind === 'thinking' && s.active);
  const anyPending = steps.some((s) => s.kind === 'pending');
  const running = anyToolRunning || anyThinkingActive || anyPending;
  const status: ShellStatus = running ? 'running' : 'success';

  const tsList = tools
    .map((t) => t.timestamp)
    .filter((t): t is number => typeof t === 'number');
  const startTs = tsList.length ? Math.min(...tsList) : mountTs;
  const endTs = tsList.length ? Math.max(...tsList) : mountTs;

  // 折叠态下这行是用户唯一看得到的信息，所以报的是实际动作而不是"执行中/已完成"：
  // 跑的时候跟着当前那个工具走，收尾后按这一批用过的工具类型给结论。
  // MCP 连接器的名字是会话期动态下发的，先解析好再交给文案表——没登记在表里的
  // 工具就靠这个名字自报家门。
  const labeled = tools.map((tc) => ({
    name: tc.name,
    displayName: resolveToolDisplayName(tc, toolDisplayNames),
  }));
  const runningTool = labeled.find((_, i) => toolStatuses[i] === 'running');
  const title = running
    ? (runningTool
        ? getRunningActionLabel(runningTool)
        : anyThinkingActive ? t('正在思考') : t('执行中'))
    : getDoneActionLabel(labeled);

  // 折叠态下这行是唯一的进度来源。批量作业（run_job）会把一轮卡在同一个工具里几十分钟，
  // 步骤数一个都不会涨——不把里面那张卡的实时进度顶到头上，用户看到的就是"执行中 · 5 个步骤"
  // 一直转圈，除了刷新页面没有别的办法判断它到底跑没跑完。
  const runningNote = running
    ? tools.find((tc, i) => toolStatuses[i] === 'running' && tc.progressNote)?.progressNote || ''
    : '';

  // History / non-streaming renders are static: the `--static` modifier kills
  // the shell + step-row + title entrance animations (see tool.css), so
  // switching chats or reloading never replays the whole run card.
  return (
    <div className={`jx-trs jx-trs--${status}${isStreaming ? '' : ' jx-trs--static'}`}>
      <button
        type="button"
        className={`jx-trs-head${open ? ' jx-trs-head--open' : ''}`}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className={`jx-trs-mark jx-trs-mark--${status}`} aria-hidden="true">
          <BrandLoader
            size={18}
            done={status === 'success'}
            label={status === 'success' ? t('已完成') : t('执行中')}
          />
        </span>
        {/* keyed remount → 0.15s fade when In progress ↔ Done flips */}
        <span key={title} className={`jx-trs-title${running ? ' jx-trs-title--live' : ''}`}>{title}</span>
        <ShellTimer startTs={startTs} endTs={endTs} running={running} />
        {runningNote && <span className="jx-trs-note">{runningNote}</span>}
        {/* 折叠态下用户最关心的是"到底动了多少次手"，所以有工具就直接报工具数；
            只有纯思考、没有工具的批次才退回步骤数。 */}
        <span className="jx-trs-steps">
          {tools.length > 0
            ? t('{n} 个工具', { n: tools.length })
            : t('{n} 个步骤', { n: steps.length })}
        </span>
        <span className={`jx-trs-chev${open ? ' jx-trs-chev--open' : ''}`} aria-hidden="true" />
      </button>

      <div
        ref={collapseRef}
        className={`jx-trs-bodyWrap${open ? ' jx-trs-bodyWrap--open' : ''}`}
      >
        <div className="jx-trs-body">
          {steps.map((step) => {
            if (step.kind === 'tool') {
              return <ToolCallRow key={step.key} tool={step.tool} isStreaming={isStreaming} />;
            }
            if (step.kind === 'thinking') {
              return <ThinkingStepRow key={step.key} content={step.content} active={step.active} />;
            }
            return <PendingStepRow key={step.key} startTs={step.startTs} />;
          })}
        </div>
      </div>
    </div>
  );
}
