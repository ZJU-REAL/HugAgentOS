import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Spin } from 'antd';
import { t } from '../../i18n';
import {
  DownloadOutlined,
  EyeOutlined,
  FileUnknownOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons';
import type { ResourceItem } from '../../types';
import { getApiUrl, authFetch } from '../../api';
import { mdToHtml } from '../../utils/markdown';
import { getFileIconSrc } from '../../utils/fileIcon';
import { recomputeSheetRefs } from '../../utils/xlsxRange';

interface FilePreviewPaneProps {
  item: ResourceItem | null;
}

type ViewKind =
  | 'image'
  | 'pdf'
  | 'video'
  | 'audio'
  | 'markdown'
  | 'json'
  | 'csv'
  | 'text'
  | 'pptx'
  | 'docx'
  | 'xlsx'
  | 'unknown';

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico']);
const PDF_EXTS = new Set(['pdf']);
const VIDEO_EXTS = new Set(['mp4', 'webm', 'mov', 'm4v', 'ogv']);
const AUDIO_EXTS = new Set(['mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac']);
const MD_EXTS = new Set(['md', 'markdown', 'mdx']);
const JSON_EXTS = new Set(['json', 'json5', 'jsonl', 'ndjson']);
const CSV_EXTS = new Set(['csv', 'tsv']);
const PPTX_EXTS = new Set(['ppt', 'pptx']);
const DOCX_EXTS = new Set(['doc', 'docx']);
const XLSX_EXTS = new Set(['xls', 'xlsx', 'xlsm']);
const TEXT_EXTS = new Set([
  'txt', 'log', 'py', 'js', 'ts', 'tsx', 'jsx', 'mjs', 'cjs',
  'html', 'htm', 'css', 'scss', 'sass', 'less',
  'xml', 'yaml', 'yml', 'ini', 'conf', 'env', 'toml', 'lock',
  'sh', 'bash', 'zsh', 'fish', 'ps1',
  'go', 'rs', 'java', 'kt', 'swift', 'c', 'cpp', 'cc', 'h', 'hpp',
  'rb', 'php', 'r', 'sql', 'graphql', 'gql', 'proto',
  'vue', 'svelte', 'astro',
]);

function extOf(name: string): string {
  const idx = name.lastIndexOf('.');
  return idx === -1 ? '' : name.slice(idx + 1).toLowerCase();
}

function detectKind(item: ResourceItem): ViewKind {
  const mime = (item.mime_type || '').toLowerCase();
  const ext = extOf(item.name || '');
  if (mime.startsWith('image/')) return 'image';
  if (mime === 'application/pdf') return 'pdf';
  if (mime.startsWith('video/')) return 'video';
  if (mime.startsWith('audio/')) return 'audio';
  if (mime === 'text/markdown') return 'markdown';
  if (mime === 'application/json') return 'json';
  if (mime === 'text/csv') return 'csv';
  if (IMAGE_EXTS.has(ext)) return 'image';
  if (PDF_EXTS.has(ext)) return 'pdf';
  if (VIDEO_EXTS.has(ext)) return 'video';
  if (AUDIO_EXTS.has(ext)) return 'audio';
  if (MD_EXTS.has(ext)) return 'markdown';
  if (JSON_EXTS.has(ext)) return 'json';
  if (CSV_EXTS.has(ext)) return 'csv';
  if (PPTX_EXTS.has(ext)) return 'pptx';
  if (DOCX_EXTS.has(ext)) return 'docx';
  if (XLSX_EXTS.has(ext)) return 'xlsx';
  if (TEXT_EXTS.has(ext)) return 'text';
  if (mime.startsWith('text/')) return 'text';
  return 'unknown';
}

function buildRawUrl(item: ResourceItem, inline = false): string {
  const base = item.download_url || (item.file_id ? `/files/${item.file_id}` : '');
  if (!base) return '';
  const url = `${getApiUrl()}${base}`;
  if (!inline) return url;
  return url.includes('?') ? `${url}&inline=true` : `${url}?inline=true`;
}

function buildOfficePreviewUrl(item: ResourceItem): string {
  if (!item.file_id) return '';
  return `${getApiUrl()}/files/${item.file_id}/preview`;
}

// Hide the browser's built-in PDF viewer chrome (toolbar / sidebar / scrollbar).
// Adobe PDF "Open Parameters" — supported by Chrome, Edge, Firefox.
const PDF_VIEWER_PARAMS = '#toolbar=0&navpanes=0&scrollbar=0&view=FitH';

function withPdfViewerParams(url: string): string {
  if (!url) return url;
  return `${url}${PDF_VIEWER_PARAMS}`;
}

// ─── small async-text hook ─────────────────────────────────────────

function useFetchText(item: ResourceItem) {
  const [state, setState] = useState<{ text: string | null; err: string | null; loading: boolean }>({
    text: null,
    err: null,
    loading: true,
  });
  useEffect(() => {
    let cancelled = false;
    setState({ text: null, err: null, loading: true });
    const url = buildRawUrl(item, true);
    if (!url) {
      setState({ text: null, err: t('缺少下载链接'), loading: false });
      return;
    }
    authFetch(url)
      .then(async (resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return resp.text();
      })
      .then((text) => {
        if (!cancelled) setState({ text, err: null, loading: false });
      })
      .catch((e) => {
        if (!cancelled) setState({ text: null, err: String(e?.message || e), loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, [item.id]);
  return state;
}

// ─── viewer leaves ─────────────────────────────────────────────────

function ImageViewer({ src, name }: { src: string; name: string }) {
  return (
    <div className="jx-spaceImportPreview-image">
      <img src={src} alt={name} />
    </div>
  );
}

function FrameViewer({ src }: { src: string }) {
  // iframe forbids transform: after onLoad, only ramp opacity to full (CSS .is-loaded)
  const [loaded, setLoaded] = useState(false);
  return (
    <iframe
      className={`jx-spaceImportPreview-frame${loaded ? ' is-loaded' : ''}`}
      src={src}
      title={t('文件预览')}
      onLoad={() => setLoaded(true)}
    />
  );
}

function VideoViewer({ src }: { src: string }) {
  return (
    <div className="jx-spaceImportPreview-media">
      <video src={src} controls playsInline />
    </div>
  );
}

function AudioViewer({ src, name }: { src: string; name: string }) {
  const initial = (name || '?').slice(0, 1).toUpperCase();
  return (
    <div className="jx-spaceImportPreview-audio">
      <div className="jx-spaceImportPreview-audioCover">{initial}</div>
      <div className="jx-spaceImportPreview-audioName" title={name}>{name}</div>
      <audio src={src} controls />
    </div>
  );
}

function TextViewer({ item }: { item: ResourceItem }) {
  const { text, err, loading } = useFetchText(item);
  if (loading) return <PreviewLoading />;
  if (err) return <PreviewError msg={err} />;
  return <pre className="jx-spaceImportPreview-text">{text}</pre>;
}

function JsonViewer({ item }: { item: ResourceItem }) {
  const { text, err, loading } = useFetchText(item);
  const pretty = useMemo(() => {
    if (!text) return '';
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return text;
    }
  }, [text]);
  if (loading) return <PreviewLoading />;
  if (err) return <PreviewError msg={err} />;
  return <pre className="jx-spaceImportPreview-text jx-spaceImportPreview-text--json">{pretty}</pre>;
}

function MarkdownViewer({ item }: { item: ResourceItem }) {
  const { text, err, loading } = useFetchText(item);
  if (loading) return <PreviewLoading />;
  if (err) return <PreviewError msg={err} />;
  return (
    <div
      className="jx-spaceImportPreview-md"
      dangerouslySetInnerHTML={{ __html: mdToHtml(text || '') }}
    />
  );
}

function parseCsv(text: string, sep: string): string[][] {
  const rows: string[][] = [];
  let cur: string[] = [];
  let field = '';
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
          continue;
        }
        inQuotes = false;
        continue;
      }
      field += ch;
      continue;
    }
    if (ch === '"') {
      inQuotes = true;
      continue;
    }
    if (ch === sep) {
      cur.push(field);
      field = '';
      continue;
    }
    if (ch === '\r') continue;
    if (ch === '\n') {
      cur.push(field);
      rows.push(cur);
      cur = [];
      field = '';
      continue;
    }
    field += ch;
  }
  if (field !== '' || cur.length > 0) {
    cur.push(field);
    rows.push(cur);
  }
  return rows;
}

function CsvViewer({ item }: { item: ResourceItem }) {
  const { text, err, loading } = useFetchText(item);
  const sep = extOf(item.name) === 'tsv' ? '\t' : ',';
  const rows = useMemo(() => parseCsv(text || '', sep), [text, sep]);
  if (loading) return <PreviewLoading />;
  if (err) return <PreviewError msg={err} />;
  if (rows.length === 0) return <PreviewError msg={t('空文件')} />;
  return (
    <div className="jx-spaceImportPreview-tableWrap">
      <table className="jx-spaceImportPreview-table">
        <thead>
          <tr>
            {rows[0].map((cell, ci) => (
              <th key={ci}>{cell}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(1).map((row, ri) => (
            <tr key={ri}>
              {row.map((cell, ci) => (
                <td key={ci}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function XlsxViewer({ item }: { item: ResourceItem }) {
  const [state, setState] = useState<{
    loading: boolean;
    err: string | null;
    sheets: { name: string; html: string }[];
    active: string;
  }>({ loading: true, err: null, sheets: [], active: '' });
  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, err: null, sheets: [], active: '' });
    (async () => {
      try {
        const url = buildRawUrl(item, true);
        if (!url) throw new Error(t('缺少下载链接'));
        const resp = await authFetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const buf = await resp.arrayBuffer();
        const XLSX = await import('xlsx');
        const wb = XLSX.read(buf, { type: 'array' });
        // A stale <dimension> would make sheet_to_html drop rows outside the
        // declared range; rebuild !ref from the real cells first.
        recomputeSheetRefs(wb, XLSX);
        const sheets = wb.SheetNames.map((n) => ({
          name: n,
          html: XLSX.utils.sheet_to_html(wb.Sheets[n], { id: '' }),
        }));
        if (!cancelled) {
          setState({
            loading: false,
            err: null,
            sheets,
            active: sheets[0]?.name || '',
          });
        }
      } catch (e) {
        if (!cancelled) {
          setState({
            loading: false,
            err: String((e as Error)?.message || e),
            sheets: [],
            active: '',
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [item.id]);
  if (state.loading) return <PreviewLoading />;
  if (state.err) return <PreviewError msg={state.err} />;
  if (state.sheets.length === 0) return <PreviewError msg={t('工作簿为空')} />;
  const activeHtml = state.sheets.find((s) => s.name === state.active)?.html || '';
  return (
    <div className="jx-spaceImportPreview-xlsx">
      {state.sheets.length > 1 && (
        <div className="jx-spaceImportPreview-sheetTabs">
          {state.sheets.map((s) => (
            <button
              key={s.name}
              type="button"
              className={`jx-spaceImportPreview-sheetTab${state.active === s.name ? ' active' : ''}`}
              onClick={() => setState((prev) => ({ ...prev, active: s.name }))}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      <div
        className="jx-spaceImportPreview-tableWrap"
        dangerouslySetInnerHTML={{ __html: activeHtml }}
      />
    </div>
  );
}

// ─── status / empty states ─────────────────────────────────────────

function PreviewLoading() {
  return (
    <div className="jx-spaceImportPreview-status">
      <Spin />
      <div className="jx-spaceImportPreview-statusText">{t('正在加载预览…')}</div>
    </div>
  );
}

function PreviewError({ msg }: { msg: string }) {
  return (
    <div className="jx-spaceImportPreview-status jx-spaceImportPreview-status--error">
      <ExclamationCircleOutlined style={{ fontSize: 28 }} />
      <div className="jx-spaceImportPreview-statusText">{t('预览加载失败')}</div>
      <div className="jx-spaceImportPreview-statusSub">{msg}</div>
    </div>
  );
}

function UnsupportedView({ item }: { item: ResourceItem }) {
  const dl = buildRawUrl(item);
  return (
    <div className="jx-spaceImportPreview-status">
      <FileUnknownOutlined style={{ fontSize: 36, color: 'var(--color-text-tertiary)' }} />
      <div className="jx-spaceImportPreview-statusText">{t('此格式暂不支持预览')}</div>
      <div className="jx-spaceImportPreview-statusSub">{item.name}</div>
      {dl && (
        <Button
          type="primary"
          ghost
          icon={<DownloadOutlined />}
          href={dl}
          target="_blank"
          rel="noreferrer"
          style={{ marginTop: 12 }}
        >
          {t('下载文件')}
        </Button>
      )}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="jx-spaceImportPreview-empty">
      <div className="jx-spaceImportPreview-emptyIcon">
        <EyeOutlined />
      </div>
      <div className="jx-spaceImportPreview-emptyTitle">{t('点击文件右侧的眼睛图标')}</div>
      <div className="jx-spaceImportPreview-emptySub">
        {t('支持图片、PDF、Office 文档、Markdown、代码等多种格式预览')}
      </div>
    </div>
  );
}

// ─── main entry ────────────────────────────────────────────────────

export function FilePreviewPane({ item }: FilePreviewPaneProps) {
  if (!item) return <EmptyState />;
  const kind = detectKind(item);
  const rawUrl = buildRawUrl(item, true);
  const dlUrl = buildRawUrl(item);

  let body: ReactNode;
  switch (kind) {
    case 'image':
      body = <ImageViewer src={rawUrl} name={item.name} />;
      break;
    case 'pdf':
      body = <FrameViewer src={withPdfViewerParams(rawUrl)} />;
      break;
    case 'video':
      body = <VideoViewer src={rawUrl} />;
      break;
    case 'audio':
      body = <AudioViewer src={rawUrl} name={item.name} />;
      break;
    case 'markdown':
      body = <MarkdownViewer item={item} />;
      break;
    case 'json':
      body = <JsonViewer item={item} />;
      break;
    case 'csv':
      body = <CsvViewer item={item} />;
      break;
    case 'text':
      body = <TextViewer item={item} />;
      break;
    case 'pptx':
    case 'docx':
      body = <FrameViewer src={withPdfViewerParams(buildOfficePreviewUrl(item))} />;
      break;
    case 'xlsx':
      body = <XlsxViewer item={item} />;
      break;
    default:
      body = <UnsupportedView item={item} />;
  }

  return (
    <div className="jx-spaceImportPreview">
      <div className="jx-spaceImportPreview-header">
        <img
          className="jx-spaceImportPreview-headerIcon"
          src={getFileIconSrc(item.name)}
          width={20}
          height={20}
          alt=""
        />
        <div className="jx-spaceImportPreview-headerName" title={item.name}>
          {item.name}
        </div>
        {dlUrl && (
          <a
            className="jx-spaceImportPreview-headerAction"
            href={dlUrl}
            target="_blank"
            rel="noreferrer"
            download={item.name}
            title={t('下载原文件')}
          >
            <DownloadOutlined />
          </a>
        )}
      </div>
      <div className="jx-spaceImportPreview-body">
        {/* Preview switch keyed fade-in: CSS primitive, pure opacity (content may contain an iframe, transform forbidden) */}
        <div
          key={item.id}
          className="jx-filePreview-fade jx-anim-fadeIn"
          style={{ animationDuration: '0.18s' }}
        >
          {body}
        </div>
      </div>
    </div>
  );
}
