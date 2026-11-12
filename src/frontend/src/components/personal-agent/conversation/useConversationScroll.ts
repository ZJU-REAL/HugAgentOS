import { useCallback, useEffect, useLayoutEffect, useRef, useState, type UIEvent } from 'react';
import { distanceFromBottom, SCROLL_TO_BOTTOM_BTN_THRESHOLD, scrollElementToBottom } from '../../../utils/scroll';
import type { PersonalMessage } from '../api';

// How close to the top, in pixels, the next older page starts loading.
const OLDER_PRELOAD = 400;
// Gap kept above a message placed at the top of the view.
const TOP_GAP = 16;

interface Options {
  /** Rows on screen, oldest first. */
  rows: PersonalMessage[];
  /** Whether the typing line is under the last row. */
  typing: boolean;
  /** Names the reader's latest send; a new key brings the view back to the end. */
  sentKey?: string;
  hasOlder: boolean;
  loadingOlder: boolean;
  loadOlder: () => Promise<void>;
}

/**
 * Where the conversation view sits. The desktop app runs in WebKit, which has no scroll anchoring, so every move is
 * placed here before paint:
 * - a conversation opens at its end, and follows new rows while the reader is at the end;
 * - sending brings the view back to the end, so the sent message and the typing line sit just above the composer;
 * - a reply taller than the view opens at its first line instead of its last;
 * - an older page goes in above what the reader is looking at without moving it.
 */
export function useConversationScroll({ rows, typing, sentKey, hasOlder, loadingOlder, loadOlder }: Options) {
  const [viewport, setViewport] = useState<HTMLDivElement | null>(null);
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  const [dock, setDock] = useState<HTMLDivElement | null>(null);
  const [topSentinel, setTopSentinel] = useState<HTMLDivElement | null>(null);
  // Whether the reader is at the end; scrolling up to read or select text stops following.
  const following = useRef(true);

  // Where a row starts in the scrolled content, or null when it is not on screen.
  const offsetOf = useCallback((id: string | undefined) => {
    const node = viewport && id ? viewport.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(id)}"]`) : null;
    return viewport && node ? node.getBoundingClientRect().top - viewport.getBoundingClientRect().top + viewport.scrollTop : null;
  }, [viewport]);

  // A send means the reader wants the end again, even if they had scrolled up.
  const placedSend = useRef(sentKey);
  useLayoutEffect(() => {
    if (!viewport || !sentKey || placedSend.current === sentKey) return;
    placedSend.current = sentKey;
    following.current = true;
    scrollElementToBottom(viewport);
  }, [viewport, sentKey]);

  const lastId = rows[rows.length - 1]?.id;
  const seenLastId = useRef<string | undefined>(undefined);
  useLayoutEffect(() => {
    if (!viewport) return;
    const previous = seenLastId.current;
    seenLastId.current = lastId;
    if (!following.current) return;
    const start = previous && previous !== lastId ? rows.findIndex(row => row.id === previous) : -1;
    const arrived = start >= 0 ? rows.slice(start + 1).find(row => row.role !== 'user') : undefined;
    const top = (offsetOf(arrived?.id) ?? Infinity) - TOP_GAP;
    if (top < viewport.scrollHeight - viewport.clientHeight) {
      viewport.scrollTop = top;
      following.current = false;
    } else scrollElementToBottom(viewport);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- placed when a row or the typing line appears; `rows` is only read to find the new row.
  }, [viewport, lastId, typing, offsetOf]);

  // Content that grows after it is placed (markdown, images, fonts) or a card docking above the composer.
  useEffect(() => {
    if (!viewport) return;
    const observer = new ResizeObserver(() => { if (following.current) scrollElementToBottom(viewport); });
    observer.observe(viewport);
    if (content) observer.observe(content);
    if (dock) observer.observe(dock);
    return () => observer.disconnect();
  }, [viewport, content, dock]);

  // The first row in view is remembered before an older page loads and put back in place once it is in.
  const anchor = useRef<{ id: string; top: number } | null>(null);
  const requestOlder = useRef<() => void>(() => {});
  requestOlder.current = () => {
    // The anchor doubles as the busy flag: it is cleared once the page is placed.
    if (!viewport || !hasOlder || loadingOlder || anchor.current) return;
    const viewTop = viewport.getBoundingClientRect().top;
    const first = Array.from(viewport.querySelectorAll<HTMLElement>('[data-message-id]')).find(node => node.getBoundingClientRect().bottom > viewTop);
    anchor.current = first ? { id: first.dataset.messageId!, top: first.getBoundingClientRect().top - viewTop } : null;
    following.current = false;
    void loadOlder();
  };
  useEffect(() => {
    if (!topSentinel || !viewport) return;
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) requestOlder.current(); },
      { root: viewport, rootMargin: OLDER_PRELOAD + 'px 0px 0px 0px' });
    observer.observe(topSentinel);
    return () => observer.disconnect();
    // Observing again after each page checks again: a page of short messages can leave the top still in reach.
  }, [topSentinel, viewport, loadingOlder]);
  const firstId = rows[0]?.id;
  useLayoutEffect(() => {
    const saved = anchor.current;
    // Wait for the page itself: the render that starts loading must not use up the anchor.
    if (!viewport || !saved || loadingOlder) return;
    anchor.current = null;
    const top = offsetOf(saved.id);
    if (top !== null) viewport.scrollTop = top - saved.top;
  }, [viewport, firstId, loadingOlder, offsetOf]);

  const onScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    following.current = distanceFromBottom(event.currentTarget) < SCROLL_TO_BOTTOM_BTN_THRESHOLD;
  }, []);

  return { viewport, setViewport, setContent, setDock, setTopSentinel, onScroll };
}
