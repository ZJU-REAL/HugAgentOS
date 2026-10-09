import React from 'react';
import { Tag } from 'antd';
import { DataView } from './renderers/DataView';
import { mdToHtml } from '../../utils/markdown';
import { t } from '../../i18n';
type SkillLoadPayload = {
  kind?: string;
  skill_id?: string;
  name?: string;
  description?: string;
  version?: string;
  tags?: string[];
  detail?: string;
};

type FilePreviewPayload = {
  kind?: string;
  name?: string;
  path?: string | null;
  mime_type?: string | null;
  total_chars?: number | null;
  returned_chars?: number | null;
  has_more?: boolean;
  line_count?: number | null;
  preview?: string;
  extra?: Record<string, unknown>;
};

// ── 通用列表渲染器 ──────────────────────────────────────────────
// 与后端 orchestration/citation_anchor.py 共用同一套字段别名约定：
// 没有专属渲染器的工具，只要结果里能认出"字典数组"列表（带 cite_id 或
// 标题类字段），就渲染成标准卡片列表，而不是裸 JSON。
const GENERIC_LIST_KEYS = ['items', 'results', 'events', 'pages', 'records', 'entries', 'data', 'list', 'rows', 'docs'];
const GENERIC_TITLE_KEYS = ['title', '标题', '文件名称', '企业名称', '产品名称', 'name', '名称', 'document_name'];
const GENERIC_SNIPPET_KEYS = ['content', '文件内容', 'snippet', '摘要', 'summary', 'description', 'abstract', 'text'];

function firstStr(item: Record<string, unknown>, keys: string[]): string {
  for (const k of keys) {
    const v = item[k];
    if ((typeof v === 'string' || typeof v === 'number') && String(v).trim()) return String(v);
  }
  return '';
}

export function findGenericList(raw: unknown): Array<Record<string, unknown>> | null {
  let out = raw;
  if (typeof out === 'string') {
    try { out = JSON.parse(out); } catch { return null; }
  }
  if (!out || typeof out !== 'object' || Array.isArray(out)) return null;
  const scopes: Array<Record<string, unknown>> = [out as Record<string, unknown>];
  const inner = (out as Record<string, unknown>).result;
  if (inner && typeof inner === 'object' && !Array.isArray(inner)) scopes.push(inner as Record<string, unknown>);
  for (const scope of scopes) {
    for (const key of GENERIC_LIST_KEYS) {
      const val = scope[key];
      if (Array.isArray(val) && val.length > 0 && val.every(x => x && typeof x === 'object' && !Array.isArray(x))) {
        const items = val as Array<Record<string, unknown>>;
        // 至少要认得出锚点或标题，否则宁可显示原始 JSON
        if (items.some(it => it.cite_id || firstStr(it, GENERIC_TITLE_KEYS))) return items;
      }
    }
  }
  return null;
}

