import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeftOutlined,
  CloseOutlined,
  CompressOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Empty, Spin, message } from 'antd';
import { getWikiGraph, getWikiPage } from '../../api';
import type { WikiGraphData, WikiGraphNode, WikiPageDetail } from '../../types';
import { mdToHtml } from '../../utils/markdown';
import { ConceptGraph, type ConceptGraphHandle } from './ConceptGraph';
import { GRAPH_TYPE_STYLE } from './wikiGraphTheme';

/**
 * 概念图谱视图：交互式力导向图 + 图例过滤 + 右侧节点详情抽屉。
 *
 * 全库两千多节点、上万条边，一次性画出来既卡也读不出结构，所以取的是**按关联度
 * 排序的前 N 个枢纽**（overview）或**某节点的邻域**（ego）；节点数可由用户调档。
 * 点节点在右侧滑出**悬浮抽屉**讲清楚「这是什么、连着谁」——抽屉浮在画布上层，
 * 不挤压画布、不触发重排，看完关掉画布原样。双击节点直接以它为中心展开。
 */

// 默认 40：这个量级标签还读得出；再多就只适合看整体形态了
const NODE_BUDGETS = [40, 80, 140] as const;

interface WikiGraphViewProps {
  kbId: string;
  onOpenPage: (slug: string) => void;
}

