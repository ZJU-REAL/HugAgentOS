import { useEffect, useRef, useState } from 'react';

interface DelayedFlagOptions {
  /** How many ms to delay after `active` turns true before actually emitting true; if it ends during that window, skip showing to avoid flicker */
  showAfter?: number;
  /** Once true is emitted, keep it for at least this many ms before allowing a return to false */
  minVisible?: number;
}

/**
 * Convert `active` (e.g. loading) into a display flag with delay / minimum dwell time:
 * - active duration < showAfter: always return false (skeleton never flickers)
 * - active duration ≥ showAfter: return true, and keep it for at least minVisible ms
 */
export function useDelayedFlag(active: boolean, options?: DelayedFlagOptions): boolean {
  const showAfter = options?.showAfter ?? 150;
  const minVisible = options?.minVisible ?? 300;

  const [visible, setVisible] = useState(false);
  const showTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shownAtRef = useRef<number | null>(null);

  useEffect(() => {
    if (active) {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
      if (visible || showTimerRef.current) return;
      showTimerRef.current = setTimeout(() => {
        shownAtRef.current = Date.now();
        showTimerRef.current = null;
        setVisible(true);
      }, showAfter);
      return;
    }

    if (showTimerRef.current) {
      clearTimeout(showTimerRef.current);
      showTimerRef.current = null;
    }
    if (!visible || hideTimerRef.current) return;
    const elapsed = shownAtRef.current ? Date.now() - shownAtRef.current : minVisible;
    const remaining = Math.max(0, minVisible - elapsed);
    hideTimerRef.current = setTimeout(() => {
      shownAtRef.current = null;
      hideTimerRef.current = null;
      setVisible(false);
    }, remaining);
  }, [active, visible, showAfter, minVisible]);

  useEffect(() => () => {
    if (showTimerRef.current) clearTimeout(showTimerRef.current);
    if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
  }, []);

  return visible;
}
