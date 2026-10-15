import { useEffect, useRef, useState } from 'react';

/** Reserve every fixed sibling, including More, management and public tools. */
export function useRailCapacity<Element extends HTMLElement = HTMLElement>(keys: readonly string[], rowHeight = 44, enabled = true) {
  const ref = useRef<Element | null>(null);
  const heights = useRef(new Map<string, number>());
  const [capacity, setCapacity] = useState(0);
  const signature = keys.join('\n');
  useEffect(() => {
    const root = ref.current;
    if (!root || !enabled) return;
    let frame = 0;
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const style = getComputedStyle(root);
        const gap = parseFloat(style.rowGap) || 0;
        const padding = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
        const fixed = [...root.children].filter(node => node instanceof HTMLElement && !node.hasAttribute('data-rail-application')) as HTMLElement[];
        const reserved = fixed.reduce((sum, node) => sum + node.offsetHeight, 0)
          + gap * Math.max(0, fixed.length - 1);
        [...root.children].forEach(node => {
          if (node instanceof HTMLElement && node.dataset.railApplication)
            heights.current.set(node.dataset.railApplication,node.offsetHeight);
        });
        const button = root.querySelector<HTMLElement>('[data-rail-application],button[aria-haspopup="dialog"]');
        const defaultHeight = button?.offsetHeight || rowHeight;
        let room = root.clientHeight - padding - reserved;
        let count = 0;
        for (const key of signature.split('\n').filter(Boolean)) {
          const height = (heights.current.get(key) ?? defaultHeight) + gap;
          if (room < height) break;
          room -= height; count++;
        }
        setCapacity(count);
      });
    };
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    [...root.children].forEach(node => observer.observe(node));
    window.visualViewport?.addEventListener('resize', measure);
    measure();
    return () => { cancelAnimationFrame(frame); observer.disconnect(); window.visualViewport?.removeEventListener('resize', measure); };
  }, [signature, rowHeight, enabled, capacity]);
  return {ref, capacity};
}
