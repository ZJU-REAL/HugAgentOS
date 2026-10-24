import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { t } from '../../../i18n';
import { authFetch } from '../../../api';
import { readLimitedBlob } from '../../../utils/filePreviewSafety';
import { useCanvasZoomTarget } from '../canvasZoom';
import { previewErrorMessage } from './previewError';

/** Grey margin around the pages; must match `.jx-canvas-docx .docx-wrapper` padding. */
const PAGE_GUTTER = 24;
/** 'fit' never blows a page up past this on a very wide (fullscreen) pane. */
const MAX_FIT_SCALE = 1.5;

export function DocxRenderer({ url, maxBytes }: { url: string; maxBytes: number }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Widest page at 100%, measured once per render of the document.
  const [pageWidth, setPageWidth] = useState(0);
  const [paneWidth, setPaneWidth] = useState(0);
  const { zoom, reportFitScale } = useCanvasZoomTarget(!error);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        setLoading(true);
        setError(null);
        const resp = await authFetch(url, { signal: controller.signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await readLimitedBlob(resp, maxBytes);
        const { renderAsync } = await import('docx-preview');
        const container = containerRef.current;
        if (cancelled || !container) return;
        container.innerHTML = '';
        // Real page geometry (width, margins, height, page breaks) so the
        // preview reads like the Word page; scaling happens on the wrapper.
        await renderAsync(blob, container, undefined, {
          inWrapper: true,
          ignoreWidth: false,
          ignoreHeight: false,
          breakPages: true,
        });
      } catch (error: unknown) {
        if (!cancelled) setError(previewErrorMessage(error, t('文档预览失败')));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [maxBytes, url]);

  // Track the scroll viewport (the canvas body), not the content, so
  // scaling the pages never feeds back into the measurement.
  useEffect(() => {
    const viewport = containerRef.current?.parentElement;
    if (!viewport) return;
    const observer = new ResizeObserver(() => setPaneWidth(viewport.clientWidth));
    observer.observe(viewport);
    setPaneWidth(viewport.clientWidth);
    return () => observer.disconnect();
  }, []);

  const fitScale = pageWidth && paneWidth
    ? Math.min(MAX_FIT_SCALE, paneWidth / (pageWidth + PAGE_GUTTER * 2))
    : 1;
  const scale = zoom === 'fit' ? fitScale : zoom;

  useEffect(() => { if (pageWidth && paneWidth) reportFitScale(Math.round(fitScale * 100) / 100); }, [fitScale, pageWidth, paneWidth, reportFitScale]);

  // Measure once the freshly rendered (still unscaled) pages become visible.
  useLayoutEffect(() => {
    if (loading) return;
    const pages = Array.from(containerRef.current?.querySelectorAll<HTMLElement>('section.docx') ?? []);
    setPageWidth(Math.max(0, ...pages.map((page) => page.offsetWidth)));
  }, [loading]);

  // docx-preview owns the wrapper's DOM, so the scale is applied imperatively.
  useLayoutEffect(() => {
    const wrapper = containerRef.current?.querySelector<HTMLElement>('.docx-wrapper');
    if (wrapper) wrapper.style.zoom = String(scale);
  }, [scale, loading]);

  if (error) return <div className="jx-canvas-error">{error}</div>;
  return (
    <>
      {loading && <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在渲染文档…')}</span></div>}
      {/* fadeIn class is only attached once rendering finishes, so the entrance
          animation starts exactly when the document becomes visible. */}
      <div
        ref={containerRef}
        className={`jx-canvas-docx${loading ? '' : ' jx-canvas-fadeIn'}`}
        style={{ display: loading ? 'none' : 'block' }}
      />
    </>
  );
}
