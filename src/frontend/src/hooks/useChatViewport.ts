import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { distanceFromBottom, hasActiveSelectionIn, nextFollowState, scrollElementToBottom } from '../utils/scroll';
export function useChatViewport(currentChatId: string, hasMessages: boolean, chatSurface: boolean) {
  // ── Refs ──
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const chatListRef = useRef<HTMLDivElement | null>(null);
  // 滚动容器要等认证闸放行才渲染出来。用回调 ref 进 state，元素一出现依赖它的
  // effect 就重跑挂好监听；挂载时 querySelector 一次的写法会永远拿到 null。
  const [contentEl, setContentEl] = useState<HTMLElement | null>(null);
  const handleContentRef = useCallback((el: HTMLElement | null) => setContentEl(el), []);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const userScrolledUpRef = useRef(false);
  // 上一次观察到的 scrollTop：用来判断这次滚动是"往上"还是"内容长高把视口顶下去"。
  const lastScrollTopRef = useRef(0);
  // 鼠标在消息区按下到抬起之间：用户正在拖选（此刻选区可能还是空的），先停跟随。
  const isSelectingRef = useRef(false);

  // Track whether the user has scrolled up on purpose
  useEffect(() => {
    const content = contentEl;
    if (!content) return;
    // 判据是"这一次滚动有没有把视口往上挪"，滚轮/触摸/拖滚动条/PageUp 一视同仁。
    const handleScroll = () => {
      const next = nextFollowState(
        { userScrolledUp: userScrolledUpRef.current, lastScrollTop: lastScrollTopRef.current },
        { scrollTop: content.scrollTop, distanceFromBottom: distanceFromBottom(content) },
      );
      lastScrollTopRef.current = next.lastScrollTop;
      userScrolledUpRef.current = next.userScrolledUp;
    };
    // 顶到头时不产生 scroll 事件，只有 wheel —— 所以滚轮向上直接置位。
    const handleWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) userScrolledUpRef.current = true;
    };
    const handleTouchMove = () => {
      if (distanceFromBottom(content) > 1) userScrolledUpRef.current = true;
    };
    // 拖选正文既不发 wheel 也不发 touchmove，靠这一对标记让跟随让位给选择。
    const handleMouseDown = (e: MouseEvent) => {
      if (e.button !== 0) return;
      const list = chatListRef.current;
      isSelectingRef.current = !!list && e.target instanceof Node && list.contains(e.target);
    };
    const handleMouseUp = () => { isSelectingRef.current = false; };
    content.addEventListener('scroll', handleScroll, { passive: true });
    content.addEventListener('wheel', handleWheel, { passive: true });
    content.addEventListener('touchmove', handleTouchMove, { passive: true });
    document.addEventListener('mousedown', handleMouseDown, true);
    document.addEventListener('mouseup', handleMouseUp, true);
    return () => {
      content.removeEventListener('scroll', handleScroll);
      content.removeEventListener('wheel', handleWheel);
      content.removeEventListener('touchmove', handleTouchMove);
      document.removeEventListener('mousedown', handleMouseDown, true);
      document.removeEventListener('mouseup', handleMouseUp, true);
    };
  }, [contentEl]);

  // Chat switch: reset follow state and land at the bottom in the same commit, before the
  // browser paints — opening a conversation shows its latest message directly, with no
  // scroll animation. Height growth from follow-up/action-bar animations after reaching the
  // bottom is covered by the ResizeObserver below.
  // hasMessages as a dependency: entering a chat whose messages haven't been fetched yet,
  // the first render has scrollHeight===clientHeight so the jump is a no-op; once messages
  // load asynchronously this effect runs again, ensuring we truly land at the bottom.
  useLayoutEffect(() => {
    userScrolledUpRef.current = false;
    const content = contentEl;
    if (!content) return;
    scrollElementToBottom(content);
    // 换会话后列表整个换了一棵树，旧的 scrollTop 基线没有意义：不同步的话
    // 下一次 scroll 事件会拿旧基线比出"用户往上滚"，一进来就脱离跟随。
    lastScrollTopRef.current = content.scrollTop;
  }, [currentChatId, hasMessages, contentEl]);

  // Observe chat-list size changes: when streaming chunks or the framer-motion animations
  // of the follow-up/action bar grow the height, snap-align to the bottom as long as the
  // user hasn't scrolled up. Compared to multi-stage setTimeout fallbacks, this is driven
  // by "content actually changed" — no magic time numbers, and no pending setTimeouts
  // piling up while idle.
  // hasMessages as a dependency: the .jx-chatList that chatListRef points to only mounts
  // when messages exist; when the list goes from none to some we must re-observe the new node.
  useEffect(() => {
    if (!chatSurface || !hasMessages) return;
    const content = contentEl;
    const list = chatListRef.current;
    if (!content || !list || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      if (userScrolledUpRef.current) return;
      // 正在拖选或已经选中了正文：跟随必须让位，否则选区在手底下被拽走、复制不了。
      if (isSelectingRef.current || hasActiveSelectionIn(list, window.getSelection())) return;
      content.scrollTop = content.scrollHeight;
    });
    ro.observe(list);
    return () => ro.disconnect();
  }, [chatSurface, currentChatId, hasMessages, contentEl]);

  // Expanding a history plan card's step details, or expanding a tool-call card to view its
  // output, grows the DOM — the ResizeObserver above would then yank the viewport to the
  // bottom, pushing the content the user just expanded off screen. Here we intercept clicks
  // in the capture phase: whenever the user clicks the expand/collapse control of a plan
  // card or tool-call card, pre-mark userScrolledUpRef=true so the subsequent resize event
  // skips auto-scroll. Scrolling back to the bottom or sending another message naturally
  // resets this flag, leaving later streaming follow unaffected.
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target) return;
      // .jx-msgActionBtn / .jx-editMessage：点「编辑消息」展开编辑框、点「取消」收起
      // 都会播放高度动画，ResizeObserver 会误判为流式增高而滚到底部 —— 预置脱离跟随。
      if (target.closest('.jx-plan-stepHeader, .jx-plan-stepsToggle, .jx-tcr-header, .jx-trs-head, .jx-msgActionBtn, .jx-editMessage, .jx-conversationNav-button')) {
        userScrolledUpRef.current = true;
      }
    };
    document.addEventListener('click', handler, { capture: true });
    return () => document.removeEventListener('click', handler, { capture: true } as EventListenerOptions);
  }, []);

  return { inputRef, fileInputRef, chatListRef, messagesEndRef, handleContentRef, userScrolledUpRef };
}
