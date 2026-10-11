import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

/** Model and effort panes share one placement policy, independent of item count. */
export function useModelMenuBounds(
  anchorRef: RefObject<HTMLElement | null>,
  open: boolean,
  projectOverview: boolean,
) {
  const [bounds, setBounds] = useState({ height: 360, below: projectOverview, top: 0, bottom: 0, right: 12, width: 320 });
  useLayoutEffect(() => {
    if (!open) return;
    const anchor = anchorRef.current;
    if (!anchor) return;
    const measure = () => {
      const rect = anchor.getBoundingClientRect();
      const viewport = window.visualViewport;
      const top = viewport?.offsetTop ?? 0;
      const bottom = top + (viewport?.height ?? window.innerHeight);
      const titlebar = parseFloat(getComputedStyle(document.documentElement)
        .getPropertyValue('--hugagent-desktop-titlebar-height')) || 0;
      const above = Math.max(0, rect.top - Math.max(top, titlebar) - 16);
      const below = Math.max(0, bottom - rect.bottom - 16);
      // Project overview owns a downward menu. Grow content inside the available
      // space instead of changing its direction when models or panes are added.
      const openBelow = projectOverview || above < Math.min(360, below);
      const height = Math.min(360, openBelow ? below : above);
      const width = viewport?.width ?? window.innerWidth;
      const viewportLeft = viewport?.offsetLeft ?? 0;
      const menuWidth = Math.min(320, width - 24);
      const right = Math.max(window.innerWidth - viewportLeft - width + 12, Math.min(window.innerWidth - rect.right,
        window.innerWidth - viewportLeft - menuWidth - 12));
      const next = { height, below: openBelow, top: rect.bottom + 8,
        bottom: window.innerHeight - rect.top + 8, right, width: menuWidth };
      setBounds(previous => previous.height === next.height && previous.below === next.below
        && previous.top === next.top && previous.bottom === next.bottom && previous.right === next.right && previous.width === next.width
        ? previous : next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    // Composer attachments, editor growth and project headers can move the
    // trigger without changing its size. Observe its layout ancestors as well.
    let node: HTMLElement | null = anchor;
    while (node) {
      observer.observe(node, { box: 'border-box' });
      node = node.parentElement;
    }
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    window.visualViewport?.addEventListener('resize', measure);
    window.visualViewport?.addEventListener('scroll', measure);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
      window.visualViewport?.removeEventListener('resize', measure);
      window.visualViewport?.removeEventListener('scroll', measure);
    };
  }, [anchorRef, open, projectOverview]);
  // This hook owns placement. The portal must not run a second flip/align policy.
  const popupStyle: CSSProperties = {
    position: 'fixed',
    top: bounds.below ? bounds.top : 'auto',
    bottom: bounds.below ? 'auto' : bounds.bottom,
    right: bounds.right,
    left: 'auto',
    transform: 'none',
    minWidth: 0,
    width: bounds.width,
  };
  return { ...bounds, popupStyle };
}
