import React from 'react';
import { Tag } from 'antd';
import { CheckCircleOutlined } from '@ant-design/icons';
import { useUIStore } from '../../stores';
import { renderRetrieveDatasetContent, renderRetrieveLocalKB } from './renderers/KBRenderer';
import { renderInternetSearch } from './renderers/SearchRenderer';
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

function findGenericList(raw: unknown): Array<Record<string, unknown>> | null {
  let out = raw;
  if (typeof out === 'string') {
    try { out = JSON.parse(out); } catch { return null; }
  }
  if (!out || typeof out !== 'object' || Array.isArray(out)) return null;
  const scopes: Array<Record<string, unknown>> = [out as Record<string, unknown>];
  const inner = (out as any).result;
  if (inner && typeof inner === 'object' && !Array.isArray(inner)) scopes.push(inner);
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

function renderGenericList(
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
          body: (
            <pre className="jx-tr-jsonBlock" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
              {JSON.stringify(item, null, 2)}
            </pre>
          ),
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

function renderFilePreview(out: unknown): React.ReactNode {
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

function renderLoadSkill(out: unknown): React.ReactNode {
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

export function renderToolOutputBody(toolName: string, out: unknown, setDetailModal: (modal: { title: string; body: React.ReactNode } | null) => void): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;

  // Load skill: render the same detail as the capability center, avoiding stuffing the full SKILL.md into the card
  if (toolName === 'load_skill') return renderLoadSkill(out);

  // Load/read file: compact metadata card + short preview, avoiding stuffing the whole file into the card
  if (toolName === 'view_text_file' || toolName === 'read_artifact' || toolName === 'Read') {
    return renderFilePreview(out);
  }

  if (toolName === 'query_database') {
    const raw = (typeof out === 'object' && out !== null && typeof (out as any).result === 'string')
      ? (out as any).result
      : (typeof out === 'string' ? out : JSON.stringify(out, null, 2));
    const str = raw as string;
    const isSuccess = str.includes('✅') || str.includes('查询成功');
    const isErr = str.includes('❌') || str.startsWith('错误');
    let parsedData: any = null;
    let headerText = '';
    try {
      const nlIdx = str.indexOf('\n\n');
      if (nlIdx >= 0) {
        headerText = str.slice(0, nlIdx).replace(/^[✅❌⚠️]\s*/u, '').trim();
        parsedData = JSON.parse(str.slice(nlIdx + 2).trim());
      }
    } catch { /* noop */ }
    return (
      <div className="jx-tr-db">
        {headerText && <div className={`jx-tr-dbHeader ${isErr ? 'error' : isSuccess ? 'success' : ''}`}>{headerText}</div>}
        {parsedData && Array.isArray(parsedData) && parsedData.length > 0 && typeof parsedData[0] === 'object' ? (
          <div className="jx-tr-tableWrap">
            <table className="jx-tr-table">
              <thead><tr>{Object.keys(parsedData[0]).map((k: string) => <th key={k}>{k}</th>)}</tr></thead>
              <tbody>
                {parsedData.map((row: any, ri: number) => (
                  <tr key={ri}>{Object.values(row).map((v: any, ci: number) => <td key={ci}>{v == null ? '—' : String(v)}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : parsedData != null ? (
          <pre className="jx-tr-jsonBlock">{JSON.stringify(parsedData, null, 2)}</pre>
        ) : (
          <div className={`jx-tr-dbText ${isErr ? 'error' : ''}`}>{str}</div>
        )}
      </div>
    );
  }

  if (toolName === 'list_datasets') {
    const data = (typeof out === 'object' && out !== null ? out : {}) as any;
    const publicDs: any[] = Array.isArray(data?.public_datasets) ? data.public_datasets : [];
    const privateDs: any[] = Array.isArray(data?.private_datasets) ? data.private_datasets : [];
    const allDs = [...publicDs, ...privateDs];
    if (allDs.length === 0) return empty(t('暂无可用知识库'));

    const renderTable = (title: string, items: any[], idKey: string) => {
      if (items.length === 0) return null;
      return (
        <div style={{ marginBottom: 16 }}>
          <div className="jx-tr-dbHeader success" style={{ marginBottom: 8 }}>{title}（{t('{n} 个', { n: items.length })}）</div>
          <div className="jx-tr-tableWrap">
            <table className="jx-tr-table">
              <thead>
                <tr>
                  <th style={{ width: 40 }}>{t('序号')}</th>
                  <th>{t('名称')}</th>
                  <th>{t('简介')}</th>
                  <th style={{ width: 60 }}>{t('文档数')}</th>
                  <th>{t('包含文档')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item: any, idx: number) => {
                  const name = String(item.name || '');
                  const desc = String(item.description || '—');
                  const docCount = item.document_count ?? 0;
                  const docTitles: string[] = Array.isArray(item.document_titles) ? item.document_titles : [];
                  const docDisplay = docTitles.length > 0
                    ? docTitles.slice(0, 5).join('、') + (docTitles.length > 5 ? ` ${t('等 {n} 个', { n: docTitles.length })}` : '')
                    : '—';
                  const openDetail = () => {
                    setDetailModal({
                      title: name || t('知识库详情'),
                      body: (
                        <div className="jx-tr-detailScroll">
                          <div className="jx-tr-detailKV">
                            <div className="jx-tr-detailKVRow"><span className="jx-tr-detailKVKey">ID</span><span className="jx-tr-detailKVValue" style={{ fontFamily: 'monospace', fontSize: 12 }}>{item[idKey] || '—'}</span></div>
                            <div className="jx-tr-detailKVRow"><span className="jx-tr-detailKVKey">{t('名称')}</span><span className="jx-tr-detailKVValue">{name}</span></div>
                            <div className="jx-tr-detailKVRow"><span className="jx-tr-detailKVKey">{t('类型')}</span><span className="jx-tr-detailKVValue">{item.type === 'public' ? t('公有知识库') : t('私有知识库')}</span></div>
                            <div className="jx-tr-detailKVRow"><span className="jx-tr-detailKVKey">{t('简介')}</span><span className="jx-tr-detailKVValue">{desc}</span></div>
                            <div className="jx-tr-detailKVRow"><span className="jx-tr-detailKVKey">{t('文档数量')}</span><span className="jx-tr-detailKVValue">{docCount}</span></div>
                            {docTitles.length > 0 && (
                              <div className="jx-tr-detailKVRow"><span className="jx-tr-detailKVKey">{t('文档列表')}</span>
                                <div className="jx-tr-detailKVValue">
                                  {docTitles.map((docTitle, i) => <div key={i} style={{ padding: '2px 0', borderBottom: '1px solid rgba(0,0,0,.06)' }}>{i + 1}. {docTitle}</div>)}
                                </div>
                              </div>
                            )}
                          </div>
                        </div>
                      ),
                    });
                  };
                  return (
                    <tr key={idx} onClick={openDetail} style={{ cursor: 'pointer' }} title={t('点击查看详情')}>
                      <td>{idx + 1}</td>
                      <td><strong>{name}</strong></td>
                      <td>{desc.length > 60 ? desc.slice(0, 60) + '…' : desc}</td>
                      <td style={{ textAlign: 'center' }}>{docCount}</td>
                      <td style={{ fontSize: 12, color: '#808080' }}>{docDisplay}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      );
    };

    return (
      <div className="jx-tr-db">
        {renderTable(t('公有知识库'), publicDs, 'dataset_id')}
        {renderTable(t('私有知识库'), privateDs, 'kb_id')}
      </div>
    );
  }

  if (toolName === 'retrieve_dataset_content') return renderRetrieveDatasetContent(out, setDetailModal);
  if (toolName === 'retrieve_local_kb') return renderRetrieveLocalKB(out, setDetailModal);
  if (toolName === 'internet_search') return renderInternetSearch(out);
  // ── Chart/export/scrape and similar tools ──────────────────────────────────────────
  if (toolName === 'generate_chart_tool') {
    return (
      <div className="jx-tr-skillBadge">
        <CheckCircleOutlined style={{ color: '#02B589', fontSize: 14 }} />
        <span>{t('图表已生成')}</span>
      </div>
    );
  }

  if (
    toolName === 'word_create_from_markdown' ||
    toolName === 'export_report_to_docx' ||
    toolName === 'export_table_to_excel'
  ) {
    const label =
      toolName === 'export_table_to_excel' ? t('Excel 表格已生成') : t('Word 报告已生成');
    return (
      <div className="jx-tr-skillBadge">
        <CheckCircleOutlined style={{ color: '#02B589', fontSize: 14 }} />
        <span>{label}</span>
      </div>
    );
  }

  if (toolName === 'web_fetch') {
    const wfData = (typeof out === 'object' && out !== null ? out : {}) as any;
    const wfUrl = wfData?.url || '';
    let wfDomain = '';
    try { wfDomain = new URL(wfUrl).hostname; } catch { /* noop */ }
    return (
      <div className="jx-tr-skillBadge">
        <CheckCircleOutlined style={{ color: '#02B589', fontSize: 14 }} />
        <span>{wfDomain ? t('网页内容已获取（{domain}）', { domain: wfDomain }) : t('网页内容已获取')}</span>
      </div>
    );
  }

  if (toolName === 'call_subagent') {
    const saData = (typeof out === 'object' && out !== null ? out : {}) as any;
    const agentName = saData?.agent_name || saData?.name || '';
    return (
      <div className="jx-tr-skillBadge">
        <CheckCircleOutlined style={{ color: '#02B589', fontSize: 14 }} />
        <span>{agentName ? t('子智能体「{name}」已完成', { name: agentName }) : t('子智能体已完成')}</span>
      </div>
    );
  }

  if (toolName === 'bash') {
    const data = (typeof out === 'object' && out !== null ? out : {}) as any;
    const stdout = data?.stdout || '';
    const stderr = data?.stderr || '';
    const exitCode = typeof data?.exit_code === 'number' ? data.exit_code : null;
    const isError = !!data?.error || (exitCode !== null && exitCode !== 0);
    const displayText = data?.error
      || (stdout || stderr ? `${stdout}${stderr ? `\n--- stderr ---\n${stderr}` : ''}` : '')
      || (typeof out === 'string' ? out : '');
    const elapsedMs = data?.execution_time_ms;
    return (
      <div className="jx-tr-db">
        <div className={`jx-tr-dbHeader ${isError ? 'error' : 'success'}`}>
          {isError ? t('命令执行失败') : t('命令执行完成')}
          {exitCode !== null && <span style={{ marginLeft: 8, fontSize: 12, opacity: .6 }}>(exit={exitCode})</span>}
          {typeof elapsedMs === 'number' && <span style={{ marginLeft: 8, fontSize: 12, opacity: .6 }}>({(elapsedMs / 1000).toFixed(2)}s)</span>}
        </div>
        {displayText && (
          <pre className="jx-tr-jsonBlock" style={{ maxHeight: 300, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
            {typeof displayText === 'string' ? displayText : JSON.stringify(displayText, null, 2)}
          </pre>
        )}
      </div>
    );
  }

  if (toolName === 'sandbox_put_artifact' || toolName === 'sandbox_get_artifact') {
    const data = (typeof out === 'object' && out !== null ? out : {}) as any;
    const isError = !!data?.error || data?.ok === false;
    if (isError) {
      return (
        <div className="jx-tr-db">
          <div className="jx-tr-dbHeader error">
            {toolName === 'sandbox_put_artifact' ? t('写入文件失败') : t('保存文件失败')}
          </div>
          <pre className="jx-tr-jsonBlock" style={{ maxHeight: 200, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
            {data?.error || JSON.stringify(data, null, 2)}
          </pre>
        </div>
      );
    }
    if (toolName === 'sandbox_put_artifact') {
      return (
        <div className="jx-tr-skillBadge">
          <CheckCircleOutlined style={{ color: '#02B589', fontSize: 14 }} />
          <span>{t('已写入沙盒：{path}（{size} B）', { path: data?.dest_path, size: data?.size ?? 0 })}</span>
        </div>
      );
    }
    // sandbox_get_artifact: render download link
    const name = data?.name || '';
    const url = data?.url || '';
    return (
      <div className="jx-tr-db">
        <div className="jx-tr-dbHeader success">{t('已生成文件')}</div>
        <div style={{ padding: '8px 12px' }}>
          {url ? (
            <a href={url} target="_blank" rel="noopener noreferrer">{name || t('下载文件')}</a>
          ) : (
            <span>{name}</span>
          )}
          {typeof data?.size === 'number' && <span style={{ marginLeft: 8, opacity: .6 }}>({data.size} B)</span>}
        </div>
      </div>
    );
  }

  // 通用列表兜底：认得出"字典数组 + 标题/锚点"的未知工具 → 标准卡片列表
  const genericItems = findGenericList(out);
  if (genericItems) {
    return (
      <div className="jx-tr-db">
        <div className="jx-tr-dbHeader success">{t('工具执行完成')}</div>
        {renderGenericList(genericItems, setDetailModal)}
      </div>
    );
  }

  // fallback — show actual content instead of hiding it
  const fallbackStr = typeof out === 'string' ? out
    : (typeof out === 'object' && out !== null) ? JSON.stringify(out, null, 2)
    : String(out ?? '');
  if (fallbackStr && fallbackStr.length > 0) {
    // 整体型锚点（后端整份注号）：把 cite_id 提到头部徽章，别让它像乱码字段
    const wholeCiteId = (out && typeof out === 'object' && typeof (out as any).cite_id === 'string')
      ? String((out as any).cite_id) : '';
    return (
      <div className="jx-tr-db">
        <div className="jx-tr-dbHeader success">
          {t('工具执行完成')}
          {wholeCiteId && <span className="jx-tr-citeTag">{wholeCiteId}</span>}
        </div>
        <pre className="jx-tr-jsonBlock" style={{ maxHeight: 300, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
          {fallbackStr}
        </pre>
      </div>
    );
  }
  return (
    <div className="jx-tr-skillBadge">
      <CheckCircleOutlined style={{ color: '#02B589', fontSize: 14 }} />
      <span>{t('工具执行完成')}</span>
    </div>
  );
}

/** Wrapper component that reads setDetailModal from UIStore */
export function ToolOutputBody({ toolName, output }: { toolName: string; output: unknown }) {
  const { setDetailModal } = useUIStore();
  return <>{renderToolOutputBody(toolName, output, setDetailModal)}</>;
}
