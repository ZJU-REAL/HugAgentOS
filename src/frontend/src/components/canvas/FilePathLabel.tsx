import { useLayoutEffect, useRef } from 'react';
import './CanvasFileHeader.css';

/** DSH PathLabel's filename-preserving layout, adapted to product tokens (MIT). */
export function FilePathLabel({ path }: { path: string }) {
  const outerRef = useRef<HTMLSpanElement>(null);
  const innerRef = useRef<HTMLSpanElement>(null);
  const index = Math.max(path.lastIndexOf('/'), path.lastIndexOf(String.fromCharCode(92)));
  useLayoutEffect(() => {
    const outer = outerRef.current, inner = innerRef.current;
    if (!outer || !inner) return;
    const update = () => outer.toggleAttribute('data-path-clipped', inner.offsetWidth > outer.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(outer); observer.observe(inner);
    return () => observer.disconnect();
  }, [path]);
  return <span className="jx-filePath" ref={outerRef} title={path} data-file-path>
    <span className="jx-filePath-text" ref={innerRef}>
      {index >= 0 && <span className="jx-filePath-directory">{path.slice(0, index + 1)}</span>}
      <span>{path.slice(index + 1)}</span>
    </span>
  </span>;
}
