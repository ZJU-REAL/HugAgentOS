import { useEffect, useState } from 'react';

/**
 * How long a streaming reply has gone without visible activity, in steps: the
 * largest threshold the current quiet spell has passed (0 until the first one).
 *
 * `signature` changes whenever the reply shows something new (text, a tool
 * starting or finishing, reasoning). Callers only compare the idle time with a
 * few fixed thresholds, so instead of polling a clock this arms one timeout per
 * threshold and re-renders only when a threshold is crossed — a reply that is
 * busy streaming or running a tool never re-renders for the clock's sake.
 *
 * Timed on this device's clock: server event timestamps are fine for a
 * minutes-long counter, but a few seconds of clock skew would wreck sub-second
 * pacing like "keep the last tool's label for 400ms". A remount restarts the
 * count, which is harmless for the short waits this paces.
 */
export function useActivityAge(
  signature: string,
  active: boolean,
  thresholds: readonly number[],
): number {
  const [age, setAge] = useState({ signature, idleMs: 0 });
  let current = age;
  if (age.signature !== signature) {
    current = { signature, idleMs: 0 };
    setAge(current);
  }

  useEffect(() => {
    if (!active) return;
    const timers = thresholds.map((ms) => window.setTimeout(() => {
      setAge((prev) => (prev.signature !== signature || prev.idleMs >= ms
        ? prev
        : { signature, idleMs: ms }));
    }, ms));
    return () => timers.forEach((id) => window.clearTimeout(id));
  }, [signature, active, thresholds]);

  return active ? current.idleMs : 0;
}
