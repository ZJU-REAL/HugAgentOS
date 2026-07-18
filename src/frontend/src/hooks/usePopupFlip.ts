import { useLayoutEffect, useState } from 'react';
import type React from 'react';

/** Minimum available space that must be left above the popup = popup max height + margin allowance (px). */
const FLIP_GAP = 16;

export interface PopupFlip {
  /** true = should flip below the input box (paired with the `--below` modifier class) */
  below: boolean;
  /** When below, the popup's top (px) relative to the positioned ancestor: anchored to the bottom edge of the
   *  cursor's line, expanding right against the trigger character (@ / /) — the project page's edit area is very
   *  tall (160px+), so anchoring to the bottom edge of the whole input box would be too far from the cursor.
   *  null when cursor geometry is unavailable, falling back to the CSS default `top: 100%`. */
  belowTop: number | null;
}

/**
 * The input box popup (/ skills, @ mentions) pops up by default (CSS `bottom: 100%`). When the input box is
 * too close to the top of the scroll/clip container (e.g. the project detail page, where the input box is in
 * the upper part of the page), popping up gets its top clipped by the container — in that case flip to expand
 * below the cursor's line.
 *
 * The measurement reference is the **nearest ancestor that clips absolutely positioned overflow** (overflow-y
 * other than visible), or the viewport if none. Measured only once, on the frame the popup appears
 * (useLayoutEffect, before paint, so it doesn't flash once then flip).
 *
 * @param ref     popup element ref (mounted inside composerWrap; the anchor is its parent element)
 * @param visible whether the popup is visible
 * @param popupMaxHeight popup CSS max-height (px), kept consistent with the styles
 */
export function usePopupFlip(
  ref: React.RefObject<HTMLElement | null>,
  visible: boolean,
  popupMaxHeight = 200,
): PopupFlip {
  const [flip, setFlip] = useState<PopupFlip>({ below: false, belowTop: null });

  useLayoutEffect(() => {
    if (!visible) return;
    const anchor = ref.current?.parentElement;
    if (!anchor) return;
    let clipTop = 0;
    let node: HTMLElement | null = anchor.parentElement;
    while (node && node !== document.body) {
      const { overflowY } = window.getComputedStyle(node);
      if (overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'hidden') {
        clipTop = node.getBoundingClientRect().top;
        break;
      }
      node = node.parentElement;
    }
    const anchorRect = anchor.getBoundingClientRect();
    const below = anchorRect.top - clipTop < popupMaxHeight + FLIP_GAP;

    let belowTop: number | null = null;
    if (below) {
      const sel = window.getSelection();
      if (sel && sel.rangeCount > 0 && anchor.contains(sel.getRangeAt(0).startContainer)) {
        const range = sel.getRangeAt(0);
        let rect: DOMRect | null = range.getBoundingClientRect();
        if (!rect.top && !rect.bottom && !rect.height) {
          // in some browsers a collapsed cursor yields an all-zero rect; fall back to the rect of the cursor's node
          const c = range.startContainer;
          const el = c instanceof HTMLElement ? c : c.parentElement;
          rect = el ? el.getBoundingClientRect() : null;
        }
        if (rect && (rect.top || rect.bottom)) {
          belowTop = Math.max(0, Math.min(rect.bottom - anchorRect.top, anchorRect.height));
        }
      }
    }
    // DOM geometry is only measurable after mount; useLayoutEffect guarantees the flip completes before paint, without flicker
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFlip({ below, belowTop });
  }, [visible, ref, popupMaxHeight]);

  return flip;
}
