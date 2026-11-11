import { AgentMark } from '../common/AgentMark';
import { TurnClock } from '../common/TurnClock';
import { RollingText } from '../common/RollingText';
import { t } from '../../i18n';

interface TurnStatusIndicatorProps {
  /**
   * What the wait is for. Defaults to the generic startup line. The vision
   * bridge reading an uploaded image passes its own label — several seconds of
   * network wait that would otherwise be indistinguishable from the model
   * thinking.
   */
  label?: string;
  /**
   * The reply's travelling agent mark is parked on this row: the slot carries
   * this id and stays empty. Without it the row draws its own mark.
   */
  agentSlotKey?: string;
  /** Start of the turn; when set, the whole-turn clock trails the label. */
  turnStartTs?: number;
}

/**
 * The tail row of a streaming reply: the agent's place whenever nothing else
 * owns the current moment — before anything has streamed, and in the pause
 * after text before the next step shows up (not while text streams: the words
 * are the progress signal then). A single line with the brand mark, so a wait
 * never reads as a frozen screen.
 * Once a real tool run starts, the run's own row takes over (and the
 * whole-turn clock moves there with it).
 */
export function TurnStatusIndicator({
  label,
  agentSlotKey,
  turnStartTs,
}: TurnStatusIndicatorProps) {
  return (
    <div className="jx-turnStatus jx-turnStatusRow" role="status" aria-live="polite">
      <span className="jx-agentSlot" data-agent-slot={agentSlotKey} aria-hidden="true">
        {agentSlotKey ? null : <AgentMark state="work" intro />}
      </span>
      <RollingText text={label ?? t('正在处理…')} shimmer className="jx-turnStatus-label jx-turnStatusText" />
      {turnStartTs != null && <TurnClock startTs={turnStartTs} />}
    </div>
  );
}
