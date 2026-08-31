import { useEffect, useRef } from 'react';

/** 与 .jx-collapse / .jx-trs-bodyWrap 的 transition 时长保持一致。 */
const COLLAPSE_MS = 240;

/**
 * 折叠区的高度过渡：量一次内容实高，在 0 ↔ 实高之间做 height 过渡。
 *
 * 比 `grid-template-rows: 0fr → 1fr` 那套稳——后者在部分浏览器/内容结构下
 * 压根不插值，表现就是"啪"地一下没有过程。
 *
 * 高度直接写 DOM 而不是过 state：收起要先把当前实高定死、再归零，中间必须
 * 夹一次强制回流，交给 React 排渲染就赌不准这个时序了。展开动画结束后置回
 * auto，之后流式追加的步骤才不会被裁在这次量到的高度里。
 *
 * 折叠态的初始高度由 CSS 的 `height: 0` 负责，避免首帧闪一下全高。
 */
export function useCollapseHeight(open: boolean) {
  const collapseRef = useRef<HTMLDivElement | null>(null);
  const mounted = useRef(false);

  useEffect(() => {
    const el = collapseRef.current;
    if (!el) return;
    // 首帧不放动画：历史消息一进来就该是它该有的样子，不重放一遍展开。
    if (!mounted.current) {
      mounted.current = true;
      el.style.height = open ? 'auto' : '0px';
      return;
    }

    if (open) {
      el.style.height = `${el.scrollHeight}px`;
      const id = window.setTimeout(() => { el.style.height = 'auto'; }, COLLAPSE_MS + 20);
      return () => window.clearTimeout(id);
    }

    el.style.height = `${el.scrollHeight}px`;
    void el.offsetHeight;   // 强制回流，让浏览器把这个起点高度认下来
    el.style.height = '0px';
  }, [open]);

  return collapseRef;
}
