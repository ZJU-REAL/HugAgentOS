import { useCanvasPane, useCanvasPaneId } from './useCanvasPane';
import { useEffect, useRef, useState, useCallback, type CSSProperties } from 'react';
import {
  SaveOutlined,
} from '@ant-design/icons';
import { t } from '../../i18n';
import { CanvasFileHeader } from './CanvasFileHeader';
import { useCanvasGuard } from './useCanvasGuard';
import { message } from 'antd';
import { UniverSpreadsheet } from './UniverSpreadsheet';
import { CanvasTabBar } from './CanvasTabBar';
import type { UniverSpreadsheetHandle } from './UniverSpreadsheet';
import { overwriteFile, LOCAL_TARGET_HEADER } from '../../api';
import { clampPanelWidth } from '../../stores/canvasStore';
import { uploadSpaceFile } from '../myspace/spaceFilesApi';
import { artifactUrl, artifactOrigin, withUrlParams } from '../../utils/artifactAccess';
import {
  exceedsPreviewLimit,
  getPreviewLimitBytes,
} from '../../utils/filePreviewSafety';


import { ArchiveBrowser } from '../file/ArchiveBrowser';
import { FilePreviewPane } from '../file/FilePreviewPane';
import { MediaPlayer } from '../file/MediaPlayer';

/* ── helpers ── */

import { getFilePreviewKind } from '../../utils/filePreviewKind';
import { useCanvasZoomTarget } from './canvasZoom';
import { useCanvasSelection } from './selection/useCanvasSelection';
import { FileTextPanel, type FileTextHandle, type FileTextState } from './FileTextPanel';
import { CanvasFind } from './find';
import { CanvasOutline } from './outline';
import { DocxRenderer, PdfRenderer, OfficeRenderer, ImageRenderer, TextRenderer, MarkdownRenderer, HtmlRenderer, UnknownRenderer, LargeFileRenderer } from './renderers';

/* ── Main Panel ── */

