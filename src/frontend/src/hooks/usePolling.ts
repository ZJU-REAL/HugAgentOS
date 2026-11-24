import { useEffect, useRef } from 'react';

export interface UsePollingOptions {
  /** Milliseconds between two calls. */
  interval: number;
  /** Poll only while this is true. Turning it off stops the timer at once. */
  enabled?: boolean;
  /** Also call once as soon as polling starts, instead of waiting one interval. */
  immediate?: boolean;
  /**
   * Pause while the page is hidden (default true). When the page becomes
   * visible again the callback runs once right away, then the interval resumes.
   */
  pauseWhenHidden?: boolean;
}

function pageVisible(): boolean {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

/**
 * Calls `callback` every `interval` ms while `enabled` is true and the page is visible.
 *
 * - The latest `callback` is always used, so callers don't need to memoize it.
 * - A tick is skipped while the previous async call is still running, so slow
 *   requests never pile up.
 * - The timer is cleared on unmount and whenever `enabled` turns false.
 */
export function usePolling(
  callback: () => unknown,
  { interval, enabled = true, immediate = false, pauseWhenHidden = true }: UsePollingOptions,
): void {
  const callbackRef = useRef(callback);
  useEffect(() => {
    callbackRef.current = callback;
  });

  useEffect(() => {
    if (!enabled || interval <= 0) return undefined;
    let timer: ReturnType<typeof setInterval> | null = null;
    let running = false;
    let disposed = false;

    const tick = () => {
      if (running || disposed) return;
      let result: unknown;
      try {
        result = callbackRef.current();
      } catch {
        return;
      }
      if (result && typeof (result as PromiseLike<unknown>).then === 'function') {
        running = true;
        Promise.resolve(result)
          .catch(() => undefined)
          .finally(() => { running = false; });
      }
    };
    const start = () => {
      if (timer !== null) return;
      timer = setInterval(tick, interval);
    };
    const stop = () => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    };

    if (!pauseWhenHidden || pageVisible()) {
      if (immediate) tick();
      start();
    }

    const onVisibility = () => {
      if (pageVisible()) {
        tick();
        start();
      } else {
        stop();
      }
    };
    if (pauseWhenHidden && typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }
    return () => {
      disposed = true;
      stop();
      if (pauseWhenHidden && typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
    };
  }, [enabled, immediate, interval, pauseWhenHidden]);
}
