import { useEffect, useLayoutEffect, useState, type RefObject } from 'react';
import { create } from 'zustand';

/** One occurrence found by the pane's find bar. */
export interface FindHit {
  /** DOM range when the hit is currently rendered (virtualized pages may not be). */
  range: () => Range | null;
  /** Bring the hit into view, rendering it first if needed. */
  reveal: () => Promise<void>;
}

export interface FindSource {
  search: (query: string) => Promise<FindHit[]>;
  /** Fires when rendered content changes, so hits and painted ranges must be recomputed. */
  subscribe: (listener: () => void) => () => void;
}

export interface OutlineNode {
  id: string;
  title: string;
  children: OutlineNode[];
  open: () => void;
  /**
   * Viewport y of the entry's target while it is laid out (null when it is not),
   * so the outline can mark the section being read.
   */
  top?: () => number | null;
}

/** The element whose scrolling moves the document: a renderer's own scroller (pdf) or the body. */
export function contentScroller(body: HTMLElement): HTMLElement {
  return body.querySelector<HTMLElement>('[data-canvas-scroller]') ?? body;
}

/**
 * Renderers whose content is not plain DOM under `.jx-canvas-body` (virtualized
 * pdf pages, bookmarks that live in the file) register richer find/outline
 * providers for their tab. Everything else falls back to DOM scanning.
 */
export interface CanvasDocument {
  find?: FindSource;
  outline?: () => Promise<OutlineNode[]>;
}

const useCanvasDocumentStore = create<{ docs: Record<string, CanvasDocument> }>(() => ({ docs: {} }));

export function useRegisterCanvasDocument(tabId: string | null | undefined, doc: CanvasDocument | null) {
  useEffect(() => {
    if (!tabId || !doc) return;
    useCanvasDocumentStore.setState((s) => ({ docs: { ...s.docs, [tabId]: doc } }));
    return () => useCanvasDocumentStore.setState((s) => {
      if (s.docs[tabId] !== doc) return s;
      const docs = { ...s.docs };
      delete docs[tabId];
      return { docs };
    });
  }, [tabId, doc]);
}

export function useCanvasDocument(tabId: string | null | undefined): CanvasDocument | undefined {
  return useCanvasDocumentStore((s) => (tabId ? s.docs[tabId] : undefined));
}

function scrollParent(node: Element | null): HTMLElement | null {
  for (let el = node?.parentElement ?? null; el; el = el.parentElement) {
    const { overflowY } = getComputedStyle(el);
    if ((overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight) return el;
  }
  return null;
}

/**
 * Scroll the nearest scrolling ancestor so `target` sits at `block`. Unlike
 * `scrollIntoView` this never moves `overflow: hidden` ancestors (the canvas
 * shell), which would shift the whole pane.
 */
export function scrollIntoPane(target: Element | Range, block: 'start' | 'center' = 'center') {
  const anchor = target instanceof Range
    ? (target.startContainer instanceof Element ? target.startContainer : target.startContainer.parentElement)
    : target;
  const scroller = scrollParent(anchor);
  if (!scroller) return;
  const rect = target.getBoundingClientRect();
  const box = scroller.getBoundingClientRect();
  const top = block === 'start'
    ? rect.top - box.top - 8
    : rect.top - box.top - (scroller.clientHeight - rect.height) / 2;
  const outsideX = rect.left < box.left || rect.right > box.left + scroller.clientWidth;
  scroller.scrollTo({
    top: scroller.scrollTop + top,
    left: outsideX ? scroller.scrollLeft + rect.left - box.left - scroller.clientWidth / 3 : scroller.scrollLeft,
  });
}

/** Canvas shell and body that contain `anchor` (find bar / outline buttons live in the header). */
export function paneElements(anchor: Element | null) {
  const pane = anchor?.closest<HTMLElement>('.jx-canvas') ?? null;
  return { pane, body: pane?.querySelector<HTMLElement>(':scope > .jx-canvas-body') ?? null };
}

/**
 * Pane shell/body for overlays (find bar, outline list) mounted from the
 * header: `top` is where the body starts inside the positioned `.jx-canvas`.
 * `bodyKey` must change whenever CanvasPanel remounts its body (tab / openSeq).
 */
export function usePaneSurface(anchor: RefObject<Element | null>, active: boolean, bodyKey: string) {
  const [surface, setSurface] = useState<{ pane: HTMLElement; body: HTMLElement; top: number } | null>(null);
  useLayoutEffect(() => {
    const { pane, body } = paneElements(anchor.current);
    if (!active || !pane || !body) return;
    // ResizeObserver also reports once right after observe(), which does the first measure.
    const observer = new ResizeObserver(() => setSurface((prev) => (
      prev && prev.pane === pane && prev.body === body && prev.top === body.offsetTop ? prev : { pane, body, top: body.offsetTop }
    )));
    observer.observe(pane);
    observer.observe(body);
    return () => observer.disconnect();
  }, [anchor, active, bodyKey]);
  // A remounted body (tab switch) leaves the old one detached until the observer reports.
  return active && surface?.body.isConnected ? surface : null;
}
