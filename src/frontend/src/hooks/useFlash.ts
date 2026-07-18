// Family of one-shot highlight (flash) triggers —— replaces the hand-written
// useState(key) + useRef(timer) + setTimeout clearing boilerplate scattered around.
// Companion CSS: .jx-anim-flash / .jx-anim-flash-row in styles/motion.css (or per-area custom classes).
import { useEffect, useRef, useState } from 'react';

/** Manual-trigger variant: flash(key) highlights that key, then auto-clears after holdMs.
 * Typical usage: after a table row moves/saves successfully, call flash(rowKey); rowClassName compares against flashKey to attach the class. */
export function useFlashKey(holdMs = 600) {
  const [flashKey, setFlashKey] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const flash = (key: string) => {
    setFlashKey(key);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setFlashKey(null), holdMs);
  };
  return { flashKey, flash };
}

/** Polled-list status-flip detection variant: ids whose prev→next satisfies predicate enter the returned Set,
 * removed automatically after holdMs. The animation binds to the status diff rather than render —— stays silent when polling replaces the whole array. */
export function useStatusFlash<T>(
  items: T[],
  getId: (item: T) => string,
  getStatus: (item: T) => string,
  predicate: (prev: string, next: string) => boolean,
  holdMs = 1500,
) {
  const prevRef = useRef<Map<string, string> | null>(null);
  const [flashed, setFlashed] = useState<Set<string>>(() => new Set());
  const timers = useRef<number[]>([]);
  useEffect(() => () => { timers.current.forEach((t) => window.clearTimeout(t)); }, []);
  useEffect(() => {
    const prev = prevRef.current;
    const next = new Map(items.map((it) => [getId(it), getStatus(it)]));
    prevRef.current = next;
    if (!prev) return;
    const hits: string[] = [];
    next.forEach((status, id) => {
      const p = prev.get(id);
      if (p !== undefined && p !== status && predicate(p, status)) hits.push(id);
    });
    if (!hits.length) return;
    setFlashed((s) => new Set([...s, ...hits]));
    timers.current.push(window.setTimeout(() => {
      setFlashed((s) => {
        const n = new Set(s);
        hits.forEach((h) => n.delete(h));
        return n;
      });
    }, holdMs));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);
  return flashed;
}

/** Returns true for a short while when the value changes and satisfies the predicate, used to trigger a one-shot CSS animation (e.g. a badge 0→n pulse). */
export function usePulseOnChange<T>(
  value: T,
  shouldPulse: (prev: T, next: T) => boolean,
  holdMs = 600,
) {
  const prevRef = useRef(value);
  const [pulsing, setPulsing] = useState(false);
  useEffect(() => {
    const prev = prevRef.current;
    prevRef.current = value;
    if (prev !== value && shouldPulse(prev, value)) {
      setPulsing(true);
      const t = window.setTimeout(() => setPulsing(false), holdMs);
      return () => window.clearTimeout(t);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return pulsing;
}
