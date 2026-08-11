import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AppstoreOutlined,
  FileTextOutlined,
  NodeIndexOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { Button, Empty, Input, Spin, Tooltip, message } from 'antd';
import {
  getWikiIndexOverview,
  getWikiPage,
  getWikiPages,
  getWikiSourceChunks,
  getWikiStats,
  searchWikiPages,
} from '../../api';
import type {
  WikiIndexOverview,
  WikiPageBrief,
  WikiPageDetail,
  WikiSourceChunk,
  WikiStats,
} from '../../types';
import { WIKI_KNOWLEDGE_TYPES } from '../../types';
import { mdToHtml } from '../../utils/markdown';
import { WikiGraphView } from './WikiGraphView';
import { WikiTree } from './WikiTree';

/**
 * 外接知识库的「知识 Wiki」视图。
 *
 * Wiki 是这个知识库的**地图**：模型离线把每篇文档抽成概念页/实体页并互相链接，
 * 用户在这里浏览的是这张地图，而事实与出处始终回到原文——所以每页底部都有
 * 「原文出处」，顺 chunk_refs 按 ID 直取，不做二次检索。
 *
 * 侧栏结构对齐 WeKnora 自己的组织方式：
 *   索引总览 —— 虚拟视图，按类型分节列出代表条目
 *   知识 Tab —— 合并 entity/concept/synthesis/comparison，走**多层目录树**
 *   摘要 Tab —— 一篇文档一页，服务端对该类型不提供目录，故走平铺列表
 */

const KNOWLEDGE_TYPES = WIKI_KNOWLEDGE_TYPES.join(',');
const FLAT_PAGE_SIZE = 80;

type WikiView = 'browse' | 'graph';
type SidebarTab = 'knowledge' | 'summary';

const TYPE_LABELS: Record<string, string> = {
  concept: '概念',
  entity: '实体',
  synthesis: '综述',
  comparison: '对比',
  summary: '文档摘要',
  index: '索引',
};

interface WikiPanelProps {
  kbId: string;
  kbName: string;
}

