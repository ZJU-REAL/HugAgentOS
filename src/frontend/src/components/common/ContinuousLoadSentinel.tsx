import { useEffect, useRef } from 'react';

interface ContinuousLoadSentinelProps {
  /** 还有没显示出来的内容 */
  hasMore: boolean;
  /** 当前已显示的条数；每次增长后重新观察，哨兵仍在可视区时会继续加载 */
  count: number;
  onMore: () => void;
}

/**
 * 手机上替代页码条：列表末尾的隐形哨兵，滚到离底部一屏以内时追加下一批。
 * iOS 的列表都是一条连续滚动面，不出现「上一页 / 下一页」。
 * IntersectionObserver 以视口为根，会计入祖先滚动容器的裁剪，所以放在弹窗或模块页里都成立。
 */
export function ContinuousLoadSentinel({ hasMore, count, onMore }: ContinuousLoadSentinelProps) {
  const ref = useRef<HTMLDivElement>(null);
  const onMoreRef = useRef(onMore);
  useEffect(() => { onMoreRef.current = onMore; }, [onMore]);
  useEffect(() => {
    const node = ref.current;
    if (!hasMore || !node || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) onMoreRef.current();
    }, { rootMargin: '0px 0px 600px 0px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMore, count]);
  if (!hasMore) return null;
  return <div ref={ref} className="jx-continuousLoadSentinel" aria-hidden="true" />;
}
