import { useEffect, useState } from 'react';
import type { ToolCall } from '../../types';
import { BrandLoader } from '../common';
import { ToolCallRow } from './ToolCallRow';
import { ThinkingStepRow } from './ThinkingStepRow';
import { PendingStepRow } from './PendingStepRow';
import { computeEffectiveStatus } from './renderers/utils';
import { useDelayedFlag } from '../../hooks/useDelayedFlag';
import { t } from '../../i18n';

/** Aggregate status of a contiguous step batch. */
type ShellStatus = 'running' | 'success' | 'error';

/** Threshold before a running batch auto-expands; avoids open→close flicker. */
const AUTO_OPEN_DELAY_MS = 800;
const AUTO_OPEN_MIN_VISIBLE_MS = 500;

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
 * While running it ticks live from the batch start (the model does not stream
 * tool args, so a wall-clock counter is the only progress signal available).
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
  /** Keep auto-open active while the assistant is still between tool work and answer text. */
  holdOpenUntilText?: boolean;
}

/**
 * Minimal collapsible shell wrapping a contiguous batch of steps.
 *
 * Running  → expanded, header shows "In progress · M min SS sec" with a ticking timer.
 * Done     → collapses to a single "Done · M min SS sec" line; the user can click
 *            to re-expand. Status is conveyed by icon shape, not colour, to
 *            keep the run monochrome and quiet in the chat flow.
 *
 * Thinking segments adjacent to a tool batch are folded in as additional
 * steps so the user sees one unified "agent run" card instead of separate
 * "thinking process / tool call" entries in the message flow.
 */
export function ToolRunShell({ steps, isStreaming, holdOpenUntilText }: ToolRunShellProps) {
  const [mountTs] = useState(() => Date.now());

  const tools = steps.flatMap((s) => (s.kind === 'tool' ? [s.tool] : []));
  const toolStatuses = tools.map((t) => computeEffectiveStatus(t, isStreaming));
  const anyToolRunning = toolStatuses.includes('running');
  const hasSettledTool = toolStatuses.some((s) => s !== 'running');
  const anyThinkingActive = steps.some((s) => s.kind === 'thinking' && s.active);
  const anyPending = steps.some((s) => s.kind === 'pending');
  const running = anyToolRunning || anyThinkingActive || anyPending;
  const status: ShellStatus = running
    ? 'running'
    : toolStatuses.includes('error')
      ? 'error'
      : 'success';

  const tsList = tools
    .map((t) => t.timestamp)
    .filter((t): t is number => typeof t === 'number');
  const startTs = tsList.length ? Math.min(...tsList) : mountTs;
  const endTs = tsList.length ? Math.max(...tsList) : mountTs;

  const [override, setOverride] = useState<boolean | null>(null);
  // Keep the auto-open window alive across tool_result → next tool_call gaps.
  // It closes when real answer text starts, so chained tools do not bounce
  // between expanded/collapsed states while the model is still working.
  const autoOpen = useDelayedFlag(running || !!holdOpenUntilText, {
    showAfter: AUTO_OPEN_DELAY_MS,
    minVisible: AUTO_OPEN_MIN_VISIBLE_MS,
  });
  const keepOpenBetweenTools = !!holdOpenUntilText && hasSettledTool;
  const open = override ?? (autoOpen || keepOpenBetweenTools);

  const title =
    running ? t('执行中') : status === 'error' ? t('已完成（含失败）') : t('已完成');

  // History / non-streaming renders are static: the `--static` modifier kills
  // the shell + step-row + title entrance animations (see tool.css), so
  // switching chats or reloading never replays the whole run card.
  return (
    <div className={`jx-trs jx-trs--${status}${isStreaming ? '' : ' jx-trs--static'}`}>
      <button
        type="button"
        className={`jx-trs-head${open ? ' jx-trs-head--open' : ''}`}
        aria-expanded={open}
        onClick={() => setOverride(!open)}
      >
        <span className={`jx-trs-mark jx-trs-mark--${status}`} aria-hidden="true">
          {status === 'error' ? (
            <svg viewBox="0 0 16 16" width="14" height="14">
              <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
            </svg>
          ) : (
            <BrandLoader
              size={18}
              done={status === 'success'}
              label={status === 'success' ? t('已完成') : t('执行中')}
            />
          )}
        </span>
        {/* keyed remount → 0.15s fade when In progress ↔ Done flips */}
        <span key={title} className="jx-trs-title">{title}</span>
        <ShellTimer startTs={startTs} endTs={endTs} running={running} />
        <span className="jx-trs-steps">{t('{n} 个步骤', { n: steps.length })}</span>
        <span className={`jx-trs-chev${open ? ' jx-trs-chev--open' : ''}`} aria-hidden="true" />
      </button>

      <div className={`jx-trs-bodyWrap${open ? ' jx-trs-bodyWrap--open' : ''}`}>
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