/** 把正文里的 [[slug|显示名]] 双链换成可点击锚点，其余交给通用 markdown 渲染 */
function renderWikiMarkdown(content: string): string {
  const withLinks = (content || '').replace(
    /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g,
    (_match, slug: string, label?: string) => {
      const safeSlug = slug.trim().replace(/"/g, '&quot;');
      const text = (label || slug).trim();
      return `<a class="jx-wikiLink" data-wiki-slug="${safeSlug}" href="#">${text}</a>`;
    },
  );
  return mdToHtml(withLinks);
}

export function WikiPanel({ kbId, kbName }: WikiPanelProps) {
  const [view, setView] = useState<WikiView>('browse');
  const [stats, setStats] = useState<WikiStats | null>(null);

  const [tab, setTab] = useState<SidebarTab>('knowledge');
  const [query, setQuery] = useState('');
  const [searchResults, setSearchResults] = useState<WikiPageBrief[] | null>(null);
  const [searching, setSearching] = useState(false);

  // 摘要 Tab 的平铺列表（服务端对该类型不提供目录）
  const [flatPages, setFlatPages] = useState<WikiPageBrief[]>([]);
  const [flatTotal, setFlatTotal] = useState(0);
  const [flatLoading, setFlatLoading] = useState(false);

  // 阅读区：索引总览 或 某个页面
  const [showIndex, setShowIndex] = useState(true);
  const [indexData, setIndexData] = useState<WikiIndexOverview | null>(null);
  const [indexLoading, setIndexLoading] = useState(false);

  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [page, setPage] = useState<WikiPageDetail | null>(null);
  const [pageLoading, setPageLoading] = useState(false);

  const [sources, setSources] = useState<WikiSourceChunk[] | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);

  const readerRef = useRef<HTMLDivElement>(null);

  // ── 数据加载 ───────────────────────────────────────────────────────────────

  const loadStats = useCallback(async () => {
    try {
      setStats(await getWikiStats(kbId));
    } catch {
      setStats(null);
    }
  }, [kbId]);

  const loadIndex = useCallback(async () => {
    setIndexLoading(true);
    try {
      setIndexData(await getWikiIndexOverview(kbId, 12));
    } catch {
      setIndexData(null);
    } finally {
      setIndexLoading(false);
    }
  }, [kbId]);

  const loadFlatPages = useCallback(async () => {
    setFlatLoading(true);
    try {
      const result = await getWikiPages(kbId, {
        pageType: 'summary',
        pageSize: FLAT_PAGE_SIZE,
      });
      setFlatPages(result.pages);
      setFlatTotal(result.total);
    } catch (e) {
      message.error(`加载摘要列表失败：${(e as Error).message}`);
      setFlatPages([]);
      setFlatTotal(0);
    } finally {
      setFlatLoading(false);
    }
  }, [kbId]);

  const openPage = useCallback(
    async (slug: string) => {
      setShowIndex(false);
      setActiveSlug(slug);
      setPageLoading(true);
      setSources(null);
      try {
        const detail = await getWikiPage(kbId, slug);
        setPage(detail);
        readerRef.current?.scrollTo({ top: 0 });
      } catch (e) {
        message.error(`打开页面失败：${(e as Error).message}`);
        setPage(null);
      } finally {
        setPageLoading(false);
      }
    },
    [kbId],
  );

  const runSearch = useCallback(async () => {
    const keyword = query.trim();
    if (!keyword) {
      setSearchResults(null);
      return;
    }
    setSearching(true);
    try {
      const result = await searchWikiPages(kbId, keyword, 40);
      setSearchResults(result.pages);
    } catch (e) {
      message.error(`检索失败：${(e as Error).message}`);
    } finally {
      setSearching(false);
    }
  }, [kbId, query]);

  const loadSources = useCallback(async () => {
    if (!activeSlug) return;
    setSourceLoading(true);
    try {
      const result = await getWikiSourceChunks(kbId, activeSlug, 6);
      setSources(result.chunks);
      if (!result.chunks.length) message.info('该页面暂未关联到可回溯的原文片段');
    } catch (e) {
      message.error(`回溯原文失败：${(e as Error).message}`);
    } finally {
      setSourceLoading(false);
    }
  }, [kbId, activeSlug]);

  // ── 生命周期 ───────────────────────────────────────────────────────────────

  useEffect(() => {
    setActiveSlug(null);
    setPage(null);
    setSources(null);
    setQuery('');
    setSearchResults(null);
    setShowIndex(true);
    setTab('knowledge');
    setFlatPages([]);
    void loadStats();
    void loadIndex();
  }, [kbId, loadStats, loadIndex]);

  useEffect(() => {
    if (tab === 'summary' && !flatPages.length && !flatLoading) void loadFlatPages();
  }, [tab, flatPages.length, flatLoading, loadFlatPages]);

  // 正文里的 [[双链]] 用事件委托接管，避免给每个链接单独绑监听
  useEffect(() => {
    const node = readerRef.current;
    if (!node) return;
    const handler = (event: MouseEvent) => {
      const target = (event.target as HTMLElement)?.closest('[data-wiki-slug]');
      if (!target) return;
      event.preventDefault();
      const slug = target.getAttribute('data-wiki-slug');
      if (slug) void openPage(slug);
    };
    node.addEventListener('click', handler);
    return () => node.removeEventListener('click', handler);
  }, [openPage]);

  // ── 派生 ───────────────────────────────────────────────────────────────────

  const pageHtml = useMemo(() => (page ? renderWikiMarkdown(page.content) : ''), [page]);
  const introHtml = useMemo(() => (indexData?.intro ? mdToHtml(indexData.intro) : ''), [indexData]);

  const statChips = useMemo(() => {
    if (!stats) return [];
    const byType = stats.pages_by_type || {};
    const chips: Array<{ label: string; value: number }> = [
      { label: '概念页', value: byType.concept || 0 },
      { label: '实体页', value: byType.entity || 0 },
    ];
    if (byType.synthesis) chips.push({ label: '综述', value: byType.synthesis });
    if (byType.comparison) chips.push({ label: '对比', value: byType.comparison });
    chips.push({ label: '文档摘要', value: byType.summary || 0 });
    chips.push({ label: '关联链接', value: stats.total_links || 0 });
    return chips;
  }, [stats]);

  const tabDefs = useMemo(() => {
    const byType = stats?.pages_by_type || {};
    const knowledge = WIKI_KNOWLEDGE_TYPES.reduce((sum, t) => sum + (byType[t] || 0), 0);
    return [
      { key: 'knowledge' as SidebarTab, label: '知识', count: knowledge },
      { key: 'summary' as SidebarTab, label: '摘要', count: byType.summary || 0 },
    ];
  }, [stats]);

  // ── 渲染 ───────────────────────────────────────────────────────────────────

  const renderSidebar = () => (
    <aside className="jx-wikiSidebar">
      <div className="jx-wikiSearchRow">
        <Input
          allowClear
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            if (!e.target.value.trim()) setSearchResults(null);
          }}
          onPressEnter={() => void runSearch()}
          prefix={<SearchOutlined />}
          placeholder="搜索概念、机构、制度…"
        />
        <Button type="primary" loading={searching} onClick={() => void runSearch()}>
          检索
        </Button>
      </div>

      <button
        type="button"
        className={`jx-wikiNavItem${showIndex ? ' is-active' : ''}`}
        onClick={() => {
          setShowIndex(true);
          setActiveSlug(null);
          if (!indexData && !indexLoading) void loadIndex();
        }}
      >
        <AppstoreOutlined />
        <span>索引总览</span>
        {stats ? (
          <span className="jx-wikiNavCount">{stats.total_pages.toLocaleString()}</span>
        ) : null}
      </button>

      {searchResults !== null ? (
        <>
          <div className="jx-wikiListMeta">
            <span>检索到 {searchResults.length} 页</span>
            <Button
              type="link"
              size="small"
              onClick={() => {
                setQuery('');
                setSearchResults(null);
              }}
            >
              返回目录
            </Button>
          </div>
          <div className="jx-wikiList">
            {searchResults.length === 0 ? (
              <div className="jx-wikiListEmpty">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的知识页" />
                <p className="jx-wikiListEmptyHint">
                  Wiki 检索按字面匹配，口语化说法可能对不上。换更书面的术语试试，
                  或一次给多个说法（如 <code>资质|牌照|证书</code>）。
                </p>
              </div>
            ) : (
              searchResults.map((item, idx) => (
                <button
                  key={item.slug}
                  type="button"
                  className={`jx-wikiListItem${activeSlug === item.slug ? ' is-active' : ''}`}
                  style={{ '--stagger-index': idx } as React.CSSProperties}
                  onClick={() => void openPage(item.slug)}
                >
                  <div className="jx-wikiListItemTop">
                    <span className={`jx-wikiTypeTag jx-wikiTypeTag--${item.page_type}`}>
                      {item.type_label || TYPE_LABELS[item.page_type] || '其他'}
                    </span>
                    <span className="jx-wikiListItemTitle">{item.title}</span>
                  </div>
                  {item.summary ? <p className="jx-wikiListItemDesc">{item.summary}</p> : null}
                </button>
              ))
            )}
          </div>
        </>
      ) : (
        <>
          <div className="jx-wikiTypeFilter" role="tablist" aria-label="内容分类">
            {tabDefs.map((item) => (
              <button
                key={item.key}
                type="button"
                role="tab"
                aria-selected={tab === item.key}
                className={`jx-wikiTypeChip${tab === item.key ? ' is-active' : ''}`}
                onClick={() => setTab(item.key)}
              >
                {item.label}
                {item.count ? <b>{item.count.toLocaleString()}</b> : null}
              </button>
            ))}
            <Tooltip title="刷新">
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => {
                  void loadStats();
                  if (tab === 'summary') {
                    setFlatPages([]);
                    void loadFlatPages();
                  }
                }}
              />
            </Tooltip>
          </div>

          <div className="jx-wikiList">
            {tab === 'knowledge' ? (
              <WikiTree
                key={`${kbId}:${KNOWLEDGE_TYPES}`}
                kbId={kbId}
                pageTypes={KNOWLEDGE_TYPES}
                activeSlug={activeSlug}
                onSelectPage={(slug) => void openPage(slug)}
              />
            ) : flatLoading ? (
              <div className="jx-wikiTreeLoading">
                <Spin size="small" />
              </div>
            ) : flatPages.length === 0 ? (
              <div className="jx-wikiListEmpty">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无文档摘要页" />
              </div>
            ) : (
              <>
                {flatPages.map((item, idx) => (
                  <button
                    key={item.slug}
                    type="button"
                    className={`jx-wikiListItem${activeSlug === item.slug ? ' is-active' : ''}`}
                    style={{ '--stagger-index': idx } as React.CSSProperties}
                    onClick={() => void openPage(item.slug)}
                  >
                    <div className="jx-wikiListItemTop">
                      <span className="jx-wikiTypeTag jx-wikiTypeTag--summary">文档摘要</span>
                      <span className="jx-wikiListItemTitle">{item.title}</span>
                    </div>
                    {item.summary ? <p className="jx-wikiListItemDesc">{item.summary}</p> : null}
                  </button>
                ))}
                {flatTotal > flatPages.length && (
                  <div className="jx-wikiTreeEmpty">
                    仅显示前 {flatPages.length} / {flatTotal} 页，用搜索精确定位
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}
    </aside>
  );

  const renderIndexOverview = () => (
    <article className="jx-wikiArticle">
      <div className="jx-wikiArticleHead">
        <h1 className="jx-wikiArticleTitle">
          <span className="jx-wikiTypeTag jx-wikiTypeTag--index">索引</span>
          {kbName} · 知识索引
        </h1>
      </div>
      {indexLoading ? (
        <div className="jx-wikiReaderLoading">
          <Spin />
        </div>
      ) : !indexData ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无索引" />
      ) : (
        <>
          {introHtml ? (
            <div
              className="jx-wikiArticleBody jx-md jx-wikiIndexIntro"
              dangerouslySetInnerHTML={{ __html: introHtml }}
            />
          ) : null}
          {indexData.groups
            .filter((group) => group.total > 0)
            .map((group) => (
              <section key={group.type} className="jx-wikiIndexGroup">
                <h3 className="jx-wikiIndexGroupTitle">
                  <span className={`jx-wikiTypeTag jx-wikiTypeTag--${group.type}`}>
                    {group.type_label || TYPE_LABELS[group.type] || group.type}
                  </span>
                  <span className="jx-wikiIndexGroupCount">
                    共 {group.total.toLocaleString()} 页
                  </span>
                </h3>
                <div className="jx-wikiIndexItems">
                  {group.items.map((item) => (
                    <button
                      key={item.slug}
                      type="button"
                      className="jx-wikiIndexItem"
                      onClick={() => void openPage(item.slug)}
                    >
                      <span className="jx-wikiIndexItemTitle">{item.title}</span>
                      {item.summary ? (
                        <span className="jx-wikiIndexItemDesc">{item.summary}</span>
                      ) : null}
                    </button>
                  ))}
                </div>
              </section>
            ))}
        </>
      )}
    </article>
  );

  const renderArticle = () => {
    if (!page) return null;
    return (
      <article className="jx-wikiArticle">
        <div className="jx-wikiArticleHead">
          {page.wiki_path ? (
            <nav className="jx-wikiBreadcrumb">
              {page.wiki_path.split('/').map((seg, i, arr) => (
                <span key={`${seg}-${i}`}>
                  {seg}
                  {i < arr.length - 1 && <i>/</i>}
                </span>
              ))}
            </nav>
          ) : null}
          <h1 className="jx-wikiArticleTitle">
            <span className={`jx-wikiTypeTag jx-wikiTypeTag--${page.page_type}`}>
              {page.type_label || TYPE_LABELS[page.page_type] || '其他'}
            </span>
            {page.title}
          </h1>
          {page.aliases?.length ? (
            <div className="jx-wikiAliases">
              别名：
              {page.aliases.map((alias) => (
                <span key={alias}>{alias}</span>
              ))}
            </div>
          ) : null}
        </div>

        <div className="jx-wikiArticleBody jx-md" dangerouslySetInnerHTML={{ __html: pageHtml }} />

        {page.related_pages?.length ? (
          <div className="jx-wikiRelated">
            <h4>相关概念</h4>
            <div className="jx-wikiRelatedList">
              {page.related_pages.slice(0, 24).map((rel) => (
                <button
                  key={rel.slug}
                  type="button"
                  className="jx-wikiRelatedChip"
                  title={rel.slug}
                  onClick={() => void openPage(rel.slug)}
                >
                  <span className={`jx-wikiRelatedChipDot jx-wikiTypeTag--${rel.page_type}`} />
                  {rel.title}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <div className="jx-wikiSource">
          <div className="jx-wikiSourceHead">
            <h4>原文出处</h4>
            <Button
              size="small"
              loading={sourceLoading}
              onClick={() => void loadSources()}
              disabled={!page.source_refs?.length}
            >
              {sources ? '重新回溯' : '查看原文出处'}
            </Button>
          </div>
          <p className="jx-wikiSourceHint">
            {page.source_refs?.length
              ? '按页面记录的血缘坐标直接取回原始段落，不是重新检索——所以取到的就是这页依据的那几段。'
              : '该页面没有记录来源坐标，无法回溯原文。'}
          </p>
          {sources?.length ? (
            <ol className="jx-wikiSourceList">
              {sources.map((chunk) => (
                <li key={chunk.chunk_id} className="jx-wikiSourceItem">
                  <div className="jx-wikiSourceItemHead">{chunk.document_title || '原始文档'}</div>
                  <p className="jx-wikiSourceItemBody">{chunk.content}</p>
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      </article>
    );
  };

  return (
    <div className="jx-wikiPanel">
      <header className="jx-wikiHeader">
        <div className="jx-wikiHeaderMain">
          <div className="jx-wikiTabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={view === 'browse'}
              className={`jx-wikiTab${view === 'browse' ? ' is-active' : ''}`}
              onClick={() => setView('browse')}
            >
              <FileTextOutlined />
              知识 Wiki
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={view === 'graph'}
              className={`jx-wikiTab${view === 'graph' ? ' is-active' : ''}`}
              onClick={() => setView('graph')}
            >
              <NodeIndexOutlined />
              概念图谱
            </button>
          </div>
          {stats && (
            <div className="jx-wikiStats">
              <span className="jx-wikiStatsTotal">
                <b>{stats.total_pages.toLocaleString()}</b> 个知识页
              </span>
              {statChips.map((chip) => (
                <span key={chip.label} className="jx-wikiStatChip">
                  {chip.label}
                  <b>{chip.value.toLocaleString()}</b>
                </span>
              ))}
            </div>
          )}
        </div>
        <p className="jx-wikiHeaderHint">
          由大模型从《{kbName}》的文档中抽取概念并互相链接而成，是这个知识库的结构地图；
          事实与出处请以每页底部回溯的原文为准。
        </p>
      </header>

      {view === 'browse' ? (
        <div className="jx-wikiBody">
          {renderSidebar()}
          <section className="jx-wikiReader" ref={readerRef}>
            {showIndex ? (
              renderIndexOverview()
            ) : pageLoading ? (
              <div className="jx-wikiReaderLoading">
                <Spin />
              </div>
            ) : page ? (
              renderArticle()
            ) : (
              <div className="jx-wikiReaderPlaceholder">
                <div className="jx-wikiReaderPlaceholderIcon">
                  <FileTextOutlined />
                </div>
                <h3>从左侧选择一个知识页</h3>
                <p>
                  概念页解释「是什么」，实体页记录具体的机构、文件与制度，
                  文档摘要则对应一篇原始文档。页面之间的蓝色链接可以直接跳转。
                </p>
              </div>
            )}
          </section>
        </div>
      ) : (
        <WikiGraphView
          kbId={kbId}
          onOpenPage={(slug) => {
            setView('browse');
            void openPage(slug);
          }}
        />
      )}
    </div>
  );
}

export default WikiPanel;
