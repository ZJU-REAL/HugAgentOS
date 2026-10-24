import { useEffect, useState } from 'react';

/**
 * Detects when a streaming source has gone silent for longer than `stallMs`.
 *
 * `signature` should change whenever the stream emits new content (text token,
 * tool call, segment, etc). When it stops changing for `stallMs` we flip to
 * `waiting = true` and report the timestamp the stall started, e.g. while the
 * LLM buffers a long tool-call payload server-side (MiniMax) before emitting it.
 *
 * `active` says whether there is a stream to watch at all, and it is not an
 * optimisation: hooks cannot be called conditionally, so every caller that
 * renders per message — which is what this one does — would otherwise hold a
 * 2Hz timer for every finished message in the conversation, forever. A long
 * history would then re-render itself twice a second with nothing streaming,
 * and each of those renders re-runs everything the message body computes.
 */
export function useStallDetector(
  signature: string | number,
  stallMs: number = 2500,
  anchorTs?: number,
  active: boolean = true,
): { waiting: boolean; since: number } {
  const [now, setNow] = useState(() => Date.now());
  const [lastChange, setLastChange] = useState(now);
  const [prevSig, setPrevSig] = useState(signature);

  if (prevSig !== signature) {
    setPrevSig(signature);
    setLastChange(now);
  }

  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [active]);

  // `anchorTs` (when provided) is a persisted, remount-stable timestamp of the
  // last stream activity — prefer it so the stall clock keeps counting from
  // the real start across session switches / page refreshes. Local `lastChange`
  // is the fallback for callers without a persisted anchor; it resets on mount.
  const since = anchorTs ?? lastChange;
  // Without a ticking clock `now` is whatever it was when the stream ended, so
  // the comparison below is meaningless once inactive — say so rather than
  // report a stall nobody is waiting on.
  const waiting = active && now - since >= stallMs;
  return { waiting, since };
}