export function CanvasPanel() {
  const paneId = useCanvasPaneId();
  const activePane = useCanvasPane(s => s.activePane);
  const {
    isOpen, activeView, artifact, updateArtifact, openSeq,
    activeTabId, setTabDirty, panelWidth, setPanelWidth, reloadTab,
  } = useCanvasPane();
  const guardDirty = useCanvasGuard();
  // Drag-resize in progress: kills the width transition (frame-accurate follow)
  // and mounts a full-screen transparent mask so iframes (PDF/HTML preview)
  // can't swallow mousemove events mid-drag.
  const [dragging, setDragging] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const dragCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => dragCleanupRef.current?.(), []);
  const resizeWidth = useCallback((width: number) => {
    const available = panelRef.current?.closest('.jx-appMainLayout')?.clientWidth ?? window.innerWidth;
    setPanelWidth(clampPanelWidth(width, available));
  }, [setPanelWidth]);

  // xlsx-specific state
  const textRef = useRef<FileTextHandle>(null);
  const [htmlControls, setHtmlControls] = useState<HTMLDivElement | null>(null);
  const [textState, setTextState] = useState<FileTextState>({ canEdit: false, dirty: false, saving: false });
  // Read-only text and markdown previews scale through the body's --canvas-zoom
  // (see canvas.css); the editors and the too-large fallback ignore it.
  const textKind = artifact ? getFilePreviewKind(artifact) : null;
  const cssZoomed = !!artifact && !!textKind && ['text', 'json', 'csv', 'markdown'].includes(textKind)
    && !textState.canEdit && !exceedsPreviewLimit(artifact.size, getPreviewLimitBytes(textKind));
  const { zoom: cssZoom } = useCanvasZoomTarget(cssZoomed);
  const [xlsxDirty, setXlsxDirty] = useState(false);
  // 表格还在「正在加载电子表格…」时，Univer 实例尚未建好，exportXlsx 只会抛
  // "Univer not initialised"。保存按钮必须等它就绪才可点（问题 28）。
  const [xlsxReady, setXlsxReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const xlsxSaveButtonRef = useRef<HTMLButtonElement>(null);
  const xlsxSavingRef = useRef(false);
  const univerRef = useRef<UniverSpreadsheetHandle>(null);
  const selection = useCanvasSelection(artifact);

  // Reset when a new file is opened (openSeq changes on every openCanvas call)
  useEffect(() => {
    setXlsxDirty(false);
  }, [openSeq]);

  // 未保存状态提给 store：页签栏在关闭页签 / 切页签 / 收起面板前据此确认，
  // 因为这三条路径都会把本面板连同编辑缓冲一起卸载。
  useEffect(() => {
    if (activeTabId && artifact && getFilePreviewKind(artifact) === 'xlsx') setTabDirty(activeTabId, xlsxDirty);
  }, [activeTabId, artifact, setTabDirty, xlsxDirty]);

  // Intercept Ctrl+S to prevent browser "Save Page" and trigger xlsx save instead
  useEffect(() => {
    if (!isOpen || activeView !== 'file' || activePane !== paneId) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        e.stopPropagation();
        xlsxSaveButtonRef.current?.click();
      }
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [activeView, isOpen, activePane, paneId]);

  const handleDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = panelRef.current?.closest('.jx-canvasSplit')?.clientWidth || panelRef.current?.offsetWidth || 700;

    const onMove = (ev: MouseEvent) => {
      const delta = startX - ev.clientX; // dragging left = wider
      resizeWidth(startWidth + delta);
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      setDragging(false);
      dragCleanupRef.current = null;
    };
    dragCleanupRef.current?.();
    dragCleanupRef.current = onUp;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    setDragging(true);
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [resizeWidth]);

  if (!isOpen || activeView !== 'file' || !artifact) return null;

  const fileUrl = artifactUrl(artifact, { params: { v: String(openSeq) } });
  const category = getFilePreviewKind(artifact);
  const isXlsx = category === 'xlsx';
  const previewLimitBytes = getPreviewLimitBytes(category);
  const previewTooLarge = exceedsPreviewLimit(artifact.size, previewLimitBytes);
  const maxPreviewBytes = previewLimitBytes ?? Number.MAX_SAFE_INTEGER;

  const isTextFile = !previewTooLarge && ['text', 'json', 'csv', 'markdown', 'html'].includes(category)
    && (!!artifact.space || (!artifact.file_id.startsWith('lpf_') && artifact.url.startsWith('/files/')));

  const handleDownload = async () => {
    // If xlsx has been edited, export the current state instead of re-downloading the original
    if (isXlsx && xlsxDirty && xlsxReady && univerRef.current) {
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
    if (!univerRef.current || !artifact || artifact.space?.writable === false || xlsxSavingRef.current) return;
    if (!xlsxReady) {
      message.warning(t('电子表格尚未加载完成，请稍候再保存'));
      return;
    }
    try {
      xlsxSavingRef.current = true;
      setSaving(true);
      const exported = await univerRef.current.exportXlsx();
      const file = new File([exported], artifact.name, { type: exported.type });
      // Overwrite in-place — same file_id, same URL, content updated on server
      const result = artifact.space
        ? (await uploadSpaceFile(artifact.space.id, artifact.space.path, file), { size: file.size })
        : await overwriteFile(artifact.file_id, file, artifactOrigin(artifact) === 'local' ? { [LOCAL_TARGET_HEADER]: 'local' } : {});
      updateArtifact({ size: result.size });
      setXlsxDirty(false);
      univerRef.current?.resetDirty();
    } catch (error: unknown) {
      message.error(error instanceof Error ? error.message : t('保存失败'));
    } finally {
      xlsxSavingRef.current = false;
      setSaving(false);
    }
  };

  const renderContent = (revision?: string) => {
    const textUrl = revision ? withUrlParams(fileUrl, { v: revision }) : fileUrl;
    if (!fileUrl) return <div className="jx-canvas-error">{t('缺少下载链接')}</div>;
    if (previewTooLarge && previewLimitBytes !== null) {
      return (
        <LargeFileRenderer
          artifact={artifact}
          limitBytes={previewLimitBytes}
          onDownload={() => { void handleDownload(); }}
        />
      );
    }
    switch (category) {
      case 'archive': return <ArchiveBrowser key={activeTabId + ':' + openSeq} file={artifact} renderMember={item => <FilePreviewPane item={item} />} />;
      case 'docx': return <DocxRenderer url={fileUrl} maxBytes={maxPreviewBytes} />;
      case 'xlsx':
        return (
          <UniverSpreadsheet
            ref={univerRef}
            url={fileUrl}
            fileName={artifact.name}
            readOnly={artifact.space?.writable === false}
            maxBytes={maxPreviewBytes}
            onDirty={setXlsxDirty}
            onReadyChange={setXlsxReady}
          />
        );
      case 'pdf': return <PdfRenderer url={fileUrl} maxBytes={maxPreviewBytes} />;
      // Legacy .doc, PPT(X), OpenDocument: the server converts to PDF.
      case 'office': return <OfficeRenderer url={artifactUrl(artifact, { preview: true, params: { format: 'pdf' } })} maxBytes={maxPreviewBytes} />;
      case 'image': return <ImageRenderer url={fileUrl} name={artifact.name} />;
      // inline: My Space only types the response (and allows playback) when asked to.
      case 'video':
      case 'audio': return <MediaPlayer kind={category} src={withUrlParams(fileUrl, { inline: '1' })} name={artifact.name} />;
      case 'json':
      case 'csv':
      case 'text': return <TextRenderer url={textUrl} name={artifact.name} maxBytes={maxPreviewBytes} />;
      case 'markdown': return <MarkdownRenderer url={textUrl} maxBytes={maxPreviewBytes} />;
      case 'html': return <HtmlRenderer url={fileUrl} version={revision || `${openSeq}-${artifact.size ?? 0}`} maxBytes={maxPreviewBytes} />;
      default: return <UnknownRenderer name={artifact.name} />;
    }
  };

  return (
    <div
      ref={panelRef}
      className={`jx-canvas jx-canvas--${category}${dragging ? ' jx-canvas--dragging' : ''}`}
    >
      {/* Drag handle */}
      <div
        className="jx-canvas-dragHandle"
        role="separator"
        aria-orientation="vertical"
        aria-label={t('右侧面板')}
        tabIndex={0}
        onMouseDown={handleDragStart}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
          event.preventDefault();
          resizeWidth((panelRef.current?.closest('.jx-canvasSplit')?.clientWidth || panelWidth || 700) + (event.key === 'ArrowLeft' ? 32 : -32));
        }}
      />
      {/* 拖拽期间的全屏透明遮罩：防 iframe 吞 mousemove */}
      {dragging && <div className="jx-canvas-dragMask" />}
      <CanvasTabBar />
      <CanvasFileHeader artifact={artifact} revision={openSeq}
        dirty={xlsxDirty || textState.dirty} saving={saving || textState.saving}
        onReload={() => guardDirty(() => { if (activeTabId) reloadTab(activeTabId); })}
        onDownload={isXlsx && xlsxDirty ? () => { void handleDownload(); } : undefined}>
          {!previewTooLarge && !textState.canEdit && ['pdf', 'office', 'docx', 'markdown', 'text', 'json', 'csv'].includes(category) && <CanvasFind />}
          {!previewTooLarge && ['pdf', 'office', 'docx', 'markdown'].includes(category) && <CanvasOutline />}
          {isTextFile && category === 'html' && <div ref={setHtmlControls} className="jx-canvas-htmlControls" />}
          {isXlsx && !previewTooLarge && artifact.space?.writable !== false && (
            <button
              className="jx-canvas-actionBtn jx-canvas-saveBtn"
              ref={xlsxSaveButtonRef}
              aria-label={t('保存')}
              onClick={handleSave}
              disabled={saving || !xlsxReady}
              title={xlsxReady ? t('保存文件') : t('电子表格加载中，暂不可保存')}
            >
              <SaveOutlined />
            </button>
          )}
          {isTextFile && textState.canEdit && (
            <button className="jx-canvas-actionBtn jx-canvas-saveBtn"
              aria-label={t('保存')} title={t('保存')}
              disabled={!textState.dirty || textState.saving}
              onClick={() => { void textRef.current?.save(); }}>
              <SaveOutlined />
            </button>
          )}
      </CanvasFileHeader>

      {/* Content */}
      <div className="jx-canvas-body" key={activeTabId + ":" + openSeq} ref={selection.ref}
        style={cssZoomed && cssZoom !== 'fit' ? { '--canvas-zoom': cssZoom } as CSSProperties : undefined}>
        {isTextFile ? (
          <FileTextPanel headerControls={htmlControls} ref={textRef} onStateChange={setTextState} key={`${activeTabId}-${openSeq}`} artifact={artifact} preview={renderContent} />
        ) : renderContent()}
      </div>
      {selection.toolbar}
    </div>
  );
}
