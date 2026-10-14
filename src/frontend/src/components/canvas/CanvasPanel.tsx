import { useEffect, useRef, useState, useCallback } from 'react';
import {
  SaveOutlined,
} from '@ant-design/icons';
import { t } from '../../i18n';
import { getFileIconSrc } from '../../utils/fileIcon';
import { message } from 'antd';
import { useCanvasStore } from '../../stores/canvasStore';
import type { CanvasArtifact } from '../../stores/canvasStore';
import { UniverSpreadsheet } from './UniverSpreadsheet';
import { CanvasTabBar } from './CanvasTabBar';
import type { UniverSpreadsheetHandle } from './UniverSpreadsheet';
import { overwriteFile, LOCAL_TARGET_HEADER } from '../../api';
import { artifactUrl, artifactOrigin, withUrlParams } from '../../utils/artifactAccess';
import { ArtifactFileAction } from '../file/ArtifactFileAction';
import {
  exceedsPreviewLimit,
  getPreviewLimitBytes,
} from '../../utils/filePreviewSafety';


/* ── helpers ── */

import { getFileCategory } from './canvasFileCategory';
import { FileTextPanel, type FileTextHandle, type FileTextState } from './FileTextPanel';
import { DocxRenderer, PdfRenderer, PptRenderer, ImageRenderer, TextRenderer, MarkdownRenderer, HtmlRenderer, UnknownRenderer, LargeFileRenderer } from './CanvasFileRenderers';

function getFileIcon(artifact: CanvasArtifact) { return <img src={getFileIconSrc(artifact.name)} width="20" height="20" alt="" aria-hidden="true" />; }

/* ── Main Panel ── */

