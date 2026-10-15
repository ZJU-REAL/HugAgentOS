import { useLayoutEffect, useRef, useState } from 'react';

/** CSS decides whether Canvas covers the pane; mirror that decision for focus
 * and portal content without duplicating viewport/container breakpoints. */
export function usePaneOcclusion(canvasOpen: boolean, fullscreen: boolean) {
  const paneRef = useRef<HTMLDivElement>(null);
  const [covered, setCovered] = useState(false);
  const restoreFocus = useRef(false);

  useLayoutEffect(() => {
    const pane = paneRef.current;
    const workspace = pane?.parentElement;
    if (!pane || !workspace) return;

    const sync = () => {
      const hidden = fullscreen || (canvasOpen && getComputedStyle(pane).visibility === 'hidden');
      const active = document.activeElement;
      if (hidden && active instanceof HTMLElement && pane.contains(active)) {
        workspace.querySelector<HTMLButtonElement>('.jx-canvasPanelSlot button:not([disabled])')?.focus();
      } else if (!canvasOpen && active instanceof HTMLElement
        && workspace.contains(active) && active.closest('.jx-canvasPanelSlot')) {
        restoreFocus.current = true;
      }
      setCovered(hidden);
    };
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(workspace);
    window.addEventListener('resize', sync);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', sync);
    };
  }, [canvasOpen, fullscreen]);

  const hidden = fullscreen || covered;
  useLayoutEffect(() => {
    // The pane must leave inert before its toggle can receive focus.
    if (!hidden && !canvasOpen && restoreFocus.current) {
      restoreFocus.current = false;
      paneRef.current?.querySelector<HTMLButtonElement>('.jx-rightSidebarToggle')?.focus();
    }
  }, [hidden, canvasOpen]);

  return { paneRef, hidden };
}