export function WikiGraphView({ kbId, onOpenPage }: WikiGraphViewProps) {
  const [graph, setGraph] = useState<WikiGraphData | null>(null);
  const [loading, setLoading] = useState(false);
  const [center, setCenter] = useState('');
  const [budget, setBudget] = useState<number>(NODE_BUDGETS[0]);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());

  const [selected, setSelected] = useState<WikiGraphNode | null>(null);
  const [detail, setDetail] = useState<WikiPageDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const graphRef = useRef<ConceptGraphHandle>(null);

  const loadGraph = useCallback(
    async (nextCenter = '', nextBudget = budget) => {
      setLoading(true);
      try {
        const data = nextCenter
          ? await getWikiGraph(kbId, {
              mode: 'ego',
              center: nextCenter,
              depth: 1,
              limit: nextBudget,
            })
          : await getWikiGraph(kbId, { mode: 'overview', limit: nextBudget });
        setGraph(data);
        setCenter(nextCenter);
      } catch (e) {
        message.error(`加载图谱失败：${(e as Error).message}`);
      } finally {
        setLoading(false);
      }
    },
    [kbId, budget],
  );

  useEffect(() => {
    setGraph(null);
    setCenter('');
    setSelected(null);
    setDetail(null);
    setHiddenTypes(new Set());
    void loadGraph('');
    // loadGraph 依赖 budget，这里只想在换库时重置，故不把它列进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId]);

  // 选中节点后拉它的页面详情，用于右侧抽屉
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    void getWikiPage(kbId, selected.slug)
      .then((d) => alive && setDetail(d))
      .catch(() => alive && setDetail(null))
      .finally(() => alive && setDetailLoading(false));
    return () => {
      alive = false;
    };
  }, [kbId, selected]);

  /** 图例点击：把某个类型整体隐藏/显示，用来在密集图里只看某一类 */
  const toggleType = useCallback((type: string) => {
    setHiddenTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }, []);

  const visibleGraph = useMemo<WikiGraphData | null>(() => {
    if (!graph) return null;
    if (!hiddenTypes.size) return graph;
    const nodes = graph.nodes.filter((n) => !hiddenTypes.has(n.page_type));
    const keep = new Set(nodes.map((n) => n.slug));
    return {
      nodes,
      edges: graph.edges.filter((e) => keep.has(e.source) && keep.has(e.target)),
      meta: graph.meta,
    };
  }, [graph, hiddenTypes]);

  /** 当前图里与选中节点直接相连的节点——抽屉的「关联邻居」 */
  const neighbours = useMemo(() => {
    if (!graph || !selected) return [];
    const linked = new Set<string>();
    for (const edge of graph.edges) {
      if (edge.source === selected.slug) linked.add(edge.target);
      else if (edge.target === selected.slug) linked.add(edge.source);
    }
    return graph.nodes
      .filter((n) => linked.has(n.slug))
      .sort((a, b) => (b.link_count || 0) - (a.link_count || 0));
  }, [graph, selected]);

  const typesPresent = useMemo(() => {
    const seen = new Set<string>();
    for (const node of graph?.nodes || []) seen.add(node.page_type);
    return Array.from(seen).sort();
  }, [graph]);

  const detailHtml = useMemo(
    () => (detail?.content ? mdToHtml(detail.content) : ''),
    [detail],
  );

  const meta = graph?.meta;
  // 自建库返回 total/returned；外接后端可能只带旧键，逐级兜底避免展示 undefined
  const metaTotal = meta?.total ?? meta?.total_pages;
  const metaReturned = meta?.returned ?? graph?.nodes.length;

  return (
    <div className="jx-wikiGraphWrap">
      <div className="jx-wikiGraphToolbar">
        {center ? (
          <Button icon={<ArrowLeftOutlined />} onClick={() => void loadGraph('')}>
            回到全局视图
          </Button>
        ) : (
          <span className="jx-wikiGraphToolbarLabel">全局视图 · 关联最密集的枢纽概念</span>
        )}

        <div className="jx-wikiGraphBudget" role="group" aria-label="节点数量">
          <span>节点</span>
          {NODE_BUDGETS.map((n) => (
            <button
              key={n}
              type="button"
              className={`jx-wikiGraphBudgetBtn${budget === n ? ' is-active' : ''}`}
              onClick={() => {
                setBudget(n);
                void loadGraph(center, n);
              }}
            >
              {n}
            </button>
          ))}
        </div>

        {meta?.truncated && metaTotal != null && (
          <span className="jx-wikiGraphTruncated">
            共 {metaTotal.toLocaleString()} 个节点，已按关联度取前 {metaReturned}
          </span>
        )}
        <Button
          type="text"
          icon={<CompressOutlined />}
          onClick={() => graphRef.current?.fitToView()}
        >
          适应屏幕
        </Button>
        <Button
          type="text"
          icon={<ReloadOutlined />}
          onClick={() => void loadGraph(center)}
          loading={loading}
        >
          刷新
        </Button>
      </div>

      <div className="jx-wikiGraphStage">
        <div className="jx-wikiGraphStageMain">
          {loading ? (
            <div className="jx-wikiGraphLoading">
              <Spin tip="正在布局概念关系…" />
            </div>
          ) : visibleGraph && visibleGraph.nodes.length ? (
            <ConceptGraph
              ref={graphRef}
              data={visibleGraph}
              centerSlug={center}
              selectedSlug={selected?.slug}
              onSelectNode={setSelected}
              onExpandNode={(n) => void loadGraph(n.slug)}
              onClearSelect={() => setSelected(null)}
            />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无图谱数据" />
          )}

          {typesPresent.length > 0 && (
            <div className="jx-wikiGraphLegend">
              {typesPresent.map((type) => {
                const style = GRAPH_TYPE_STYLE[type];
                const off = hiddenTypes.has(type);
                return (
                  <button
                    key={type}
                    type="button"
                    className={`jx-wikiGraphLegendItem${off ? ' is-off' : ''}`}
                    onClick={() => toggleType(type)}
                    title={off ? '点击显示' : '点击隐藏'}
                  >
                    <i style={{ background: style?.fill, borderColor: style?.stroke }} />
                    {style?.label || type}
                  </button>
                );
              })}
              <span className="jx-wikiGraphLegendHint">
                拖拽节点 · 滚轮缩放 · 单击看详情 · 双击以此展开
              </span>
            </div>
          )}

          {selected && (
            <aside className="jx-wikiGraphDrawer">
              <div className="jx-wikiGraphPanelHead">
                <span
                  className={`jx-wikiTypeTag jx-wikiTypeTag--${selected.page_type}`}
                >
                  {GRAPH_TYPE_STYLE[selected.page_type]?.label || selected.page_type}
                </span>
                <h3 title={selected.title}>{selected.title}</h3>
                <Button
                  type="text"
                  size="small"
                  icon={<CloseOutlined />}
                  aria-label="关闭"
                  onClick={() => setSelected(null)}
                />
              </div>

              <div className="jx-wikiGraphPanelMeta">
                <span>
                  关联 <b>{selected.link_count ?? neighbours.length}</b> 条
                </span>
                {detail?.source_refs?.length ? (
                  <span>
                    来源 <b>{detail.source_refs.length}</b> 篇
                  </span>
                ) : null}
              </div>

              <div className="jx-wikiGraphPanelActions">
                <Button
                  size="small"
                  type="primary"
                  disabled={center === selected.slug}
                  onClick={() => void loadGraph(selected.slug)}
                >
                  以此为中心展开
                </Button>
                <Button size="small" onClick={() => onOpenPage(selected.slug)}>
                  阅读全文
                </Button>
              </div>

              <div className="jx-wikiGraphPanelBody">
                {detailLoading ? (
                  <div className="jx-wikiGraphPanelLoading">
                    <Spin size="small" />
                  </div>
                ) : detail ? (
                  <>
                    {detail.summary ? (
                      <p className="jx-wikiGraphPanelSummary">{detail.summary}</p>
                    ) : null}
                    {detailHtml ? (
                      <div
                        className="jx-wikiGraphPanelContent jx-md"
                        dangerouslySetInnerHTML={{ __html: detailHtml }}
                      />
                    ) : null}
                  </>
                ) : (
                  <p className="jx-wikiGraphPanelSummary">未能加载该页面内容。</p>
                )}

                <div className="jx-wikiGraphPanelNeighbours">
                  <h4>关联邻居（{neighbours.length}）</h4>
                  {neighbours.length === 0 ? (
                    <p className="jx-wikiGraphPanelSummary">当前视图里没有它的邻居，可展开看看。</p>
                  ) : (
                    <div className="jx-wikiGraphNeighbourList">
                      {neighbours.slice(0, 30).map((n) => (
                        <button
                          key={n.slug}
                          type="button"
                          className="jx-wikiGraphNeighbour"
                          onClick={() => setSelected(n)}
                          title={n.title}
                        >
                          <span
                            className="jx-wikiGraphNeighbourDot"
                            style={{
                              background: GRAPH_TYPE_STYLE[n.page_type]?.fill,
                              borderColor: GRAPH_TYPE_STYLE[n.page_type]?.stroke,
                            }}
                          />
                          <span className="jx-wikiGraphNeighbourName">{n.title}</span>
                          <span className="jx-wikiGraphNeighbourCount">{n.link_count ?? 0}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}

export default WikiGraphView;
