import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { Popover } from 'antd';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';
import { buildConversationTurns } from '../../utils/conversationNavigation';
import '../../styles/conversationNavigation.css';

interface ConversationNavigationProps {
  messages: readonly ChatMessage[];
  chatListRef: RefObject<HTMLDivElement | null>;
}

/** Mounted per chat so preview/focus state cannot leak to another conversation. */
export function ConversationNavigation({ messages, chatListRef }: ConversationNavigationProps) {
  const turns = useMemo(() => buildConversationTurns(messages), [messages]);
  const anchors = turns.map((turn) => turn.uid).join(',');
  const [activeUid, setActiveUid] = useState<string | null>(null);
  const [previewUid, setPreviewUid] = useState<string | null>(null);
  const [position, setPosition] = useState<{ top: number; right: number; height: number } | null>(null);
  const railRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const list = chatListRef.current;
    const scroller = list?.closest<HTMLElement>('.jx-content');
    if (!list || !scroller || !anchors) return;
    const anchorUids = anchors.split(',');
    const footer = list.parentElement?.querySelector<HTMLElement>('.jx-chatFooter');
    let frame = 0;
    const update = () => {
      frame = 0;
      const rect = scroller.getBoundingClientRect();
      const footerTop = footer?.getBoundingClientRect().top ?? rect.bottom;
      const visibleBottom = Math.min(rect.bottom, footerTop);
      const height = Math.max(0, visibleBottom - rect.top - 32);
      const nextPosition = {
        top: rect.top + 16 + height / 2,
        right: Math.max(0, window.innerWidth - rect.right) + 8,
        height: Math.min(360, height),
      };
      setPosition((previous) => previous?.top === nextPosition.top
        && previous.right === nextPosition.right && previous.height === nextPosition.height
        ? previous : nextPosition);
      // Last question above the reading line owns the following answer, even a very long one.
      const readingLine = rect.top + Math.min(96, height * 0.25);
      let current = anchorUids[0];
      for (const uid of anchorUids) {
        const element = list.querySelector<HTMLElement>(`[data-message-uid="${uid}"]`);
        if (!element) continue;
        if (element.getBoundingClientRect().top > readingLine) break;
        current = uid;
      }
      // At the bottom the last turn may be too short to reach the reading line.
      if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 8) {
        current = anchorUids[anchorUids.length - 1];
      }
      setActiveUid(current);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };
    schedule();
    scroller.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    const observer = new ResizeObserver(schedule);
    observer.observe(scroller);
    observer.observe(list);
    if (footer) observer.observe(footer);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      scroller.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
    };
  }, [anchors, chatListRef]);

  useEffect(() => {
    const rail = railRef.current;
    if (!rail || rail.matches(':hover') || rail.contains(document.activeElement)) return;
    const button = rail.querySelector<HTMLElement>('[aria-current="location"]');
    if (!button) return;
    const buttonRect = button.getBoundingClientRect();
    const railRect = rail.getBoundingClientRect();
    if (buttonRect.top < railRect.top) rail.scrollTop -= railRect.top - buttonRect.top;
    else if (buttonRect.bottom > railRect.bottom) rail.scrollTop += buttonRect.bottom - railRect.bottom;
  }, [activeUid, anchors]);

  const navigate = (uid: string) => {
    const list = chatListRef.current;
    const scroller = list?.closest<HTMLElement>('.jx-content');
    const target = list?.querySelector<HTMLElement>(`[data-message-uid="${uid}"]`);
    if (!target || !scroller) return;
    setPreviewUid(null);
    const top = scroller.scrollTop + target.getBoundingClientRect().top
      - scroller.getBoundingClientRect().top - 24;
    scroller.scrollTo({
      top: Math.max(0, top),
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
    });
    target.classList.remove('jx-anim-flash');
    void target.offsetWidth;
    target.classList.add('jx-anim-flash');
  };

  if (!turns.length || !position || position.height < 44) return null;

  return createPortal(
    <nav
      ref={railRef}
      className="jx-conversationNav"
      aria-label={t('当前会话导航')}
      style={{ top: position.top, right: position.right, maxHeight: position.height }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          setPreviewUid(null);
          event.stopPropagation();
          return;
        }
        const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button'));
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === 'ArrowDown' ? Math.min(index + 1, buttons.length - 1)
          : event.key === 'ArrowUp' ? Math.max(0, index - 1)
            : event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : -1;
        if (next >= 0) {
          event.preventDefault();
          buttons[next]?.focus();
        }
      }}
    >
      {turns.map((turn, index) => {
        const title = turn.title || t('第 {n} 轮对话', { n: index + 1 });
        return (
          <Popover
            key={turn.uid}
            placement="left"
            trigger={['hover', 'focus']}
            open={previewUid === turn.uid}
            onOpenChange={(open) => setPreviewUid((current) => open ? turn.uid : current === turn.uid ? null : current)}
            arrow={false}
            mouseEnterDelay={0.12}
            classNames={{ root: 'jx-conversationNav-popover' }}
            content={(
              <div className="jx-conversationNav-preview">
                <div className="jx-conversationNav-title">{title}</div>
                <div className="jx-conversationNav-summary">
                  {turn.preview || t('暂无回复')}
                </div>
              </div>
            )}
          >
            <button
              type="button"
              className="jx-conversationNav-button"
              aria-label={t('跳转到：{title}', { title })}
              aria-current={activeUid === turn.uid ? 'location' : undefined}
              onClick={() => navigate(turn.uid)}
            >
              <span className="jx-conversationNav-mark" aria-hidden="true" />
            </button>
          </Popover>
        );
      })}
    </nav>,
    document.body,
  );
}