export function renderGenericList(
  items: Array<Record<string, unknown>>,
  setDetailModal: (modal: { title: string; body: React.ReactNode } | null) => void,
): React.ReactNode {
  return (
    <div className="jx-tr-kbList">
      {items.map((item, idx) => {
        const title = firstStr(item, GENERIC_TITLE_KEYS) || t('第 {n} 条', { n: idx + 1 });
        const snippet = firstStr(item, GENERIC_SNIPPET_KEYS);
        const citeId = typeof item.cite_id === 'string' ? item.cite_id : '';
        const openDetail = () => setDetailModal({
          title,
          body: <DataView value={item} maxHeight={520} />,
        });
        return (
          <div key={idx} className="jx-tr-kbItem jx-tr-kbItem--clickable" onClick={openDetail} title={t('点击查看详情')}>
            <div className="jx-tr-kbDocName">
              <span className="jx-tr-kbIdx">{idx + 1}</span>
              {title}
              {citeId && <span className="jx-tr-citeTag">{citeId}</span>}
            </div>
            {snippet && (
              <div className="jx-tr-kbPreview">
                <div className="jx-tr-kbContent">{snippet.length > 90 ? snippet.slice(0, 90) + '…' : snippet}</div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function formatBytes(n?: number | null): string {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return '';
  return t('{n} 字', { n: n.toLocaleString('zh-CN') });
}

export function renderFilePreview(out: unknown): React.ReactNode {
  const data = (out && typeof out === 'object') ? (out as FilePreviewPayload) : {};
  const name = (data.name || t('文件')).toString();
  const path = (data.path || '').toString();
  const mime = (data.mime_type || '').toString();
  const total = data.total_chars ?? null;
  const returned = data.returned_chars ?? null;
  const hasMore = !!data.has_more;
  const lineCount = data.line_count ?? null;
  const preview = (data.preview || '').toString();
  const extra = (data.extra && typeof data.extra === 'object') ? data.extra : {};

  // Collect metadata into a chip list
  const chips: string[] = [];
  if (mime) chips.push(mime);
  if (typeof total === 'number' && total > 0) {
    if (typeof returned === 'number' && returned !== total) {
      chips.push(t('已读 {r}/{total} 字', { r: returned.toLocaleString('zh-CN'), total: total.toLocaleString('zh-CN') }));
    } else {
      chips.push(formatBytes(total));
    }
  } else if (typeof returned === 'number' && returned > 0) {
    chips.push(t('已读 {n} 字', { n: returned.toLocaleString('zh-CN') }));
  }
  if (typeof lineCount === 'number' && lineCount > 0) chips.push(t('{n} 行', { n: lineCount }));
  if (hasMore) chips.push(t('未读完'));
  // Extra info from read_artifact
  const sheetNames = Array.isArray(extra.sheet_names) ? (extra.sheet_names as unknown[]).filter((s) => typeof s === 'string') : [];
  if (sheetNames.length > 0) chips.push(`Sheet：${(sheetNames as string[]).join('、')}`);
  if (typeof extra.slide_count === 'number') chips.push(t('共 {n} 页', { n: extra.slide_count }));
  if (typeof extra.slide_index === 'number') chips.push(t('第 {n} 页', { n: extra.slide_index }));
  // Extra info from the Read tool
  if (typeof extra.start_line === 'number' && typeof extra.end_line === 'number') {
    chips.push(t('第 {s}-{e} 行', { s: extra.start_line, e: extra.end_line }));
  }
  if (extra.parsed_text) chips.push(t('已解析为文本'));
  if (extra.recovered_from_artifact) chips.push(t('从历史恢复'));
  if (extra.type === 'binary') chips.push(t('二进制文件'));
  if (extra.type === 'too_large') chips.push(t('文件过大'));
  if (typeof extra.size_bytes === 'number') chips.push(t('{n} 字节', { n: extra.size_bytes.toLocaleString('zh-CN') }));
  if (typeof extra.error === 'string' && extra.error) chips.push(t('错误：{msg}', { msg: extra.error }));

  return (
    <div className="jx-tr-db">
      <div className="jx-tr-dbHeader success">{t('文件已读取')}</div>
      <div className="jx-tr-skillCard">
        <div className="jx-tr-skillHead">
          <span className="jx-tr-skillName">{name}</span>
        </div>
        {path && (
          <div className="jx-tr-filePath" title={path}>{path}</div>
        )}
        {chips.length > 0 && (
          <div className="jx-tr-skillTags">
            {chips.map((c, i) => (
              <Tag key={`${c}-${i}`} className="jx-tr-skillTag">{c}</Tag>
            ))}
          </div>
        )}
        {preview ? (
          <pre className="jx-tr-filePreview"><code>{preview}</code></pre>
        ) : (
          <div className="jx-tr-skillEmpty">{t('（无预览内容）')}</div>
        )}
      </div>
    </div>
  );
}

export function renderLoadSkill(out: unknown): React.ReactNode {
  const data = (out && typeof out === 'object') ? (out as SkillLoadPayload) : {};
  const name = (data.name || data.skill_id || t('技能')).toString();
  const desc = (data.description || '').toString();
  const version = (data.version || '').toString();
  const tags = Array.isArray(data.tags) ? data.tags.filter((t) => typeof t === 'string' && t) : [];
  const detail = (data.detail || '').toString();

  return (
    <div className="jx-tr-db">
      <div className="jx-tr-dbHeader success">{t('技能已加载')}</div>
      <div className="jx-tr-skillCard">
        <div className="jx-tr-skillHead">
          <span className="jx-tr-skillName">{name}</span>
          {version && <span className="jx-tr-skillVersion">v{version}</span>}
        </div>
        {desc && <p className="jx-tr-skillDesc">{desc}</p>}
        {tags.length > 0 && (
          <div className="jx-tr-skillTags">
            {tags.map((tag, i) => (
              <Tag key={`${tag}-${i}`} className="jx-tr-skillTag">{tag}</Tag>
            ))}
          </div>
        )}
        {detail ? (
          <div className="jx-md jx-tr-skillBody" dangerouslySetInnerHTML={{ __html: mdToHtml(detail) }} />
        ) : (
          <div className="jx-tr-skillEmpty">{t('暂无详情')}</div>
        )}
      </div>
    </div>
  );
}
