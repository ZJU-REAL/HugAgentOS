import { formatTurnSeconds } from '../../utils/elapsed';
import { ElapsedTimer } from './ElapsedTimer';

/**
 * The reply's single clock, shown as "· 36秒" at the end of whichever status
 * row is live right now (startup, a running tool batch, the wait after text).
 * It always counts from the start of the turn, so moving between rows never
 * resets it; individual tools and waits carry no clocks of their own.
 */
export function TurnClock({ startTs }: { startTs: number }) {
  return (
    <>
      <span className="jx-turnClock-sep" aria-hidden="true">·</span>
      <ElapsedTimer startTs={startTs} format={formatTurnSeconds} className="jx-turnClock" />
    </>
  );
}
