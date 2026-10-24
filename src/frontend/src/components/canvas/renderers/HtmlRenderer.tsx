import { useEffect, useState } from 'react';
import { t } from '../../../i18n';
import { authFetch } from '../../../api';
import { withUrlParams } from '../../../utils/artifactAccess';
import { PreviewFileTooLargeError, readLimitedText } from '../../../utils/filePreviewSafety';
import { htmlPreviewDocument } from '../htmlPreviewDocument';
import { previewErrorMessage } from './previewError';

export function HtmlRenderer(props: { url: string; version: string | number; maxBytes: number }) {
  // Cross-origin iframe navigation does not require CORS. Preserve support for
  // external artifact links; only our file API can provide readable HTTP errors.
  if (new URL(props.url, window.location.href).origin !== window.location.origin) {
    return <div className="jx-canvas-html"><iframe
      key={`${props.url}-${props.version}`}
      src={withUrlParams(props.url, { inline: '1', v: String(props.version) })}
      title="HTML Preview" sandbox="allow-scripts" className="jx-canvas-html-frame" />
    </div>;
  }
  return <FetchedHtmlRenderer {...props} />;
}

function FetchedHtmlRenderer({ url, version, maxBytes }: { url: string; version: string | number; maxBytes: number }) {
  const inlineUrl = withUrlParams(url, { inline: '1', v: String(version) });
  const [result, setResult] = useState<{ url: string; html?: string; error?: string }>();
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const response = await authFetch(inlineUrl, { signal: controller.signal });
        if (!response.ok) {
          const message = response.status === 401 ? t('请先登录后重试预览')
            : response.status === 403 ? t('没有权限预览此文件')
            : response.status === 404 ? t('文件已移动或删除')
            : `${t('预览加载失败')} (HTTP ${response.status})`;
          throw new Error(message);
        }
        const content = await readLimitedText(response, maxBytes);
        if (controller.signal.aborted) return;
        // srcDoc keeps API error documents out of the frame and treats the
        // fetched text as UTF-8. Retain relative resource resolution from the
        // original file URL. Scripts only run inside the opaque sandbox.
        const sourceUrl = new URL(response.url || inlineUrl, window.location.href).href;
        setResult({ url: inlineUrl, html: htmlPreviewDocument(content, sourceUrl) });
      } catch (error: unknown) {
        if (!controller.signal.aborted) setResult({ url: inlineUrl,
          error: error instanceof PreviewFileTooLargeError
            ? previewErrorMessage(error, t('预览加载失败'))
            : error instanceof Error ? error.message : t('预览加载失败'),
        });
      }
    })();
    return () => controller.abort();
  }, [inlineUrl, maxBytes]);
  if (result?.url !== inlineUrl) return <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在加载…')}</span></div>;
  if (result.error) return <div className="jx-canvas-error" role="alert">{result.error}</div>;
  return (
    <div className="jx-canvas-html">
      <iframe key={inlineUrl} srcDoc={result.html} title="HTML Preview"
        sandbox="allow-scripts" className="jx-canvas-html-frame" />
    </div>
  );
}