export function CanvasPanel() {
  const {
    isOpen, activeView, artifact, updateArtifact, openSeq,
    activeTabId, setTabDirty, panelWidth, setPanelWidth,
  } = useCanvasStore();
  // Drag-resize in progress: kills the width transition (frame-accurate follow)
  // and mounts a full-screen transparent mask so iframes (PDF/HTML preview)
  // can't swallow mousemove events mid-drag.
  const [dragging, setDragging] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const dragCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => dragCleanupRef.current?.(), []);
  const resizeWidth = useCallback((width: number) => {
    const available = panelRef.current?.closest('.jx-appMainLayout')?.clientWidth ?? window.innerWidth;
    setPanelWidth(Math.max(400, Math.min(width, available - 420)));
  }, [setPanelWidth]);

  // xlsx-specific state
  const textRef = useRef<FileTextHandle>(null);
  const [htmlControls, setHtmlControls] = useState<HTMLDivElement | null>(null);
  const [textState, setTextState] = useState<FileTextState>({ canEdit: false, dirty: false, saving: false });
  const [xlsxDirty, setXlsxDirty] = useState(false);
  // 表格还在「正在加载电子表格…」时，Univer 实例尚未建好，exportXlsx 只会抛
  // "Univer not initialised"。保存按钮必须等它就绪才可点（问题 28）。
  const [xlsxReady, setXlsxReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const xlsxSaveButtonRef = useRef<HTMLButtonElement>(null);
  const xlsxSavingRef = useRef(false);
  const univerRef = useRef<UniverSpreadsheetHandle>(null);
  // Lock the initial load URL so saves don't trigger Univer reload
  const xlsxLoadUrlRef = useRef<string | null>(null);

  // Reset when a new file is opened (openSeq changes on every openCanvas call)
  useEffect(() => {
    setXlsxDirty(false);
    xlsxLoadUrlRef.current = null;
  }, [openSeq]);

  // 未保存状态提给 store：页签栏在关闭页签 / 切页签 / 收起面板前据此确认，
  // 因为这三条路径都会把本面板连同编辑缓冲一起卸载。
  useEffect(() => {
    if (activeTabId) setTabDirty(activeTabId, xlsxDirty);
  }, [activeTabId, setTabDirty, xlsxDirty]);

  // Intercept Ctrl+S to prevent browser "Save Page" and trigger xlsx save instead
  useEffect(() => {
    if (!isOpen || activeView !== 'file') return;
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        e.stopPropagation();
        xlsxSaveButtonRef.current?.click();
      }
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [activeView, isOpen]);

  const handleDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = panelRef.current?.offsetWidth || 700;

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

  const fileUrl = artifactUrl(artifact);
  const category = getFileCategory(artifact);
  const isXlsx = category === 'xlsx';
  const previewLimitBytes = getPreviewLimitBytes(category);
  const previewTooLarge = exceedsPreviewLimit(artifact.size, previewLimitBytes);
  const maxPreviewBytes = previewLimitBytes ?? Number.MAX_SAFE_INTEGER;

  const isTextFile = !previewTooLarge && ['text', 'markdown', 'html'].includes(category)
    && !artifact.file_id.startsWith('lpf_') && artifact.url.startsWith('/files/');

  // For xlsx: lock the load URL so saves (which update artifact.url) don't reload Univer
  if (isXlsx && !xlsxLoadUrlRef.current) {
    xlsxLoadUrlRef.current = fileUrl;
  }
  const xlsxLoadUrl = xlsxLoadUrlRef.current || fileUrl;

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
    if (!univerRef.current || !artifact || xlsxSavingRef.current) return;
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
      const result = await overwriteFile(artifact.file_id, file, artifactOrigin(artifact) === 'local' ? { [LOCAL_TARGET_HEADER]: 'local' } : {});
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
      case 'docx': return <DocxRenderer url={fileUrl} maxBytes={maxPreviewBytes} />;
      case 'xlsx':
        return (
          <UniverSpreadsheet
            ref={univerRef}
            url={xlsxLoadUrl}
            fileName={artifact.name}
            maxBytes={maxPreviewBytes}
            onDirty={setXlsxDirty}
            onReadyChange={setXlsxReady}
          />
        );
      case 'pdf': return <PdfRenderer url={fileUrl} />;
      case 'ppt': return <PptRenderer url={artifactUrl(artifact, { preview: true, params: { format: 'pdf' } })} maxBytes={maxPreviewBytes} />;
      case 'image': return <ImageRenderer url={fileUrl} name={artifact.name} />;
      case 'text': return <TextRenderer url={textUrl} maxBytes={maxPreviewBytes} />;
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
          resizeWidth((panelRef.current?.clientWidth || panelWidth || 700) + (event.key === 'ArrowLeft' ? 32 : -32));
        }}
      />
      {/* 拖拽期间的全屏透明遮罩：防 iframe 吞 mousemove */}
      {dragging && <div className="jx-canvas-dragMask" />}
      <CanvasTabBar />
      {/* Header */}
      <div className="jx-canvas-header">
        <div className="jx-canvas-header-left">
          <span className="jx-canvas-fileIcon">{getFileIcon(artifact)}</span>
          <div className="jx-canvas-fileMeta">
            <span className="jx-canvas-fileName" title={artifact.name}>
              {artifact.name}
              {isXlsx && xlsxDirty && <span className="jx-canvas-editedBadge jx-anim-statusIn">({t('已编辑')})</span>}
            </span>
          </div>
        </div>
        <div className="jx-canvas-header-actions">
          {isTextFile && category === 'html' && <div ref={setHtmlControls} className="jx-canvas-htmlControls" />}
          {isXlsx && !previewTooLarge && (
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
          <ArtifactFileAction file={artifact} iconOnly className="jx-canvas-actionBtn"
            onDownload={handleDownload} disabled={isXlsx && xlsxDirty} />
        </div>
      </div>

      {/* Content */}
      <div className="jx-canvas-body">
        {isTextFile ? (
          <FileTextPanel headerControls={htmlControls} ref={textRef} onStateChange={setTextState} key={`${activeTabId}-${openSeq}`} artifact={artifact} preview={renderContent} />
        ) : renderContent()}
      </div>
    </div>
  );
}
