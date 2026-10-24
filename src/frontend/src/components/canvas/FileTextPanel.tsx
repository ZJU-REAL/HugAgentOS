import { useCanvasPane, useCanvasPaneId } from './useCanvasPane';
import { createPortal } from 'react-dom';
import { EyeOutlined, CodeOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState, useImperativeHandle, useMemo, type ReactNode, type Ref } from 'react';
import { Button } from 'antd';
import { apiRequest, unwrapData } from '../../api';
import { t } from '../../i18n';
import { type CanvasArtifact } from '../../stores/canvasStore';
import { artifactOrigin, artifactUrl } from '../../utils/artifactAccess';
import './FileTextPanel.css';
import { MarkdownFileEditor } from './MarkdownFileEditor';
import { getFilePreviewKind } from '../../utils/filePreviewKind';
import { htmlPreviewDocument } from './htmlPreviewDocument';

export interface FileTextState { canEdit: boolean; dirty: boolean; saving: boolean }
export interface FileTextHandle { save: () => Promise<void> }


interface TextDocument {
  file_id: string;
  name: string;
  content: string;
  revision: string;
  size: number;
  can_edit: boolean;
}

export function FileTextPanel({ artifact, preview, ref, onStateChange, headerControls }: {
  artifact: CanvasArtifact;
  headerControls?: HTMLElement | null;
  ref?: Ref<FileTextHandle>;
  onStateChange?: (state: FileTextState) => void;
  preview: (revision?: string) => ReactNode;
}) {
  const paneId = useCanvasPaneId();
  const activePane = useCanvasPane(s => s.activePane);
  const [document, setDocument] = useState<TextDocument>();
  const [draft, setDraft] = useState('');
  const [htmlView, setHtmlView] = useState<'preview' | 'source'>('preview');
  const isHtml = getFilePreviewKind(artifact) === 'html';
  const sourceUrl = artifactUrl(artifact);
  const htmlDraft = useMemo(() => isHtml && document
    ? htmlPreviewDocument(draft, new URL(sourceUrl, window.location.href).href)
    : '', [isHtml, document, draft, sourceUrl]);
  const editing = document?.can_edit === true && artifact.space?.writable !== false;
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const mounted = useRef(false);
  const savingRef = useRef(false);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const tabId = useCanvasPane(s => s.activeTabId);
  const updateArtifact = useCanvasPane(s => s.updateArtifact);
  const setTabDirty = useCanvasPane(s => s.setTabDirty);
  const dirty = editing && document !== undefined && draft !== document.content;
  useEffect(() => {
    onStateChange?.({ canEdit: editing, dirty, saving });
  }, [onStateChange, editing, dirty, saving]);
  const path = artifact.space
    ? '/v1/myspace/text?' + new URLSearchParams({ space: artifact.space.id, path: artifact.space.path })
    : '/v1/file/' + encodeURIComponent(artifact.file_id) + '/text';
  const target = artifactOrigin(artifact);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    apiRequest<unknown>(path, { signal: controller.signal, cache: 'no-store' }, target)
      .then(payload => {
        const doc = unwrapData<TextDocument>(payload);
        if (!doc || typeof doc.content !== 'string' || typeof doc.revision !== 'string'
          || typeof doc.can_edit !== 'boolean') throw new Error(t('文本加载失败'));
        if (controller.signal.aborted) return;
        setDocument(doc);
        setDraft(doc.content);
        setError('');
      }).catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : t('文本加载失败'));
      });
    return () => {
      mounted.current = false;
      controller.abort();
    };
  }, [path, retry, target]);

  useEffect(() => {
    if (tabId) setTabDirty(tabId, dirty || saving);
  }, [tabId, setTabDirty, dirty, saving]);

  useEffect(() => {
    if (!dirty && !saving) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, saving]);

  useEffect(() => { if (editing && (!isHtml || htmlView === 'source')) editorRef.current?.focus(); }, [editing, isHtml, htmlView]);

  const save = async () => {
    if (!document || !dirty || savingRef.current) return;
    if (new TextEncoder().encode(draft).length > 2 * 1024 * 1024) {
      setError(t('在线编辑最多支持 2 MiB 文本'));
      return;
    }
    savingRef.current = true;
    setSaving(true);
    setError('');
    try {
      const saved = unwrapData<TextDocument>(await apiRequest<unknown>(path, {
        method: 'PUT',
        body: JSON.stringify({
          content: document.content.includes('\r\n') ? draft.replace(/\r?\n/g, '\r\n') : draft,
          revision: document.revision,
        }),
      }, target));
      if (!mounted.current) return;
      setDocument(saved);
      setDraft(saved.content);
      updateArtifact({ size: saved.size });
    } catch (cause: unknown) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : t('保存失败'));
    } finally {
      savingRef.current = false;
      if (mounted.current) setSaving(false);
    }
  };

  // Capture while editing, before the panel's generic Ctrl+S browser guard.
  useEffect(() => {
    if (!editing || activePane !== paneId) return;
    const keydown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        event.stopImmediatePropagation();
        void save();
      }
    };
    window.addEventListener('keydown', keydown, true);
    return () => window.removeEventListener('keydown', keydown, true);
  });

  useImperativeHandle(ref, () => ({ save }));

  return <div className="jx-fileTextPanel">
    {isHtml && document && headerControls && createPortal(<div className="jx-fileTextPanel-viewActions" role="toolbar" aria-label={t('HTML视图')}>
      {(['preview', 'source'] as const).map(view => <button key={view}
        type="button" className="jx-canvas-actionBtn" aria-pressed={htmlView === view}
        aria-label={t(view === 'preview' ? '预览' : '源码')}
        title={t(view === 'preview' ? '预览' : '源码')}
        onClick={() => setHtmlView(view)}
        onKeyDown={event => {
          if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.preventDefault();
            setHtmlView(view === 'preview' ? 'source' : 'preview');
            (event.currentTarget.parentElement?.children[view === 'preview' ? 1 : 0] as HTMLElement)?.focus();
          }
        }}>{view === 'preview' ? <EyeOutlined /> : <CodeOutlined />}</button>)}
    </div>, headerControls)}
    {!editing && document && <div className="jx-fileTextPanel-notice">{t('当前文件只读')}</div>}
    {error && <div className="jx-fileTextPanel-error" role="alert">
      {error}
      {!document && <Button onClick={() => setRetry(value => value + 1)}>{t('重试')}</Button>}
    </div>}
    {isHtml && document ? htmlView === 'preview'
      ? <div className="jx-canvas-html"><iframe title={t('HTML预览')} srcDoc={htmlDraft}
          sandbox="allow-scripts" className="jx-canvas-html-frame" /></div>
      : <textarea ref={editorRef} className="jx-fileTextPanel-editor" aria-label={t('文件内容')}
          value={draft} readOnly={!editing} disabled={saving} spellCheck={false}
          onChange={event => setDraft(event.target.value)} />
      : editing && getFilePreviewKind(artifact) === 'markdown'
      ? <MarkdownFileEditor value={draft} disabled={saving} onChange={setDraft} />
      : editing ? <textarea ref={editorRef} className="jx-fileTextPanel-editor"
      aria-label={t('文件内容')} value={draft} disabled={saving} spellCheck={false}
      onChange={event => setDraft(event.target.value)} />
      : preview(document?.revision)}
  </div>;
}
