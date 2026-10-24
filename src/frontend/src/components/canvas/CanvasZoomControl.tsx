import { useContext, useEffect, useRef } from 'react';
import { ColumnWidthOutlined, MinusOutlined, PlusOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { useCanvasStore } from '../../stores/canvasStore';
import { CanvasPaneContext } from './CanvasPaneContext';
import {
  clampZoom, resolveZoom, stepZoom, useCanvasZoom, useCanvasZoomable, useCanvasZoomStore,
} from './canvasZoom';

/**
 * Header zoom control for the pane's current tab: fit / − / % / +.
 * Also owns Ctrl/⌘ + wheel over this pane, and Ctrl/⌘ + = / − / 0 while focus
 * is inside it (or nothing is focused and the pointer is over it), so browser
 * zoom keeps working everywhere else, including the other pane of a split.
 */
export function CanvasZoomControl() {
  const pane = useContext(CanvasPaneContext);
  const tabId = useCanvasStore((s) => s.paneTabs[pane]);
  const zoomable = useCanvasZoomable(tabId);
  const zoom = useCanvasZoom(tabId);
  const fitScale = useCanvasZoomStore((s) => (tabId ? s.fitScaleByTab[tabId] : undefined));
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const paneEl = rootRef.current?.closest<HTMLElement>('.jx-canvas');
    if (!zoomable || !tabId || !paneEl) return;
    let hovered = paneEl.matches(':hover');
    const onEnter = () => { hovered = true; };
    const onLeave = () => { hovered = false; };
    const onWheel = (event: WheelEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      event.preventDefault();
      // Trackpad pinch arrives as small ctrl+wheel deltas; scale continuously.
      const delta = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? event.deltaY * 16 : event.deltaY;
      // Store the unrounded scale: rounding each step would swallow slow pinches.
      useCanvasZoomStore.getState().setZoom(tabId, clampZoom(resolveZoom(tabId) * Math.exp(-delta * 0.0025)));
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      // Focus inside the pane owns the shortcut. Hover only counts while nothing
      // else holds focus, so the chat input and the other split pane keep theirs.
      const active = document.activeElement;
      const idle = !active || active === document.body;
      if (!paneEl.contains(active) && !(hovered && idle)) return;
      if (event.key === '=' || event.key === '+') stepZoom(tabId, 1);
      else if (event.key === '-' || event.key === '_') stepZoom(tabId, -1);
      else if (event.key === '0') useCanvasZoomStore.getState().setZoom(tabId, 'fit');
      else return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    paneEl.addEventListener('pointerenter', onEnter);
    paneEl.addEventListener('pointerleave', onLeave);
    paneEl.addEventListener('wheel', onWheel, { passive: false });
    window.addEventListener('keydown', onKeyDown, true);
    return () => {
      paneEl.removeEventListener('pointerenter', onEnter);
      paneEl.removeEventListener('pointerleave', onLeave);
      paneEl.removeEventListener('wheel', onWheel);
      window.removeEventListener('keydown', onKeyDown, true);
    };
  }, [tabId, zoomable]);

  if (!zoomable || !tabId) return null;
  const isFit = zoom === 'fit';
  const percent = Math.round((isFit ? fitScale ?? 1 : zoom) * 100);
  const setZoom = useCanvasZoomStore.getState().setZoom;
  return (
    <div ref={rootRef} className="jx-canvasZoom" role="group" aria-label={t('缩放')}>
      <button type="button" className="jx-canvas-actionBtn" aria-pressed={isFit}
        aria-label={t('适应宽度')} title={t('适应宽度 (Ctrl/⌘ 0)')}
        onClick={() => setZoom(tabId, 'fit')}><ColumnWidthOutlined /></button>
      <button type="button" className="jx-canvas-actionBtn" aria-label={t('缩小')} title={t('缩小 (Ctrl/⌘ −)')}
        onClick={() => stepZoom(tabId, -1)}><MinusOutlined /></button>
      <button type="button" className="jx-canvasZoom-value" aria-label={t('当前缩放 {percent}%，点击重置为 100%', { percent })}
        title={t('重置为 100%')} onClick={() => setZoom(tabId, 1)}>{percent}%</button>
      <button type="button" className="jx-canvas-actionBtn" aria-label={t('放大')} title={t('放大 (Ctrl/⌘ +)')}
        onClick={() => stepZoom(tabId, 1)}><PlusOutlined /></button>
    </div>
  );
}
