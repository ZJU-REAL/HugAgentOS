import { useEffect, useRef, useState, useCallback } from 'react';
import {
  CloseOutlined, DownloadOutlined, ExpandOutlined, CompressOutlined, SaveOutlined,
  CheckOutlined,
} from '@ant-design/icons';
import { t } from '../../i18n';
import { getFileIconSrc } from '../../utils/fileIcon';
import { message, Modal } from 'antd';
import { useCanvasStore } from '../../stores/canvasStore';
import type { CanvasArtifact } from '../../stores/canvasStore';
import { UniverSpreadsheet } from './UniverSpreadsheet';
import type { UniverSpreadsheetHandle } from './UniverSpreadsheet';
import { overwriteFile } from '../../api';

const effectiveApiUrl = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

/* ── helpers ── */

function getFileExt(name: string): string {
  return (name.split('.').pop() || '').toLowerCase();
}

function getFileCategory(artifact: CanvasArtifact): 'docx' | 'xlsx' | 'pdf' | 'ppt' | 'image' | 'text' | 'html' | 'unknown' {
  const ext = getFileExt(artifact.name);
  const mime = artifact.mime_type || '';
  if (ext === 'docx' || ext === 'doc' || mime.includes('wordprocessingml')) return 'docx';
  if (ext === 'xlsx' || ext === 'xls' || mime.includes('spreadsheetml')) return 'xlsx';
  if (ext === 'pdf' || mime === 'application/pdf') return 'pdf';
  if (ext === 'pptx' || ext === 'ppt' || mime.includes('presentationml') || mime.includes('powerpoint')) return 'ppt';
  if (mime.startsWith('image/')) return 'image';
  if (ext === 'html' || ext === 'htm' || mime === 'text/html') return 'html';
  if (['txt', 'md', 'csv', 'json', 'xml', 'yaml', 'yml', 'log', 'py', 'js', 'ts', 'tsx', 'jsx', 'css', 'sql', 'sh', 'bat', 'ini', 'conf', 'toml'].includes(ext) || mime.startsWith('text/')) return 'text';
  return 'unknown';
}

function getFileIcon(artifact: CanvasArtifact) {
  return <img src={getFileIconSrc(artifact.name)} width="20" height="20" alt="" aria-hidden="true" />;
}

