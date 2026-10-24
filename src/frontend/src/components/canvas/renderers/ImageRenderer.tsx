import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from 'react';
import { t } from '../../../i18n';
import { useCanvasZoomStore, useCanvasZoomTarget } from '../canvasZoom';

/**
 * 'fit' contain-fits the image in the pane; a numeric zoom renders it at
 * natural size × zoom inside its own scroll viewport, where it can be dragged
 * to pan. Double-click toggles fit ↔ 100%.
 */
export function ImageRenderer({ url, name }: { url: string; name: string }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const [natural, setNatural] = useState<{ width: number; height: number } | null>(null);
  const [failed, setFailed] = useState(false);
  const { tabId, zoom, reportFitScale } = useCanvasZoomTarget(!failed);
  // Keep the point at the viewport centre steady across zoom changes.
  const centerRef = useRef({ x: 0.5, y: 0.5 });
  const dragRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const isFit = zoom === 'fit';

  // In fit mode the rendered size *is* the fit scale; report it whenever the pane resizes.
  useEffect(() => {
    const img = imgRef.current;
    if (!img || !natural || !isFit) return;
    const report = () => reportFitScale(Math.round((img.clientWidth / natural.width) * 100) / 100);
    const observer = new ResizeObserver(report);
    observer.observe(img);
    report();
    return () => observer.disconnect();
  }, [isFit, natural, reportFitScale]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || isFit) return;
    viewport.scrollLeft = centerRef.current.x * viewport.scrollWidth - viewport.clientWidth / 2;
    viewport.scrollTop = centerRef.current.y * viewport.scrollHeight - viewport.clientHeight / 2;
  }, [isFit, zoom]);

  const onScroll = () => {
    const viewport = viewportRef.current;
    if (!viewport || !viewport.scrollWidth || !viewport.scrollHeight) return;
    centerRef.current = {
      x: (viewport.scrollLeft + viewport.clientWidth / 2) / viewport.scrollWidth,
      y: (viewport.scrollTop + viewport.clientHeight / 2) / viewport.scrollHeight,
    };
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const viewport = viewportRef.current;
    if (isFit || event.button !== 0 || !viewport) return;
    if (viewport.scrollWidth <= viewport.clientWidth && viewport.scrollHeight <= viewport.clientHeight) return;
    // A press on the viewport's own scrollbar belongs to the scrollbar.
    const rect = viewport.getBoundingClientRect();
    if (event.clientX - rect.left >= viewport.clientWidth || event.clientY - rect.top >= viewport.clientHeight) return;
    event.preventDefault();
    viewport.setPointerCapture(event.pointerId);
    dragRef.current = { x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop };
    setDragging(true);
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const viewport = viewportRef.current;
    const start = dragRef.current;
    if (!viewport || !start) return;
    viewport.scrollLeft = start.left - (event.clientX - start.x);
    viewport.scrollTop = start.top - (event.clientY - start.y);
  };
  const endDrag = () => {
    dragRef.current = null;
    setDragging(false);
  };

  const size = !isFit && natural ? { width: natural.width * zoom, height: natural.height * zoom } : undefined;
  return (
    <div
      ref={viewportRef}
      className={`jx-canvas-image${isFit ? '' : ' jx-canvas-image--zoomed'}${dragging ? ' is-dragging' : ''}`}
      onScroll={onScroll}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={() => { if (tabId) useCanvasZoomStore.getState().setZoom(tabId, isFit ? 1 : 'fit'); }}
      title={t('双击在适应窗口与 100% 之间切换')}
    >
      <img
        ref={imgRef}
        src={url}
        alt={name}
        className="jx-canvas-image-img"
        draggable={false}
        style={size}
        onError={() => setFailed(true)}
        onLoad={(event) => {
          const img = event.currentTarget;
          // Some SVGs have no intrinsic size; treat their laid-out size as 100%.
          setNatural({ width: img.naturalWidth || img.clientWidth || 1, height: img.naturalHeight || img.clientHeight || 1 });
        }}
      />
    </div>
  );
}
