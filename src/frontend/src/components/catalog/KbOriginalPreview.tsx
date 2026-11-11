import { useEffect, useState } from 'react';
import { Button, message } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { authFetch } from '../../api';
import { kbOriginalUrl, type KbOriginalKind } from './kbOriginal';
import { getPreviewLimitBytes, readLimitedBlob } from '../../utils/filePreviewSafety';
import { saveBlob } from '../../utils/download';
import { StandaloneZoomContext } from '../canvas/canvasZoom';
import { DocxRenderer } from '../canvas/renderers/DocxRenderer';
import { PdfRenderer } from '../canvas/renderers/PdfRenderer';
import { TextRenderer } from '../canvas/renderers/TextRenderers';
import { previewErrorMessage } from '../canvas/renderers/previewError';

/** 图片走鉴权请求，<img src> 带不上登录态，所以先取成 blob URL。 */
function AuthImage({ url, maxBytes }: { url: string; maxBytes: number }) {
  const [loaded, setLoaded] = useState<{ url: string; src?: string; error?: string } | null>(null);
  const current = loaded?.url === url ? loaded : null;

  useEffect(() => {
    let objectUrl: string | null = null;
    const controller = new AbortController();
    (async () => {
      const resp = await authFetch(url, { signal: controller.signal });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      objectUrl = URL.createObjectURL(await readLimitedBlob(resp, maxBytes));
      if (controller.signal.aborted) URL.revokeObjectURL(objectUrl);
      else setLoaded({ url, src: objectUrl });
    })().catch((err: unknown) => {
      if (!controller.signal.aborted) setLoaded({ url, error: previewErrorMessage(err, t('原文预览失败')) });
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [maxBytes, url]);

  if (current?.error) return <div className="jx-canvas-error">{current.error}</div>;
  if (!current?.src) return <div className="jx-canvas-loading" aria-busy="true"><div className="jx-canvas-spinner" /><span>{t('正在加载原文…')}</span></div>;
  return <img className="jx-kbOriginalImage" src={current.src} alt="" />;
}

/**
 * 外接知识库（WeKnora）文档的原文预览：复用画布的渲染器，按原版式展示 PDF / Word / 文本。
 * 放在 StandaloneZoomContext 里，渲染器固定适应宽度，不会去读写画布页签的缩放。
 */
export function KbOriginalPreview({
  kbId, docId, name, kind,
}: { kbId: string; docId: string; name: string; kind: KbOriginalKind }) {
  const url = kbOriginalUrl(kbId, docId);
  const maxBytes = getPreviewLimitBytes(kind) ?? 0;
  return (
    <StandaloneZoomContext.Provider value>
      {kind === 'pdf' && <PdfRenderer url={url} maxBytes={maxBytes} />}
      {kind === 'docx' && <DocxRenderer url={url} maxBytes={maxBytes} />}
      {kind === 'image' && <AuthImage url={url} maxBytes={maxBytes} />}
      {(kind === 'text' || kind === 'markdown' || kind === 'csv') && <TextRenderer url={url} name={name} maxBytes={maxBytes} />}
    </StandaloneZoomContext.Provider>
  );
}

/** 下载原始文件（走鉴权请求后另存，文件名沿用知识库里的原名）。 */
export function KbOriginalDownloadButton({
  kbId, docId, filename,
}: { kbId: string; docId: string; filename: string }) {
  const [busy, setBusy] = useState(false);
  const onClick = async () => {
    setBusy(true);
    try {
      const resp = await authFetch(kbOriginalUrl(kbId, docId));
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      saveBlob(await resp.blob(), filename || docId);
    } catch {
      message.error(t('原文下载失败'));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button size="small" icon={<DownloadOutlined />} loading={busy} onClick={onClick}>
      {t('下载原文')}
    </Button>
  );
}
