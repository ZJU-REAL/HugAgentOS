import React from 'react';
import { CheckCircleOutlined } from '@ant-design/icons';
import { renderRetrieveDatasetContent, renderRetrieveLocalKB } from './renderers/KBRenderer';
import { renderInternetSearch } from './renderers/SearchRenderer';
import { DataView } from './renderers/DataView';
import { mdToHtml } from '../../utils/markdown';
import { t } from '../../i18n';

import { findGenericList, renderGenericList, renderFilePreview, renderLoadSkill } from './ToolOutputHelpers';
export { ToolOutputBody } from './PluginToolOutputBody';
export function renderToolOutputBody(
  toolName: string,
  out: unknown,
  setDetailModal: (modal: { title: string; body: React.ReactNode } | null) => void,
  /** 被引用的证据片段：命中的内容会高亮并把滚动条送到跟前（从引用打开卡片时传入） */
  highlight?: string,
): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;

  // Load skill: render the same detail as the capability center, avoiding stuffing the full SKILL.md into the card
  if (toolName === 'load_skill') return renderLoadSkill(out);

  // Load plugin: the activation summary is human-readable markdown-ish text
  // (新增工具/技能列表) — render it as formatted content, never as a JSON dump
  if (toolName === 'load_plugin') {
    const raw = typeof out === 'string'
      ? out
      : (out && typeof out === 'object' && typeof (out as any).result === 'string')
        ? (out as any).result
        : (out && typeof out === 'object' && typeof (out as any).text === 'string')
          ? (out as any).text
          : String(out ?? '');
    if (!raw.trim()) return empty(t('暂无详情'));
    return (
      <div className="jx-tr-db">
        <div className="jx-tr-dbHeader success">{t('插件已加载')}</div>
        <div className="jx-md jx-tr-skillBody" dangerouslySetInnerHTML={{ __html: mdToHtml(raw) }} />
      </div>
    );
  }

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
          <DataView value={parsedData} focus={highlight} />
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
        <span>{agentName ? t('智能体「{name}」已完成', { name: agentName }) : t('智能体已完成')}</span>
      </div>
    );
  }

  if (toolName === 'Bash' || toolName === 'bash') {
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
          <DataView value={displayText} focus={highlight} maxHeight={340} />
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
          <DataView value={data?.error || data} maxHeight={260} />
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
        <DataView value={out} focus={highlight} />
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

/**
 * Tool result body — resolved in three tiers, in this order:
 *
 *   1. **Plugin declaration** — an installed plugin claimed this tool in its
 *      `plugin.json` (an L2 module first, then an L0 view).
 *   2. **Host built-ins** — tools the product itself owns (`bash`, `Read`,
 *      `query_database`, …), which stay in `renderToolOutputBody`.
 *   3. **Generic fallback** — a recognisable list, else the raw payload.
 *
 * The host therefore contains no branch naming a plugin's tool: which card a
 * plugin's tool gets is the plugin's own declaration, and uninstalling it
 * removes the card with it.
 */
