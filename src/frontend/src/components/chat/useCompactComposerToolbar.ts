import { useLayoutEffect, useRef } from 'react';

/** Collapse all labels based on actual contents, including late model updates. */
export function useCompactComposerToolbar() {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const bar = ref.current;
    if (!bar) return;
    const update = () => {
      bar.removeAttribute('data-compact');
      bar.setAttribute('data-measuring', '');
      const style = getComputedStyle(bar);
      const groups = Array.from(bar.children) as HTMLElement[];
      const required = groups.reduce((sum, group) => sum + group.getBoundingClientRect().width, 0)
        + parseFloat(style.columnGap) * Math.max(0, groups.length - 1)
        + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
      const compact = required > bar.clientWidth;
      bar.removeAttribute('data-measuring');
      bar.toggleAttribute('data-compact', compact);
    };
    update();
    const resize = new ResizeObserver(update);
    resize.observe(bar);
    // Model selection, permission loading and mode changes may not resize the bar.
    // Exclude attributes so our own measurement does not trigger another update.
    const content = new MutationObserver(update);
    content.observe(bar, { childList: true, characterData: true, subtree: true });
    document.fonts?.addEventListener('loadingdone', update);
    return () => {
      resize.disconnect();
      content.disconnect();
      document.fonts?.removeEventListener('loadingdone', update);
    };
  }, []);
  return ref;
}
