import { useEffect, useState } from 'react';
import { formatClock } from '../../utils/elapsed';

interface ElapsedTimerProps {
  /** Start time in epoch milliseconds. */
  startTs: number;
  /** Extra class for styling the <span>. */
  className?: string;
}


/**
 * Live "elapsed since startTs" counter, formatted M:SS, ticking every second.
 * Used to give the user a sense of progress while the model/tool is working
 * alongside any available argument or result deltas.
 */
export function ElapsedTimer({ startTs, className }: ElapsedTimerProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const sec = Math.max(0, Math.floor((now - startTs) / 1000));
  return <span className={className ?? 'jx-elapsedTimer'}>{formatClock(sec)}</span>;
}
