// Display threshold for the "back to bottom" button
export const SCROLL_TO_BOTTOM_BTN_THRESHOLD = 80;
// 重新跟随的判据：只有滚回底部附近才恢复"流式自动跟到底"。留一点余量，
// 因为平滑滚动/亚像素布局经常停在离底 1–2px 的位置。
export const SCROLL_RESUME_THRESHOLD = 8;

export function distanceFromBottom(el: HTMLElement): number {
  return el.scrollHeight - el.scrollTop - el.clientHeight;
}

/** 流式跟随的开关状态：是否已被用户滚走 + 上一次观察到的 scrollTop 基线。 */
export interface FollowScrollState {
  userScrolledUp: boolean;
  lastScrollTop: number;
}

/**
 * 一次 scroll 事件之后的跟随状态。
 *
 * 判据是"这一次滚动有没有把视口往上挪"，不是"离底部够不够远"。按距离阈值整体
 * 重算会把用户刚做出的往上滚意图抹掉（滚轮先置位脱离跟随，紧跟着的 scroll 事件
 * 因为才挪了几十 px 又判成"还在底部"），下一帧流式增高就把页面拽回底部——正是
 * "输出过程中往上翻会被强制滚到最底"。
 *
 * 同时要求"确实离开了底部"，否则重新生成/截断消息时内容变短、浏览器把 scrollTop
 * 往回夹，也会被误判成用户往上滚。
 */
export function nextFollowState(
  prev: FollowScrollState,
  metrics: { scrollTop: number; distanceFromBottom: number },
): FollowScrollState {
  const movedUp = metrics.scrollTop < prev.lastScrollTop - 1;
  const away = metrics.distanceFromBottom > SCROLL_RESUME_THRESHOLD;
  const userScrolledUp = away ? (movedUp ? true : prev.userScrolledUp) : false;
  return { userScrolledUp, lastScrollTop: metrics.scrollTop };
}

/** 消息区里有没有选中的文字：拖选不产生滚动事件，跟随必须靠它让位。 */
export function hasActiveSelectionIn(root: Node | null, selection: Selection | null): boolean {
  if (!root || !selection || selection.isCollapsed || selection.rangeCount === 0) return false;
  for (let i = 0; i < selection.rangeCount; i += 1) {
    if (root.contains(selection.getRangeAt(i).commonAncestorContainer)) return true;
  }
  return false;
}

// MQL is a live object (automatically reflects system-setting changes); caching avoids
// rebuilding it per delta during streaming follow-scroll. 懒创建而不是模块级求值：
// 这个模块里的纯函数要能在 Node 下被测试直接 import，模块顶层碰 window 会直接炸。
let reducedMotionMql: MediaQueryList | null = null;
function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  if (!reducedMotionMql) reducedMotionMql = window.matchMedia('(prefers-reduced-motion: reduce)');
  return reducedMotionMql.matches;
}

export function scrollElementToBottom(el: HTMLElement, smooth = false): void {
  if (smooth && !prefersReducedMotion()) {
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  } else {
    el.scrollTop = el.scrollHeight;
  }
}
