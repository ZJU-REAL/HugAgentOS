import { useEffect, useState } from 'react';

interface ActivityAge {
  /** The largest threshold the current quiet spell has passed (0 until the first one). */
  idleMs: number;
  /** Local time the quiet spell began; known once the first threshold has passed. */
  since: number | null;
}

/**
 * How long a streaming reply has gone without visible activity, in steps.
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
): ActivityAge {
  const [age, setAge] = useState<ActivityAge & { signature: string }>({ signature, idleMs: 0, since: null });
  let current = age;
  if (age.signature !== signature) {
    current = { signature, idleMs: 0, since: null };
    setAge(current);
  }

  useEffect(() => {
    if (!active) return;
    const timers = thresholds.map((ms) => window.setTimeout(() => {
      setAge((prev) => (prev.signature !== signature || prev.idleMs >= ms
        ? prev
        : { signature, idleMs: ms, since: prev.since ?? Date.now() - ms }));
    }, ms));
    return () => timers.forEach((id) => window.clearTimeout(id));
  }, [signature, active, thresholds]);

  return active ? { idleMs: current.idleMs, since: current.since } : { idleMs: 0, since: null };
}
