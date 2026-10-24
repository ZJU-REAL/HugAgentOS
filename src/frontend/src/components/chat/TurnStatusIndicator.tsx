import { useEffect, useState } from 'react';
import { ElapsedTimer } from '../common';
import { AgentMark } from '../common/AgentMark';
import { RollingText } from '../common/RollingText';
import { t } from '../../i18n';

interface TurnStatusIndicatorProps {
  /** Wall-clock ms the current wait began — anchors the elapsed clock. */
  startTs: number;
  /**
   * What the wait is for. Defaults to the generic startup line. The vision
   * bridge reading an uploaded image passes its own label — several seconds of
   * network wait that would otherwise be indistinguishable from the model
   * thinking.
   */
  label?: string;
  /** The clock only appears once the wait has clearly gone on a while. */
  clockAfterMs?: number;
  /**
   * The reply's travelling agent mark is parked on this row: the slot carries
   * this id and stays empty. Without it the row draws its own mark.
   */
  agentSlotKey?: string;
}

/** Short waits keep the bare label; the clock only appears once the turn has clearly been running a while. */
const CLOCK_AFTER_MS = 15_000;

/**
 * The tail row of a streaming reply: the agent's place whenever nothing else
 * owns the current moment — before anything has streamed, and in the pause
 * after text before the next step shows up (not while text streams: the words
 * are the progress signal then). A single line with the brand mark, so a wait
 * never reads as a frozen screen.
 * Once a real tool run starts, the run's own row takes over.
 */
export function TurnStatusIndicator({
  startTs,
  label,
  clockAfterMs = CLOCK_AFTER_MS,
  agentSlotKey,
}: TurnStatusIndicatorProps) {
  // One timeout reveals the clock; ElapsedTimer does the ticking from there.
  const [showClock, setShowClock] = useState(false);
  useEffect(() => {
    const id = window.setTimeout(() => setShowClock(true), Math.max(0, clockAfterMs - (Date.now() - startTs)));
    return () => window.clearTimeout(id);
  }, [startTs, clockAfterMs]);

  return (
    <div className="jx-turnStatus jx-turnStatusRow" role="status" aria-live="polite">
      <span className="jx-agentSlot" data-agent-slot={agentSlotKey} aria-hidden="true">
        {agentSlotKey ? null : <AgentMark state="work" intro />}
      </span>
      <RollingText text={label ?? t('正在处理…')} shimmer className="jx-turnStatus-label jx-turnStatusText" />
      {showClock && <ElapsedTimer startTs={startTs} className="jx-turnStatus-clock" />}
    </div>
  );
}
