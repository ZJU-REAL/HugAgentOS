import { htmlPreviewDocument } from './htmlPreviewDocument';
import { useEffect, useRef, useState, useCallback } from 'react';
import { FileExclamationOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { getFileIconSrc } from '../../utils/fileIcon';
import type { CanvasArtifact } from '../../stores/canvasStore';
import { CitationMarkdownBlock } from '../citation';
import { authFetch } from '../../api';
import { withUrlParams } from '../../utils/artifactAccess';
import { ArtifactFileAction } from '../file/ArtifactFileAction';
import { PreviewFileTooLargeError, formatPreviewBytes, readLimitedBlob, readLimitedText } from '../../utils/filePreviewSafety';

function previewErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof PreviewFileTooLargeError) {
    return t('文件超过 {limit} 的安全预览上限，请下载后在本地打开', {
      limit: formatPreviewBytes(error.limitBytes),
    });
  }
  return error instanceof Error ? error.message : fallback;
}

/* ── Renderers ── */

export function DocxRenderer({ url, maxBytes }: { url: string; maxBytes: number }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const enhanceDocxLayout = useCallback((root: HTMLElement) => {
    const page = root.querySelector('section.docx');
    if (!page) return;

    const paragraphs = Array.from(page.querySelectorAll('p')) as HTMLParagraphElement[];
    let titleMarked = false;

    paragraphs.forEach((p) => {
      p.classList.remove('jx-docx-title', 'jx-docx-meta', 'jx-docx-subtitle');
      const text = (p.textContent || '').trim();
      if (!text) return;

      if (!titleMarked) {
        titleMarked = true;
        p.classList.add('jx-docx-title');
        return;
      }

      if (/^(来源|作者|日期|原文链接)\s*[：:]/.test(text)) {
        p.classList.add('jx-docx-meta');
        return;
      }

      if (/^(核心观点|摘要|结论|一、|二、|三、|四、|五、|六、|七、|八、|九、|十、)/.test(text)) {
        p.classList.add('jx-docx-subtitle');
      }
    });
  }, []);

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
        if (cancelled || !containerRef.current) return;
        containerRef.current.innerHTML = '';
        await renderAsync(blob, containerRef.current, undefined, {
          className: 'jx-canvas-docx-wrapper',
          inWrapper: true,
          ignoreWidth: false,
          ignoreHeight: true,
        });
        enhanceDocxLayout(containerRef.current);
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
  }, [enhanceDocxLayout, maxBytes, url]);

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

/* XlsxRenderer removed — replaced by UniverSpreadsheet */

export function PdfRenderer({ url }: { url: string }) {
  const inlineUrl = withUrlParams(url, { inline: '1' });
  return (
    <div className="jx-canvas-pdf">
      <object data={inlineUrl} type="application/pdf" className="jx-canvas-pdf-frame">
        <embed src={inlineUrl} type="application/pdf" className="jx-canvas-pdf-frame" />
      </object>
    </div>
  );
}

export function PptRenderer({ url, maxBytes }: { url: string; maxBytes: number }) {
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    const controller = new AbortController();

    (async () => {
      try {
        setLoading(true);
        setError(null);
        setPdfUrl(null);

        const resp = await authFetch(url, { signal: controller.signal });
        if (!resp.ok) {
          let detail = `HTTP ${resp.status}`;
          try {
            const payload = await resp.json() as { detail?: unknown };
            if (typeof payload.detail === 'string' && payload.detail.trim()) {
              detail = payload.detail;
            }
          } catch {
            // Keep the HTTP fallback text when the response is not JSON.
          }
          throw new Error(detail);
        }

        const blob = await readLimitedBlob(resp, maxBytes);
        objectUrl = URL.createObjectURL(blob);
        if (!cancelled) {
          setPdfUrl(objectUrl);
        }
      } catch (error: unknown) {
        if (!cancelled) {
          setError(previewErrorMessage(error, t('PPT 预览失败')));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [maxBytes, url]);

  if (error) return <div className="jx-canvas-error">{error}</div>;
  if (loading || !pdfUrl) return <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在渲染演示文稿…')}</span></div>;

  return (
    <div className="jx-canvas-pdf">
      <object data={pdfUrl} type="application/pdf" className="jx-canvas-pdf-frame">
        <embed src={pdfUrl} type="application/pdf" className="jx-canvas-pdf-frame" />
      </object>
    </div>
  );
}

export function ImageRenderer({ url, name }: { url: string; name: string }) {
  return (
    <div className="jx-canvas-image">
      <img src={url} alt={name} className="jx-canvas-image-img" />
    </div>
  );
}

function useRemoteText(url: string, maxBytes: number) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        setLoading(true);
        setError(null);
        const resp = await authFetch(url, { signal: controller.signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const text = await readLimitedText(resp, maxBytes);
        if (!cancelled) setContent(text);
      } catch (error: unknown) {
        if (!cancelled) setError(previewErrorMessage(error, t('文本加载失败')));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [maxBytes, url]);

  return { content, loading, error };
}

export function TextRenderer({ url, maxBytes }: { url: string; maxBytes: number }) {
  const { content, loading, error } = useRemoteText(url, maxBytes);

  if (error) return <div className="jx-canvas-error">{error}</div>;
  if (loading) return <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在加载…')}</span></div>;
  return (
    <div className="jx-canvas-text jx-canvas-fadeIn">
      <pre className="jx-canvas-text-pre">{content}</pre>
    </div>
  );
}

export function MarkdownRenderer({ url, maxBytes }: { url: string; maxBytes: number }) {
  const { content, loading, error } = useRemoteText(url, maxBytes);

  if (error) return <div className="jx-canvas-error">{error}</div>;
  if (loading) return <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在加载…')}</span></div>;
  return (
    <CitationMarkdownBlock
      text={content}
      isMarkdown
      citations={[]}
      className="jx-canvas-markdown jx-md jx-canvas-fadeIn"
    />
  );
}

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

export function UnknownRenderer({ name }: { name: string }) {
  return (
    <div className="jx-canvas-unknown">
      <img src={getFileIconSrc(name)} width="48" height="48" alt="" aria-hidden="true" />
      <p>{t('暂不支持预览此文件格式')}</p>
      <p className="jx-canvas-unknown-hint">{name}</p>
    </div>
  );
}

export function LargeFileRenderer({
  artifact,
  limitBytes,
  onDownload,
}: {
  artifact: CanvasArtifact;
  limitBytes: number;
  onDownload: () => void;
}) {
  return (
    <div className="jx-canvas-largeFile">
      <FileExclamationOutlined className="jx-canvas-largeFileIcon" />
      <p className="jx-canvas-largeFileTitle">{t('文件较大，已停止在线预览')}</p>
      <p className="jx-canvas-largeFileHint">
        {t('当前文件 {size}，超过此格式 {limit} 的安全预览上限。请使用下方文件操作在系统应用中查看。', {
          size: formatPreviewBytes(artifact.size || 0),
          limit: formatPreviewBytes(limitBytes),
        })}
      </p>
      <ArtifactFileAction file={artifact} className="jx-dlCard-btn" onDownload={onDownload} />
    </div>
  );
}