function formatSize(bytes?: number): string {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/* ── Renderers ── */

function DocxRenderer({ url }: { url: string }) {
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
    (async () => {
      try {
        setLoading(true);
        setError(null);
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
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
      } catch (e: any) {
        if (!cancelled) setError(e.message || t('文档预览失败'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [enhanceDocxLayout, url]);

  if (error) return <div className="jx-canvas-error">{error}</div>;
  return (
    <>
      {loading && <div className="jx-canvas-loading"><div className="jx-canvas-spinner" /><span>{t('正在渲染文档…')}</span></div>}
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

function PdfRenderer({ url }: { url: string }) {
  const inlineUrl = url.includes('?') ? `${url}&inline=1` : `${url}?inline=1`;
  return (
    <div className="jx-canvas-pdf">
      <object data={inlineUrl} type="application/pdf" className="jx-canvas-pdf-frame">
        <embed src={inlineUrl} type="application/pdf" className="jx-canvas-pdf-frame" />
      </object>
    </div>
  );
}

function PptRenderer({ url }: { url: string }) {
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;

    (async () => {
      try {
        setLoading(true);
        setError(null);
        setPdfUrl(null);

        const resp = await fetch(url);
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

        const blob = await resp.blob();
        objectUrl = URL.createObjectURL(blob);
        if (!cancelled) {
          setPdfUrl(objectUrl);
        }
      } catch (e: any) {
        if (!cancelled) {
          setError(e.message || t('PPT 预览失败'));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [url]);

  if (error) return <div className="jx-canvas-error">{error}</div>;
  if (loading || !pdfUrl) return <div className="jx-canvas-loading"><div className="jx-canvas-spinner" /><span>{t('正在渲染演示文稿…')}</span></div>;

  return (
    <div className="jx-canvas-pdf">
      <object data={pdfUrl} type="application/pdf" className="jx-canvas-pdf-frame">
        <embed src={pdfUrl} type="application/pdf" className="jx-canvas-pdf-frame" />
      </object>
    </div>
  );
}

function ImageRenderer({ url, name }: { url: string; name: string }) {
  return (
    <div className="jx-canvas-image">
      <img src={url} alt={name} className="jx-canvas-image-img" />
    </div>
  );
}

function TextRenderer({ url }: { url: string }) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setLoading(true);
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const text = await resp.text();
        if (!cancelled) setContent(text);
      } catch (e: any) {
        if (!cancelled) setError(e.message || t('文本加载失败'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [url]);

  if (error) return <div className="jx-canvas-error">{error}</div>;
  if (loading) return <div className="jx-canvas-loading"><div className="jx-canvas-spinner" /><span>{t('正在加载…')}</span></div>;
  return (
    <div className="jx-canvas-text jx-canvas-fadeIn">
      <pre className="jx-canvas-text-pre">{content}</pre>
    </div>
  );
}

function HtmlRenderer({ url, version }: { url: string; version: string | number }) {
  // sandbox="allow-scripts" without allow-same-origin: iframe runs as a null
  // origin, so inline JS cannot read parent cookies / localStorage or call our
  // /api with the user's session. Subresources (img/script/link src) still
  // load via plain GET.
  //
  // ``inline=1`` flips Content-Disposition from "attachment" → "inline" on
  // /api/files/<id>; without it the browser treats the response as a download
  // and the iframe stays blank. Same workaround as PdfRenderer.
  //
  // ``v={version}`` is a cache-buster: when an in-place Edit updates an
  // artifact (same file_id, new content), the URL alone wouldn't change and
  // the browser would serve the cached bytes. We tie ``version`` to (openSeq
  // + artifact.size) so any re-open or in-place size change yields a fresh
  // URL → forces a network fetch. The ``key`` prop on the iframe element
  // additionally forces React to fully remount the iframe so the new src
  // takes effect even when the parent component doesn't unmount.
  const sep = url.includes('?') ? '&' : '?';
  const inlineUrl = `${url}${sep}inline=1&v=${encodeURIComponent(version)}`;
  return (
    <div className="jx-canvas-html">
      <iframe
        key={String(version)}
        src={inlineUrl}
        title="HTML Preview"
        sandbox="allow-scripts"
        className="jx-canvas-html-frame"
      />
    </div>
  );
}

function UnknownRenderer({ name }: { name: string }) {
  return (
    <div className="jx-canvas-unknown">
      <img src={getFileIconSrc(name)} width="48" height="48" alt="" aria-hidden="true" />
      <p>{t('暂不支持预览此文件格式')}</p>
      <p className="jx-canvas-unknown-hint">{name}</p>
    </div>
  );
}

/* ── Main Panel ── */

export function CanvasPanel() {
  const { isOpen, artifact, closeCanvas, updateArtifact, openSeq } = useCanvasStore();
  const [expanded, setExpanded] = useState(false);
  const [dragWidth, setDragWidth] = useState<number | null>(null);
  // Drag-resize in progress: kills the width transition (frame-accurate follow)
  // and mounts a full-screen transparent mask so iframes (PDF/HTML preview)
  // can't swallow mousemove events mid-drag.
  const [dragging, setDragging] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // xlsx-specific state
  const [xlsxDirty, setXlsxDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  // Brief ✓ feedback on the save button after a successful save
  const [justSaved, setJustSaved] = useState(false);
  const univerRef = useRef<UniverSpreadsheetHandle>(null);
  // Lock the initial load URL so saves don't trigger Univer reload
  const xlsxLoadUrlRef = useRef<string | null>(null);

  // Reset when a new file is opened (openSeq changes on every openCanvas call)
  useEffect(() => {
    setXlsxDirty(false);
    xlsxLoadUrlRef.current = null;
  }, [openSeq]);

  // Intercept Ctrl+S to prevent browser "Save Page" and trigger xlsx save instead
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        e.stopPropagation();
      }
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [isOpen]);

  const handleDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = panelRef.current?.offsetWidth || 700;

    const onMove = (ev: MouseEvent) => {
      const delta = startX - ev.clientX; // dragging left = wider
      const newWidth = Math.max(400, Math.min(startWidth + delta, window.innerWidth * 0.85));
      setDragWidth(newWidth);
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      setDragging(false);
    };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    setDragging(true);
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, []);

  if (!isOpen || !artifact) return null;

  const fileUrl = `${effectiveApiUrl}${artifact.url}`;
  const category = getFileCategory(artifact);
  const isXlsx = category === 'xlsx';

  // For xlsx: lock the load URL so saves (which update artifact.url) don't reload Univer
  if (isXlsx && !xlsxLoadUrlRef.current) {
    xlsxLoadUrlRef.current = fileUrl;
  }
  const xlsxLoadUrl = xlsxLoadUrlRef.current || fileUrl;

  const handleDownload = async () => {
    // If xlsx has been edited, export the current state instead of re-downloading the original
    if (isXlsx && xlsxDirty && univerRef.current) {
      try {
        const file = await univerRef.current.exportXlsx();
        const url = URL.createObjectURL(file);
        const a = document.createElement('a');
        a.href = url;
        a.download = artifact.name;
        a.click();
        URL.revokeObjectURL(url);
        message.success(t('开始下载'));
      } catch {
        message.error(t('导出失败'));
      }
      return;
    }
    const a = document.createElement('a');
    a.href = fileUrl;
    a.download = artifact.name;
    a.click();
    message.success(t('开始下载'));
  };

  const handleSave = async () => {
    if (!univerRef.current || !artifact) return;
    try {
      setSaving(true);
      const exported = await univerRef.current.exportXlsx();
      const file = new File([exported], artifact.name, { type: exported.type });
      // Overwrite in-place — same file_id, same URL, content updated on server
      const result = await overwriteFile(artifact.file_id, file);
      updateArtifact({ size: result.size });
      setXlsxDirty(false);
      univerRef.current?.resetDirty();
      setJustSaved(true);
      window.setTimeout(() => setJustSaved(false), 800);
      message.success(t('保存成功'));
    } catch (e: any) {
      message.error(e.message || t('保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const handleClose = () => {
    if (isXlsx && xlsxDirty) {
      Modal.confirm({
        title: t('有未保存的修改'),
        content: t('关闭后编辑内容将丢失，确定关闭？'),
        okText: t('关闭'),
        cancelText: t('取消'),
        okButtonProps: { danger: true },
        onOk: closeCanvas,
      });
      return;
    }
    closeCanvas();
  };

  const renderContent = () => {
    switch (category) {
      case 'docx': return <DocxRenderer url={fileUrl} />;
      case 'xlsx':
        return (
          <UniverSpreadsheet
            ref={univerRef}
            url={xlsxLoadUrl}
            onDirty={setXlsxDirty}
          />
        );
      case 'pdf': return <PdfRenderer url={fileUrl} />;
      case 'ppt': return <PptRenderer url={`${fileUrl}/preview?format=pdf`} />;
      case 'image': return <ImageRenderer url={fileUrl} name={artifact.name} />;
      case 'text': return <TextRenderer url={fileUrl} />;
      case 'html': return <HtmlRenderer url={fileUrl} version={`${openSeq}-${artifact.size ?? 0}`} />;
      default: return <UnknownRenderer name={artifact.name} />;
    }
  };

  return (
    <div
      ref={panelRef}
      className={`jx-canvas jx-canvas--${category} ${expanded ? 'jx-canvas--expanded' : ''}${dragging ? ' jx-canvas--dragging' : ''}`}
      style={dragWidth && !expanded ? { width: dragWidth } : undefined}
    >
      {/* Drag handle */}
      <div className="jx-canvas-dragHandle" onMouseDown={handleDragStart} />
      {/* 拖拽期间的全屏透明遮罩：防 iframe 吞 mousemove */}
      {dragging && <div className="jx-canvas-dragMask" />}
      {/* Header */}
      <div className="jx-canvas-header">
        <div className="jx-canvas-header-left">
          <span className="jx-canvas-fileIcon">{getFileIcon(artifact)}</span>
          <div className="jx-canvas-fileMeta">
            <span className="jx-canvas-fileName">
              {artifact.name}
              {isXlsx && xlsxDirty && <span className="jx-canvas-editedBadge jx-anim-statusIn">({t('已编辑')})</span>}
            </span>
            {artifact.size && <span className="jx-canvas-fileSize">{formatSize(artifact.size)}</span>}
          </div>
        </div>
        <div className="jx-canvas-header-actions">
          {isXlsx && (
            <button
              className="jx-canvas-actionBtn jx-canvas-saveBtn"
              onClick={handleSave}
              disabled={saving}
              title={t('保存文件')}
            >
              {justSaved
                ? <CheckOutlined className="jx-anim-statusIn" />
                : <SaveOutlined />}
            </button>
          )}
          <button className="jx-canvas-actionBtn" onClick={handleDownload} title={t('下载文件')}>
            <DownloadOutlined />
          </button>
          <button className="jx-canvas-actionBtn" onClick={() => setExpanded(!expanded)} title={expanded ? t('收起') : t('展开')}>
            {expanded ? <CompressOutlined /> : <ExpandOutlined />}
          </button>
          <button className="jx-canvas-actionBtn jx-canvas-closeBtn" onClick={handleClose} title={t('关闭预览')}>
            <CloseOutlined />
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="jx-canvas-body">
        {renderContent()}
      </div>
    </div>
  );
}
