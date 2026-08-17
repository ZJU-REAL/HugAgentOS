import { useSyncExternalStore } from 'react';

/**
 * 移动端断点的单一真源。
 *
 * 960px 这个数字原本散落在 App.tsx 的三处 matchMedia 调用和 mobile.css 的媒体查询里，
 * 各写各的——改一处漏一处时，布局会进入「CSS 认为是手机、JS 认为是桌面」的错位态。
 * 需要在 JS 里判断移动端的地方一律用本 hook，不要再裸写 matchMedia。
 *
 * 为什么会需要 JS 判断：绝大多数适配都该在 CSS 里做，唯独**被写进内联样式的值**
 * CSS 压不住（内联样式优先级高于任何非 !important 的规则）。典型是 framer-motion
 * 的 animate={{ height: N }}——侧栏历史项的行高就是这么被锁死在 36px 的，
 * 触摸热区怎么调样式表都不生效。这类值必须在 JS 侧按断点给。
 */
export const MOBILE_BREAKPOINT = 960;

const QUERY = `(max-width: ${MOBILE_BREAKPOINT}px)`;

const subscribe = (onChange: () => void) => {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {};
  const mql = window.matchMedia(QUERY);
  mql.addEventListener('change', onChange);
  return () => mql.removeEventListener('change', onChange);
};

const getSnapshot = () => {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia(QUERY).matches;
};

export function useIsMobileViewport(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
