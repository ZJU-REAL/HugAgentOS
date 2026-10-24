import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { t } from '../../../i18n';
import { authFetch } from '../../../api';
import { readLimitedBlob } from '../../../utils/filePreviewSafety';
import { previewErrorMessage } from './previewError';

// pdf.js and its bundled assets load only once a PDF is actually opened.
const PdfViewer = lazy(() => import('./PdfViewer').then((m) => ({ default: m.PdfViewer })));

async function responseError(resp: Response): Promise<Error> {
  try {
    const payload = await resp.json() as { detail?: unknown };
    if (typeof payload.detail === 'string' && payload.detail.trim()) return new Error(payload.detail);
  } catch {
    // Keep the HTTP fallback text when the response is not JSON.
  }
  return new Error(`HTTP ${resp.status}`);
}

/** Fetch a PDF (original or server-converted) within the preview size limit and show it in pdf.js. */
function RemotePdf({ url, maxBytes, loadingText, failureText }: {
  url: string; maxBytes: number; loadingText: string; failureText: string;
}) {
  // Keyed by url so a new file never shows the previous one's bytes or error.
  const [loaded, setLoaded] = useState<{ url: string; data?: Uint8Array; error?: string } | null>(null);
  const current = loaded?.url === url ? loaded : null;
  const showError = useCallback((error: string) => setLoaded({ url, error }), [url]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      const resp = await authFetch(url, { signal: controller.signal });
      if (!resp.ok) throw await responseError(resp);
      const blob = await readLimitedBlob(resp, maxBytes);
      const bytes = new Uint8Array(await blob.arrayBuffer());
      if (!cancelled) setLoaded({ url, data: bytes });
    })().catch((err: unknown) => {
      if (!cancelled) setLoaded({ url, error: previewErrorMessage(err, failureText) });
    });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [failureText, maxBytes, url]);

  if (current?.error) return <div className="jx-canvas-error">{current.error}</div>;
  const loading = <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{loadingText}</span></div>;
  if (!current?.data) return loading;
  return <Suspense fallback={loading}><PdfViewer data={current.data} onError={showError} /></Suspense>;
}

export function PdfRenderer({ url, maxBytes }: { url: string; maxBytes: number }) {
  return <RemotePdf url={url} maxBytes={maxBytes} loadingText={t('正在加载 PDF…')} failureText={t('PDF 预览失败')} />;
}

/** Office formats (.doc, .ppt(x), .rtf, OpenDocument…) the server converts to PDF. */
export function OfficeRenderer({ url, maxBytes }: { url: string; maxBytes: number }) {
  return <RemotePdf url={url} maxBytes={maxBytes} loadingText={t('正在转换文档…')} failureText={t('文档预览失败')} />;
}
