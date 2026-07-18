import { useEffect, useState } from 'react';

interface ElapsedTimerProps {
  /** Start time in epoch milliseconds. */
  startTs: number;
  /** Extra class for styling the <span>. */
  className?: string;
}

function fmt(totalSec: number): string {
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * Live "elapsed since startTs" counter, formatted M:SS, ticking every second.
 * Used to give the user a sense of progress while the model/tool is working
 * (the underlying model does not stream tool arguments, so there is no
 * finer-grained signal available).
 */
export function ElapsedTimer({ startTs, className }: ElapsedTimerProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const sec = Math.max(0, Math.floor((now - startTs) / 1000));
  return <span className={className ?? 'jx-elapsedTimer'}>{fmt(sec)}</span>;
}
