import React from 'react';
import { GlobalOutlined, SearchOutlined } from '@ant-design/icons';
import { coerceOutput, preview } from './utils';
import { staggerStyle } from '../../../utils/motionTokens';
import { t } from '../../../i18n';

type SetDetailModal = (modal: { title: string; body: React.ReactNode } | null) => void;

interface SearchItem {
  title: string;
  url: string;
  domain: string;
  snippet: string;
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
          </div>
          {r.snippet && <div className="jx-tr-searchSnippet">{preview(r.snippet)}</div>}
          {r.domain && <div className="jx-tr-searchFooter"><span className="jx-tr-searchDomain">{r.domain}</span></div>}
        </a>
      ))}
    </div>
  );
}

export function renderIndustryNews(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  const data = (typeof out === 'object' && out !== null ? out : {}) as any;
  const items: any[] = Array.isArray(data?.items) ? data.items : [];
  if (items.length === 0) return empty(t('暂无产业资讯'));
  return (
    <div className="jx-tr-newsList">
      {items.map((item: any, idx: number) => {
        const title = String(item['标题'] || item.title || '');
        const summary = String(item['摘要'] || item.summary || '');
        const tags = [item['标签'], item['对应产业链'], item['地区']].filter(Boolean).map(String);
        const openDetail = () => setDetailModal({
          title: title || t('资讯详情'),
          body: (
            <div>
              {tags.length > 0 && <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 10 }}>{tags.map((tag, ti) => <span key={ti} className="jx-tr-newsTag">{tag}</span>)}</div>}
              <div className="jx-tr-detailBody">{summary || t('暂无摘要')}</div>
            </div>
          ),
        });
        return (
          <div key={idx} className="jx-tr-newsItem jx-tr-newsItem--clickable" onClick={openDetail} title={t('点击查看详情')}>
            {title && <div className="jx-tr-newsTitle">{title}</div>}
            {summary && <div className="jx-tr-newsSummary">{preview(summary)}</div>}
            {tags.length > 0 && <div className="jx-tr-newsTags">{tags.map((tag, ti) => <span key={ti} className="jx-tr-newsTag">{tag}</span>)}</div>}
          </div>
        );
      })}
    </div>
  );
}

export function renderLatestAiNews(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  const data = (typeof out === 'object' && out !== null ? out : {}) as any;
  const items: any[] = Array.isArray(data?.items) ? data.items : [];
  if (items.length === 0) return empty(t('暂无 AI 热点'));
  return (
    <div className="jx-tr-aiNewsList">
      {items.map((item: any, idx: number) => {
        const date = String(item['时间'] || item.date || '');
        const title = String(item['标题'] || item.title || '');
        const summary = String(item['摘要'] || item.summary || '');
        const shortDate = date.length >= 10 ? date.slice(5, 10) : date.slice(0, 5);
        const openDetail = () => setDetailModal({
          title: title || t('AI 热点'),
          body: (
            <div>
              {date && <div style={{ fontSize: 11, color: 'rgba(18,109,255,.65)', fontWeight: 700, marginBottom: 8 }}>{date}</div>}
              <div className="jx-tr-detailBody">{summary || t('暂无摘要')}</div>
            </div>
          ),
        });
        return (
          <div key={idx} className="jx-tr-aiNewsItem jx-tr-aiNewsItem--clickable" onClick={openDetail} title={t('点击查看详情')}>
            {shortDate && <div className="jx-tr-aiNewsDate">{shortDate}</div>}
            <div className="jx-tr-aiNewsContent">
              {title && <div className="jx-tr-aiNewsTitle">{title}</div>}
              {summary && <div className="jx-tr-aiNewsSummary">{preview(summary)}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
