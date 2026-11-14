import { useCallback, useEffect, useLayoutEffect, useRef, useState, type UIEvent } from 'react';
import { distanceFromBottom, nextFollowState, scrollElementToBottom, type FollowScrollState } from '../../../utils/scroll';

// How close to the top, in pixels, the next older page starts loading.
const OLDER_PRELOAD = 400;

interface Options {
  /** Id of the first row on screen; it changes when an older page goes in. */
  firstId?: string;
  hasOlder: boolean;
  loadingOlder: boolean;
  loadOlder: () => Promise<void>;
}

/**
 * One rule: the conversation stays at the bottom unless the reader scrolled up. Anything new (a sent message, a
 * reply, a card above the composer) lands at the bottom; a reader who scrolled up is left where they are until they
 * scroll back down or send. Loading an older page keeps the reader's place, since WebKit (the desktop app) has no
 * scroll anchoring.
 */
export function useConversationScroll({ firstId, hasOlder, loadingOlder, loadOlder }: Options) {
  const [viewport, setViewport] = useState<HTMLDivElement | null>(null);
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  const [dock, setDock] = useState<HTMLDivElement | null>(null);
  const [topSentinel, setTopSentinel] = useState<HTMLDivElement | null>(null);
  const follow = useRef<FollowScrollState>({ userScrolledUp: false, lastScrollTop: 0 });

  const toBottom = useCallback(() => {
    if (!viewport || follow.current.userScrolledUp) return;
    scrollElementToBottom(viewport);
    follow.current.lastScrollTop = viewport.scrollTop;
  }, [viewport]);

  // Every render, before paint: new rows, the typing line or a sent message never show above the bottom.
  useLayoutEffect(toBottom);
  // Content that grows after rendering (markdown, images, fonts), a card docking, or the window resizing.
  useEffect(() => {
    if (!viewport) return;
    const observer = new ResizeObserver(toBottom);
    [viewport, content, dock].forEach(node => node && observer.observe(node));
    return () => observer.disconnect();
  }, [viewport, content, dock, toBottom]);

  // Only the reader's own upward scroll stops following; reaching the bottom again resumes it.
  const onScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    const el = event.currentTarget;
    follow.current = nextFollowState(follow.current, { scrollTop: el.scrollTop, distanceFromBottom: distanceFromBottom(el) });
  }, []);

  /** Sending brings the reader back to the bottom. */
  const stickToBottom = useCallback(() => {
    follow.current.userScrolledUp = false;
    toBottom();
  }, [toBottom]);

  // Older pages: the first row in view is remembered before the page loads and put back in place once it is in.
  const anchor = useRef<{ id: string; top: number } | null>(null);
  useEffect(() => {
    if (!topSentinel || !viewport || !hasOlder || loadingOlder) return;
    const observer = new IntersectionObserver(entries => {
      // The anchor doubles as the busy flag: it is cleared once the page is placed.
      if (!entries.some(entry => entry.isIntersecting) || anchor.current) return;
      const viewTop = viewport.getBoundingClientRect().top;
      const first = Array.from(viewport.querySelectorAll<HTMLElement>('[data-message-id]')).find(node => node.getBoundingClientRect().bottom > viewTop);
      anchor.current = first ? { id: first.dataset.messageId!, top: first.getBoundingClientRect().top - viewTop } : null;
      void loadOlder();
    }, { root: viewport, rootMargin: OLDER_PRELOAD + 'px 0px 0px 0px' });
    observer.observe(topSentinel);
    return () => observer.disconnect();
    // Observing again after each page checks again: a page of short messages can leave the top still in reach.
  }, [topSentinel, viewport, hasOlder, loadingOlder, loadOlder]);
  useLayoutEffect(() => {
    const saved = anchor.current;
    // Wait for the page itself: the render that starts loading must not use up the anchor.
    if (!viewport || !saved || loadingOlder) return;
    anchor.current = null;
    // A reader still at the bottom stays there (the render above already put them back).
    if (!follow.current.userScrolledUp) return;
    const node = viewport.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(saved.id)}"]`);
    if (node) viewport.scrollTo({ top: node.getBoundingClientRect().top - viewport.getBoundingClientRect().top + viewport.scrollTop - saved.top });
    follow.current.lastScrollTop = viewport.scrollTop;
  }, [viewport, firstId, loadingOlder]);

  return { viewport, setViewport, setContent, setDock, setTopSentinel, onScroll, stickToBottom };
}
