import React from 'react';
import { GlobalOutlined, SearchOutlined } from '@ant-design/icons';
import { coerceOutput, preview } from './utils';
import { staggerStyle } from '../../../utils/motionTokens';
import { t } from '../../../i18n';


interface SearchItem {
  title: string;
  url: string;
  domain: string;
  snippet: string;
  citeId: string;
}

function parseSearchResult(out: unknown): { query: string; results: SearchItem[] } {
  const raw = coerceOutput(out) as Record<string, unknown> | undefined;
  const sr = (raw && typeof raw === 'object' && 'result' in raw ? (raw as Record<string, unknown>).result : raw) as
    | Record<string, unknown> | undefined;
  const rawResults = sr && Array.isArray(sr.results) ? sr.results : [];
  const query = String(sr?.query ?? (raw as { query?: unknown })?.query ?? '').trim();
  const results: SearchItem[] = rawResults.map((r: any) => {
    const url = String(r?.url || '');
    let domain = '';
    try { domain = new URL(url).hostname.replace(/^www\./, ''); } catch { /* noop */ }
    return {
      title: String(r?.title || t('（无标题）')),
      url,
      domain,
      snippet: String(r?.content || r?.snippet || ''),
      citeId: String(r?.cite_id || ''),
    };
  });
  return { query, results };
}

/** Inline horizontal card strip (used inside ToolCallRow). */
export function renderInternetSearchInline(out: unknown): React.ReactNode {
  const { query, results } = parseSearchResult(out);
  if (results.length === 0) return <div className="jx-tr-empty">{t('暂无搜索结果')}</div>;
  return (
    <div className="jx-tr-searchInline">
      {query && (
        <div className="jx-tr-searchQueryBar">
          <SearchOutlined className="jx-tr-searchQueryIcon" />
          <span className="jx-tr-searchQueryText">{query}</span>
          <span className="jx-tr-searchQueryCount">{results.length}</span>
        </div>
      )}
      <div className="jx-tr-searchCardsWrap">
        {results.map((r, idx) => {
          // Stagger entrance: 30ms per card, capped at 6 steps (see tool.css).
          const cardStyle: React.CSSProperties = {
            ...(!r.url ? { cursor: 'default', pointerEvents: 'none' as const } : {}),
            ...staggerStyle(idx, 6),
          };
          return (
            <a
              key={idx}
              className="jx-tr-searchCard"
              href={r.url || undefined}
              target="_blank"
              rel="noopener noreferrer"
              title={r.title}
              style={cardStyle}
            >
              <div className="jx-tr-searchCardTitle">{r.title}</div>
              <div className="jx-tr-searchCardFooter">
                {r.citeId && <span className="jx-tr-citeTag">{r.citeId}</span>}
                {/* Inline icon instead of an external favicon service: offline/intranet
                    deployments can't reach one, and it would leak visited domains. */}
                {r.domain && <GlobalOutlined className="jx-tr-searchCardFavicon" style={{ fontSize: 12 }} />}
                {r.domain && <span className="jx-tr-searchCardDomain">{r.domain}</span>}
              </div>
            </a>
          );
        })}
      </div>
    </div>
  );
}

/** Vertical list (used in the right-side detail panel). */
export function renderInternetSearch(out: unknown): React.ReactNode {
  const { results } = parseSearchResult(out);
  if (results.length === 0) return <div className="jx-tr-empty">{t('暂无搜索结果')}</div>;
  return (
    <div className="jx-tr-searchList">
      {results.map((r, idx) => (
        <a key={idx} className="jx-tr-searchItem jx-tr-searchItem--link" href={r.url || undefined} target="_blank" rel="noopener noreferrer"
          style={!r.url ? { cursor: 'default', pointerEvents: 'none' } : undefined}>
          <div className="jx-tr-searchHeader">
            <span className="jx-tr-kbIdx">{idx + 1}</span>
            <span className="jx-tr-searchTitle">{r.title}</span>
            {r.citeId && <span className="jx-tr-citeTag">{r.citeId}</span>}
          </div>
          {r.snippet && <div className="jx-tr-searchSnippet">{preview(r.snippet)}</div>}
          {r.domain && <div className="jx-tr-searchFooter"><span className="jx-tr-searchDomain">{r.domain}</span></div>}
        </a>
      ))}
    </div>
  );
}
