import { useEffect, useState } from 'react';

/**
 * Detects when a streaming source has gone silent for longer than `stallMs`.
 *
 * `signature` should change whenever the stream emits new content (text token,
 * tool call, segment, etc). When it stops changing for `stallMs` we flip to
 * `waiting = true` and report the timestamp the stall started. This is the
 * frontend safety net for the case where the LLM buffers a long tool-call
 * payload server-side (e.g. MiniMax) and the backend's `tool_pending` event
 * either hasn't fired or fires late.
 */
export function useStallDetector(
  signature: string | number,
  stallMs: number = 2500,
  anchorTs?: number,
): { waiting: boolean; since: number } {
  const [now, setNow] = useState(() => Date.now());
  const [lastChange, setLastChange] = useState(now);
  const [prevSig, setPrevSig] = useState(signature);

  if (prevSig !== signature) {
    setPrevSig(signature);
    setLastChange(now);
  }

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, []);

  // `anchorTs` (when provided) is a persisted, remount-stable timestamp of the
  // last stream activity — prefer it so the stall clock keeps counting from
  // the real start across session switches / page refreshes. Local `lastChange`
  // is the fallback for callers without a persisted anchor; it resets on mount.
  const since = anchorTs ?? lastChange;
  const waiting = now - since >= stallMs;
  return { waiting, since };
}
