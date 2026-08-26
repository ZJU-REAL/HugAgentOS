import { useEffect, useRef, useState } from 'react';

/**
 * Reports whether a bounded scroll box's content actually exceeds its height.
 *
 * Drives the bottom fade mask on collapsible bodies: a permanently-applied mask
 * eats the last line of short content and reads as a rendering defect, so the
 * class only goes on once there really is more to scroll to.
 *
 * Returns `[boxRef, innerRef, overflowing]`. The observer watches the inner
 * wrapper, not the box — the box is pinned at its max-height once it overflows
 * and would stop reporting growth (streamed text, a row expanding inside).
 *
 * `active` gates measurement: a collapsed body has clientHeight 0 and would
 * otherwise always report as overflowing.
 */
export function useOverflowFade(active: boolean) {
  const boxRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const [overflowing, setOverflowing] = useState(false);
  const lastRef = useRef(false);

  useEffect(() => {
    const box = boxRef.current;
    // 只在值真的翻转时才 setState：流式展开时观察者每帧都会回调
    const apply = (next: boolean) => {
      if (lastRef.current === next) return;
      lastRef.current = next;
      setOverflowing(next);
    };
    if (!active || !box) {
      if (!lastRef.current) return;
      const off = requestAnimationFrame(() => apply(false));
      return () => cancelAnimationFrame(off);
    }
    const measure = () => apply(box.scrollHeight - box.clientHeight > 4);
    if (typeof ResizeObserver === 'undefined') {
      // 无 ResizeObserver 时只能测一次，放到下一帧等展开高度落定
      const first = requestAnimationFrame(measure);
      return () => cancelAnimationFrame(first);
    }
    // observe() 自带一次初始回调，不必再手动首测
    const ro = new ResizeObserver(measure);
    ro.observe(box);
    if (innerRef.current) ro.observe(innerRef.current);
    return () => ro.disconnect();
  }, [active]);

  return [boxRef, innerRef, overflowing] as const;
}
