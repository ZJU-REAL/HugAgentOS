import { usePublicMediaUrl } from '../../hooks/usePublicMediaUrl';
import { useEffect, useMemo, useState } from 'react';
import { Spin } from 'antd';
import { t } from '../../i18n';
import {
  EyeOutlined,
  FileUnknownOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons';
import type { ResourceItem } from '../../types';
import { authFetch } from '../../api';
import { ArtifactFileAction } from './ArtifactFileAction';
import { mdToHtml } from '../../utils/markdown';
import {
  PreviewFileTooLargeError,
  formatPreviewBytes,
  getPreviewLimitBytes,
  readLimitedArrayBuffer,
  readLimitedText,
} from '../../utils/filePreviewSafety';

import { buildRawUrl } from './filePreviewUrls';
import { fileExtension } from '../../utils/filePreviewKind';

// ─── small async-text hook ─────────────────────────────────────────

function useFetchText(item: ResourceItem, maxBytes: number) {
  const url = buildRawUrl(item, true);
  const [state, setState] = useState<{ text: string | null; err: string | null; loading: boolean }>({
    text: null,
    err: null,
    loading: true,
  });
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    if (!url) {
      void Promise.resolve().then(() => {
        if (!cancelled) setState({ text: null, err: t('缺少下载链接'), loading: false });
      });
      return () => {
        cancelled = true;
        controller.abort();
      };
    }
    authFetch(url, { signal: controller.signal })
      .then(async (resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return readLimitedText(resp, maxBytes);
      })
      .then((text) => {
        if (!cancelled) setState({ text, err: null, loading: false });
      })
      .catch((e) => {
        if (!cancelled) {
          const err = e instanceof PreviewFileTooLargeError
            ? t('文件超过 {limit} 的安全预览上限，请下载后在本地打开', {
                limit: formatPreviewBytes(e.limitBytes),
              })
            : String(e?.message || e);
          setState({ text: null, err, loading: false });
        }
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [maxBytes, url]);
  return state;
}

// ─── viewer leaves ─────────────────────────────────────────────────

export function ImageViewer({ src, name }: { src: string; name: string }) {
  const media = usePublicMediaUrl(src);
  if (media.error) return <PreviewError msg={media.error} />;
  if (!media.url) return <PreviewLoading />;
  return (
    <div className="jx-spaceImportPreview-image">
      <img src={media.url} alt={name} />
    </div>
  );
}

export function FrameViewer({ src }: { src: string }) {
  // iframe forbids transform: after onLoad, only ramp opacity to full (CSS .is-loaded)
  const [loaded, setLoaded] = useState(false);
  const media = usePublicMediaUrl(src);
  if (media.error) return <PreviewError msg={media.error} />;
  if (!media.url) return <PreviewLoading />;
  return (
    <iframe
      className={`jx-spaceImportPreview-frame${loaded ? ' is-loaded' : ''}`}
      src={media.url}
      title={t('文件预览')}
      onLoad={() => setLoaded(true)}
    />
  );
}

export function TextViewer({ item, maxBytes }: { item: ResourceItem; maxBytes: number }) {
  const { text, err, loading } = useFetchText(item, maxBytes);
  if (loading) return <PreviewLoading />;
  if (err) return <PreviewError msg={err} />;
  return <pre className="jx-spaceImportPreview-text">{text}</pre>;
}

export function JsonViewer({ item, maxBytes }: { item: ResourceItem; maxBytes: number }) {
  const { text, err, loading } = useFetchText(item, maxBytes);
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

export function MarkdownViewer({ item, maxBytes }: { item: ResourceItem; maxBytes: number }) {
  const { text, err, loading } = useFetchText(item, maxBytes);
  if (loading) return <PreviewLoading />;
  if (err) return <PreviewError msg={err} />;
  return (
    <div
      className="jx-spaceImportPreview-md"
      dangerouslySetInnerHTML={{ __html: mdToHtml(text || '') }}
    />
  );
}

const MAX_TABLE_PREVIEW_ROWS = 200;
const MAX_TABLE_PREVIEW_COLUMNS = 50;

function parseCsv(text: string, sep: string): { rows: string[][]; truncated: boolean } {
  const rows: string[][] = [];
  let cur: string[] = [];
  let field = '';
  let inQuotes = false;
  let truncated = false;
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
      if (cur.length < MAX_TABLE_PREVIEW_COLUMNS) cur.push(field);
      else truncated = true;
      field = '';
      continue;
    }
    if (ch === '\r') continue;
    if (ch === '\n') {
      if (cur.length < MAX_TABLE_PREVIEW_COLUMNS) cur.push(field);
      else truncated = true;
      rows.push(cur);
      if (rows.length >= MAX_TABLE_PREVIEW_ROWS) {
        truncated = i < text.length - 1;
        break;
      }
      cur = [];
      field = '';
      continue;
    }
    field += ch;
  }
  if (rows.length < MAX_TABLE_PREVIEW_ROWS && (field !== '' || cur.length > 0)) {
    if (cur.length < MAX_TABLE_PREVIEW_COLUMNS) cur.push(field);
    else truncated = true;
    rows.push(cur);
  }
  return { rows, truncated };
}

export function CsvViewer({ item, maxBytes }: { item: ResourceItem; maxBytes: number }) {
  const { text, err, loading } = useFetchText(item, maxBytes);
  const sep = fileExtension(item.name) === 'tsv' ? '\t' : ',';
  const parsed = useMemo(() => parseCsv(text || '', sep), [text, sep]);
  const { rows } = parsed;
  if (loading) return <PreviewLoading />;
  if (err) return <PreviewError msg={err} />;
  if (rows.length === 0) return <PreviewError msg={t('空文件')} />;
  return (
    <div className="jx-spaceImportPreview-tableWrap">
      {parsed.truncated && <PreviewTruncatedNotice />}
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

export function XlsxViewer({ item }: { item: ResourceItem }) {
  const url = buildRawUrl(item, true);
  const [state, setState] = useState<{
    loading: boolean;
    err: string | null;
    sheets: { name: string; rows: string[][]; truncated: boolean }[];
    active: string;
  }>({ loading: true, err: null, sheets: [], active: '' });
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        if (!url) throw new Error(t('缺少下载链接'));
        const resp = await authFetch(url, { signal: controller.signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const maxBytes = getPreviewLimitBytes('xlsx') ?? Number.MAX_SAFE_INTEGER;
        const buf = await readLimitedArrayBuffer(resp, maxBytes);
        const XLSX = await import('xlsx');
        const wb = XLSX.read(buf, { type: 'array' });
        const sheets = wb.SheetNames.map((name) => {
          const worksheet = wb.Sheets[name];
          const sparseRows = new Map<number, Map<number, string>>();
          let maxRow = -1;
          let maxColumn = -1;
          let truncated = false;
          let visitedCells = 0;

          if (worksheet) {
            for (const address in worksheet) {
              if (!Object.prototype.hasOwnProperty.call(worksheet, address) || address[0] === '!') continue;
              visitedCells += 1;
              if (visitedCells > 100_000) {
                truncated = true;
                break;
              }
              const position = XLSX.utils.decode_cell(address);
              if (position.r >= MAX_TABLE_PREVIEW_ROWS || position.c >= MAX_TABLE_PREVIEW_COLUMNS) {
                truncated = true;
                continue;
              }
              const cell = worksheet[address];
              if (!cell) continue;
              if (!sparseRows.has(position.r)) sparseRows.set(position.r, new Map());
              sparseRows.get(position.r)?.set(position.c, cell.w ?? String(cell.v ?? ''));
              maxRow = Math.max(maxRow, position.r);
              maxColumn = Math.max(maxColumn, position.c);
            }
          }

          const rows = Array.from({ length: maxRow + 1 }, (_, rowIndex) => (
            Array.from({ length: maxColumn + 1 }, (_, columnIndex) => (
              sparseRows.get(rowIndex)?.get(columnIndex) || ''
            ))
          ));
          return { name, rows, truncated };
        });
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
          const message = e instanceof PreviewFileTooLargeError
            ? t('文件超过 {limit} 的安全预览上限，请下载后在本地打开', {
                limit: formatPreviewBytes(e.limitBytes),
              })
            : String((e as Error)?.message || e);
          setState({
            loading: false,
            err: message,
            sheets: [],
            active: '',
          });
        }
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [url]);
  if (state.loading) return <PreviewLoading />;
  if (state.err) return <PreviewError msg={state.err} />;
  if (state.sheets.length === 0) return <PreviewError msg={t('工作簿为空')} />;
  const activeSheet = state.sheets.find((s) => s.name === state.active);
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
      <div className="jx-spaceImportPreview-tableWrap">
        {activeSheet?.truncated && <PreviewTruncatedNotice />}
        <table className="jx-spaceImportPreview-table">
          <tbody>
            {(activeSheet?.rows || []).map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, columnIndex) => <td key={columnIndex}>{cell}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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

function PreviewTruncatedNotice() {
  return (
    <div className="jx-spaceImportPreview-limitNotice">
      {t('为保持页面流畅，仅展示前 {rows} 行、{columns} 列', {
        rows: MAX_TABLE_PREVIEW_ROWS,
        columns: MAX_TABLE_PREVIEW_COLUMNS,
      })}
    </div>
  );
}

export function UnsupportedView({ item }: { item: ResourceItem }) {
  const dl = buildRawUrl(item);
  return (
    <div className="jx-spaceImportPreview-status">
      <FileUnknownOutlined style={{ fontSize: 36, color: 'var(--color-text-tertiary)' }} />
      <div className="jx-spaceImportPreview-statusText">{t('此格式暂不支持预览')}</div>
      <div className="jx-spaceImportPreview-statusSub">{item.name}</div>
      {dl && (
        <ArtifactFileAction file={item} className="jx-fileAction-button" />
      )}
    </div>
  );
}

export function LargeFileView({ item, limitBytes }: { item: ResourceItem; limitBytes: number }) {
  const dl = buildRawUrl(item);
  return (
    <div className="jx-spaceImportPreview-status">
      <ExclamationCircleOutlined style={{ fontSize: 36, color: 'var(--color-warning)' }} />
      <div className="jx-spaceImportPreview-statusText">{t('文件较大，已停止在线预览')}</div>
      <div className="jx-spaceImportPreview-statusSub">
        {t('当前文件 {size}，超过此格式 {limit} 的安全预览上限。请使用下方文件操作在系统应用中查看。', {
          size: formatPreviewBytes(item.size || 0),
          limit: formatPreviewBytes(limitBytes),
        })}
      </div>
      {dl && (
        <ArtifactFileAction file={item} className="jx-fileAction-button" />
      )}
    </div>
  );
}

export function EmptyState() {
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
