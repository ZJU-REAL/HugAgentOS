import {
  ApartmentOutlined,
  CloseOutlined,
  CompressOutlined,
  ExpandOutlined,
  ExportOutlined,
  MinusOutlined,
  PlusOutlined,
  TeamOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons';
import { Empty, Pagination, Spin, Tag } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  getIndustryNodeCompanies,
  type IndustryNodeCompaniesResponse,
} from '../../api';
import { t } from '../../i18n';
import { useCanvasStore } from '../../stores';
import {
  INDUSTRY_CHAIN_DEFAULT_LEVELS,
  INDUSTRY_CHAIN_MAX_LEVELS,
  parseIndustryChainOutput,
  type IndustryChainTreeNode,
} from '../../utils/industryChain';

interface PositionedNode {
  node: IndustryChainTreeNode;
  depth: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

interface PositionedEdge {
  source: PositionedNode;
  target: PositionedNode;
}

interface TreeLayout {
  nodes: PositionedNode[];
  edges: PositionedEdge[];
  width: number;
  height: number;
}

interface ViewTransform {
  x: number;
  y: number;
  scale: number;
}

interface WorldBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

const NODE_HEIGHT = 44;
const COLUMN_GAP = 260;
const ROW_GAP = 72;
const WORLD_PADDING = 48;
const MIN_SCALE = 0.28;
const MAX_SCALE = 1.8;
const AUTO_VIEW_DURATION_MS = 340;
const COMPANY_DETAIL_URL = 'https://ningboi.com/analysis/knowledge/company';
// Root is depth 0. Keep three levels visible initially; "expand all" may reveal
// the fourth level, while the layout guard prevents fifth-level legacy data.
const MAX_VISIBLE_DEPTH = INDUSTRY_CHAIN_MAX_LEVELS - 1;
const DEFAULT_EXPANDED_DEPTH = INDUSTRY_CHAIN_DEFAULT_LEVELS - 1;

function nodeWidth(label: string): number {
  const visualLength = Array.from(label).reduce(
    (length, character) => length + (character.charCodeAt(0) > 0xff ? 1 : 0.58),
    0,
  );
  return Math.max(112, Math.min(224, 54 + visualLength * 15));
}

function createTreeLayout(root: IndustryChainTreeNode, collapsed: Set<string>): TreeLayout {
  const nodes: PositionedNode[] = [];
  const edges: PositionedEdge[] = [];
  let nextLeafCenter = WORLD_PADDING + NODE_HEIGHT / 2;
  let maxDepth = 0;

  const visit = (node: IndustryChainTreeNode, depth: number): PositionedNode => {
    maxDepth = Math.max(maxDepth, depth);
    const children = depth >= MAX_VISIBLE_DEPTH || collapsed.has(node.id) ? [] : node.children;
    const childNodes = children.map((child) => visit(child, depth + 1));
    const centerY = childNodes.length > 0
      ? (childNodes[0].y + childNodes[childNodes.length - 1].y + NODE_HEIGHT) / 2
      : nextLeafCenter;

    if (childNodes.length === 0) nextLeafCenter += ROW_GAP;

    const positioned: PositionedNode = {
      node,
      depth,
      x: WORLD_PADDING + depth * COLUMN_GAP,
      y: centerY - NODE_HEIGHT / 2,
      width: nodeWidth(node.label),
      height: NODE_HEIGHT,
    };
    nodes.push(positioned);
    childNodes.forEach((child) => edges.push({ source: positioned, target: child }));
    return positioned;
  };

  visit(root, 0);
  const widestRightEdge = nodes.reduce((right, node) => Math.max(right, node.x + node.width), 0);
  const lowestBottomEdge = nodes.reduce((bottom, node) => Math.max(bottom, node.y + node.height), 0);

  return {
    nodes,
    edges,
    width: Math.max(widestRightEdge + WORLD_PADDING, (maxDepth + 1) * COLUMN_GAP),
    height: Math.max(lowestBottomEdge + WORLD_PADDING, 260),
  };
}

function edgePath(edge: PositionedEdge): string {
  const startX = edge.source.x + edge.source.width;
  const startY = edge.source.y + edge.source.height / 2;
  const endX = edge.target.x;
  const endY = edge.target.y + edge.target.height / 2;
  const curve = Math.max(56, (endX - startX) * 0.48);
  return `M ${startX} ${startY} C ${startX + curve} ${startY}, ${endX - curve} ${endY}, ${endX} ${endY}`;
}

function allBranchIds(root: IndustryChainTreeNode): string[] {
  const ids: string[] = [];
  const visit = (node: IndustryChainTreeNode) => {
    if (node.children.length > 0) ids.push(node.id);
    node.children.forEach(visit);
  };
  visit(root);
  return ids;
}

function branchIdsFromDepth(root: IndustryChainTreeNode, minimumDepth: number): string[] {
  const ids: string[] = [];
  const visit = (node: IndustryChainTreeNode, depth: number) => {
    if (node.children.length > 0 && depth >= minimumDepth) ids.push(node.id);
    node.children.forEach((child) => visit(child, depth + 1));
  };
  visit(root, 0);
  return ids;
}

function clampScale(scale: number): number {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
}

function fitBoundsToViewport(
  bounds: WorldBounds,
  viewportWidth: number,
  viewportHeight: number,
  padding = 56,
  maximumScale = 1,
): ViewTransform {
  const availableWidth = Math.max(1, viewportWidth - padding * 2);
  const availableHeight = Math.max(1, viewportHeight - padding * 2);
  const scale = clampScale(Math.min(
    availableWidth / Math.max(1, bounds.width),
    availableHeight / Math.max(1, bounds.height),
    maximumScale,
  ));
  return {
    scale,
    x: (viewportWidth - bounds.width * scale) / 2 - bounds.x * scale,
    y: (viewportHeight - bounds.height * scale) / 2 - bounds.y * scale,
  };
}

function subtreeBounds(layout: TreeLayout, root: IndustryChainTreeNode): WorldBounds | null {
  const subtreeIds = new Set<string>();
  const visit = (node: IndustryChainTreeNode) => {
    subtreeIds.add(node.id);
    node.children.forEach(visit);
  };
  visit(root);

  const nodes = layout.nodes.filter((positioned) => subtreeIds.has(positioned.node.id));
  if (nodes.length === 0) return null;
  const inset = 28;
  const left = Math.min(...nodes.map((node) => node.x)) - inset;
  const top = Math.min(...nodes.map((node) => node.y)) - inset;
  const right = Math.max(...nodes.map((node) => node.x + node.width)) + inset;
  const bottom = Math.max(...nodes.map((node) => node.y + node.height)) + inset;
  return {
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
  };
}

function companyDetailUrl(companyId: string): string {
  return `${COMPANY_DETAIL_URL}?id=${encodeURIComponent(companyId)}`;
}

export function IndustryChainCanvas() {
  const target = useCanvasStore((state) => state.industryChainTarget);
  const closeCanvas = useCanvasStore((state) => state.closeCanvas);
  const viewportRef = useRef<HTMLDivElement>(null);
  const companyPanelRef = useRef<HTMLElement>(null);
  const didInitialPositionRef = useRef(false);
  const panRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  const companyRequestRef = useRef<AbortController | null>(null);
  const autoViewFrameRef = useRef<number | null>(null);
  const autoViewTimerRef = useRef<number | null>(null);
  const panelTransitionFrameRef = useRef<number | null>(null);
  const viewBeforeCompanyPanelRef = useRef<ViewTransform | null>(null);
  const pendingLayoutFocusRef = useRef<IndustryChainTreeNode | 'fit-all' | null>(null);
  // null means the user has not changed expansion yet, so the tree can derive
  // its default collapsed branches as soon as the asynchronous result arrives.
  const [collapsed, setCollapsed] = useState<Set<string> | null>(null);
  const [view, setView] = useState<ViewTransform>({ x: 24, y: 24, scale: 1 });
  const [dragging, setDragging] = useState(false);
  const [viewAnimating, setViewAnimating] = useState(false);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });
  const [selectedNode, setSelectedNode] = useState<IndustryChainTreeNode | null>(null);
  const [companies, setCompanies] = useState<IndustryNodeCompaniesResponse | null>(null);
  const [companiesLoading, setCompaniesLoading] = useState(false);
  const [companiesError, setCompaniesError] = useState('');

  const model = useMemo(
    () => parseIndustryChainOutput(target?.output, target?.chainName),
    [target?.chainName, target?.output],
  );
  const defaultCollapsed = useMemo(
    () => model.tree
      ? new Set(branchIdsFromDepth(model.tree, DEFAULT_EXPANDED_DEPTH))
      : new Set<string>(),
    [model.tree],
  );
  const effectiveCollapsed = collapsed ?? defaultCollapsed;
  const layout = useMemo(
    () => model.tree ? createTreeLayout(model.tree, effectiveCollapsed) : null,
    [effectiveCollapsed, model.tree],
  );

  const animateToView = useCallback((nextView: ViewTransform) => {
    if (autoViewFrameRef.current != null) {
      window.cancelAnimationFrame(autoViewFrameRef.current);
    }
    if (autoViewTimerRef.current != null) {
      window.clearTimeout(autoViewTimerRef.current);
    }
    setViewAnimating(true);
    autoViewFrameRef.current = window.requestAnimationFrame(() => {
      setView(nextView);
      autoViewFrameRef.current = null;
      autoViewTimerRef.current = window.setTimeout(() => {
        setViewAnimating(false);
        autoViewTimerRef.current = null;
      }, AUTO_VIEW_DURATION_MS);
    });
  }, []);

  const getFittedView = useCallback((): ViewTransform | null => {
    if (!layout || !viewportRef.current) return null;
    const rect = viewportRef.current.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return fitBoundsToViewport(
      { x: 0, y: 0, width: layout.width, height: layout.height },
      rect.width,
      rect.height,
    );
  }, [layout]);

  const fitToView = useCallback(() => {
    const nextView = getFittedView();
    if (nextView) animateToView(nextView);
  }, [animateToView, getFittedView]);

  const focusSelectedNodeAlongsideCompanyPanel = useCallback((
    selectedLeaf: IndustryChainTreeNode,
    previousScale: number,
  ) => {
    if (!layout || !viewportRef.current || !companyPanelRef.current) return;
    const viewport = viewportRef.current;
    const body = viewport.parentElement;
    if (!body) return;
    const bodyRect = body.getBoundingClientRect();
    const panelRect = companyPanelRef.current.getBoundingClientRect();
    if (!bodyRect.width || !bodyRect.height) return;
    const panelOverlaysGraph = window.matchMedia('(max-width: 820px)').matches;
    const finalViewportWidth = panelOverlaysGraph
      ? bodyRect.width
      : Math.max(1, bodyRect.width - panelRect.width);
    const positionedLeaf = layout.nodes.find((item) => item.node.id === selectedLeaf.id);
    if (!positionedLeaf) return;
    const nodeFitScale = (finalViewportWidth - 56) / positionedLeaf.width;
    const scale = clampScale(Math.min(
      Math.max(previousScale, 0.82),
      nodeFitScale,
      1,
    ));
    const focusX = finalViewportWidth * 0.64;
    animateToView({
      scale,
      x: focusX - (positionedLeaf.x + positionedLeaf.width / 2) * scale,
      y: bodyRect.height / 2 - (positionedLeaf.y + positionedLeaf.height / 2) * scale,
    });
  }, [animateToView, layout]);

  const focusVisibleSubtree = useCallback((node: IndustryChainTreeNode) => {
    if (!layout || !viewportRef.current) return;
    const rect = viewportRef.current.getBoundingClientRect();
    const bounds = subtreeBounds(layout, node);
    if (!bounds || !rect.width || !rect.height) return;
    animateToView(fitBoundsToViewport(bounds, rect.width, rect.height, 48));
  }, [animateToView, layout]);

  const focusRoot = useCallback(() => {
    if (!layout || !viewportRef.current) return;
    const rect = viewportRef.current.getBoundingClientRect();
    const root = layout.nodes.find((node) => node.depth === 0);
    if (!rect.width || !rect.height || !root) return;
    const horizontalFit = (rect.width - 96) / layout.width;
    const scale = clampScale(Math.max(0.82, Math.min(1, horizontalFit)));
    setView({
      scale,
      x: 42 - root.x * scale,
      y: rect.height / 2 - (root.y + root.height / 2) * scale,
    });
  }, [layout]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return undefined;
    const updateSize = () => {
      const rect = viewport.getBoundingClientRect();
      setViewportSize({ width: rect.width, height: rect.height });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [target?.status]);

  useEffect(() => {
    if (!layout || didInitialPositionRef.current) return undefined;
    didInitialPositionRef.current = true;
    const frame = window.requestAnimationFrame(focusRoot);
    return () => window.cancelAnimationFrame(frame);
  }, [focusRoot, layout]);

  useEffect(() => {
    if (!layout || !pendingLayoutFocusRef.current) return undefined;
    const focusTarget = pendingLayoutFocusRef.current;
    pendingLayoutFocusRef.current = null;
    const frame = window.requestAnimationFrame(() => {
      if (focusTarget === 'fit-all') fitToView();
      else focusVisibleSubtree(focusTarget);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [fitToView, focusVisibleSubtree, layout]);

  useEffect(() => () => {
    companyRequestRef.current?.abort();
    if (autoViewFrameRef.current != null) window.cancelAnimationFrame(autoViewFrameRef.current);
    if (autoViewTimerRef.current != null) window.clearTimeout(autoViewTimerRef.current);
    if (panelTransitionFrameRef.current != null) {
      window.cancelAnimationFrame(panelTransitionFrameRef.current);
    }
  }, []);

  const zoomAtCenter = useCallback((factor: number) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    setView((current) => {
      const nextScale = clampScale(current.scale * factor);
      const worldX = (rect.width / 2 - current.x) / current.scale;
      const worldY = (rect.height / 2 - current.y) / current.scale;
      return {
        scale: nextScale,
        x: rect.width / 2 - worldX * nextScale,
        y: rect.height / 2 - worldY * nextScale,
      };
    });
  }, []);

  const handleWheel = useCallback((event: React.WheelEvent<HTMLDivElement>) => {
    if (!viewportRef.current) return;
    event.preventDefault();
    const rect = viewportRef.current.getBoundingClientRect();
    const pointerX = event.clientX - rect.left;
    const pointerY = event.clientY - rect.top;
    setView((current) => {
      const nextScale = clampScale(current.scale * Math.exp(-event.deltaY * 0.0012));
      const worldX = (pointerX - current.x) / current.scale;
      const worldY = (pointerY - current.y) / current.scale;
      return {
        scale: nextScale,
        x: pointerX - worldX * nextScale,
        y: pointerY - worldY * nextScale,
      };
    });
  }, []);

  const handlePointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button, .jx-industryChain-node, .jx-industryChain-minimap')) return;
    panRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: view.x,
      originY: view.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  }, [view.x, view.y]);

  const handlePointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (!pan || pan.pointerId !== event.pointerId) return;
    setView((current) => ({
      ...current,
      x: pan.originX + event.clientX - pan.startX,
      y: pan.originY + event.clientY - pan.startY,
    }));
  }, []);

  const handlePointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (panRef.current?.pointerId !== event.pointerId) return;
    panRef.current = null;
    setDragging(false);
    event.currentTarget.releasePointerCapture(event.pointerId);
  }, []);

  const toggleNode = useCallback((node: IndustryChainTreeNode) => {
    if (node.children.length === 0) return;
    setCollapsed((current) => {
      const next = new Set(current ?? defaultCollapsed);
      if (next.has(node.id)) {
        next.delete(node.id);
        pendingLayoutFocusRef.current = node;
      } else {
        next.add(node.id);
      }
      return next;
    });
  }, [defaultCollapsed]);

  const collapseAll = useCallback(() => {
    if (!model.tree) return;
    pendingLayoutFocusRef.current = 'fit-all';
    setCollapsed(new Set(allBranchIds(model.tree).filter((id) => id !== model.tree?.id)));
  }, [model.tree]);

  const expandAll = useCallback(() => {
    pendingLayoutFocusRef.current = 'fit-all';
    setCollapsed(new Set());
  }, []);

  const loadNodeCompanies = useCallback(async (node: IndustryChainTreeNode, page: number) => {
    if (!model.chainId || !node.nodeId) {
      setCompanies(null);
      setCompaniesError(t('当前图谱缺少节点标识，暂时无法查询关联企业'));
      return;
    }
    companyRequestRef.current?.abort();
    const controller = new AbortController();
    companyRequestRef.current = controller;
    setCompaniesLoading(true);
    setCompaniesError('');
    try {
      const result = await getIndustryNodeCompanies(
        model.chainId,
        node.nodeId,
        page,
        10,
        controller.signal,
      );
      if (!controller.signal.aborted) setCompanies(result);
    } catch (error) {
      if (!controller.signal.aborted) {
        setCompanies(null);
        setCompaniesError(error instanceof Error ? error.message : t('关联企业加载失败'));
      }
    } finally {
      if (!controller.signal.aborted) setCompaniesLoading(false);
    }
  }, [model.chainId]);

  const openNodeCompanies = useCallback((node: IndustryChainTreeNode) => {
    if (node.children.length > 0) return;
    const panelWasClosed = selectedNode === null;
    setSelectedNode(node);
    setCompanies(null);
    void loadNodeCompanies(node, 1);
    if (panelWasClosed) {
      viewBeforeCompanyPanelRef.current = view;
      if (panelTransitionFrameRef.current != null) {
        window.cancelAnimationFrame(panelTransitionFrameRef.current);
      }
      panelTransitionFrameRef.current = window.requestAnimationFrame(() => {
        panelTransitionFrameRef.current = null;
        focusSelectedNodeAlongsideCompanyPanel(node, view.scale);
      });
    }
  }, [focusSelectedNodeAlongsideCompanyPanel, loadNodeCompanies, selectedNode, view]);

  const closeNodeCompanies = useCallback(() => {
    companyRequestRef.current?.abort();
    setSelectedNode(null);
    setCompanies(null);
    setCompaniesError('');
    setCompaniesLoading(false);
    if (panelTransitionFrameRef.current != null) {
      window.cancelAnimationFrame(panelTransitionFrameRef.current);
      panelTransitionFrameRef.current = null;
    }
    const previousView = viewBeforeCompanyPanelRef.current;
    viewBeforeCompanyPanelRef.current = null;
    if (previousView) animateToView(previousView);
    else window.requestAnimationFrame(fitToView);
  }, [animateToView, fitToView]);

  const handleMinimapClick = useCallback((event: React.MouseEvent<SVGSVGElement>) => {
    if (!layout || !viewportRef.current) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const worldX = ((event.clientX - rect.left) / rect.width) * layout.width;
    const worldY = ((event.clientY - rect.top) / rect.height) * layout.height;
    const viewport = viewportRef.current.getBoundingClientRect();
    setView((current) => ({
      ...current,
      x: viewport.width / 2 - worldX * current.scale,
      y: viewport.height / 2 - worldY * current.scale,
    }));
  }, [layout]);

  if (!target) return null;

  const isLoading = target.status === 'loading';
  const error = target.error || model.error;
  const minimapViewport = layout ? {
    x: Math.max(0, -view.x / view.scale),
    y: Math.max(0, -view.y / view.scale),
    width: Math.min(layout.width, viewportSize.width / view.scale),
    height: Math.min(layout.height, viewportSize.height / view.scale),
  } : null;

  return (
    <aside className="jx-rightSidebar jx-industryChain" aria-label={t('产业链图谱')}>
      <header className="jx-industryChain-header">
        <div className="jx-industryChain-heading">
          <span className="jx-industryChain-headingIcon"><ApartmentOutlined /></span>
          <div className="jx-industryChain-headingText">
            <strong>{model.title || target.chainName || t('产业链图谱')}</strong>
            <span>
              {isLoading
                ? t('正在生成产业链图谱…')
                : t('产业链图谱 · {n} 个节点', { n: model.nodeCount })}
            </span>
          </div>
        </div>
        <div className="jx-canvas-header-actions">
          <button className="jx-canvas-actionBtn" onClick={() => zoomAtCenter(1.18)} title={t('放大')}>
            <ZoomInOutlined />
          </button>
          <button className="jx-canvas-actionBtn" onClick={() => zoomAtCenter(1 / 1.18)} title={t('缩小')}>
            <ZoomOutOutlined />
          </button>
          <button className="jx-canvas-actionBtn" onClick={fitToView} title={t('适应画布')}>
            <CompressOutlined />
          </button>
          <button className="jx-canvas-actionBtn jx-canvas-closeBtn" onClick={closeCanvas} title={t('关闭图谱')}>
            <CloseOutlined />
          </button>
        </div>
      </header>

      {!isLoading && model.metrics.length > 0 && (
        <div className="jx-industryChain-metrics" aria-label={t('产业链关键指标')}>
          {model.metrics.map((metric) => (
            <span key={metric.label} className="jx-industryChain-metric">
              <small>{metric.label}</small>
              <strong>{typeof metric.value === 'number' ? metric.value.toLocaleString() : metric.value}</strong>
            </span>
          ))}
        </div>
      )}

      <div className={`jx-industryChain-body${selectedNode ? ' has-company-panel' : ''}`}>
        {isLoading ? (
          <div className="jx-canvas-loading">
            <div className="jx-canvas-spinner" />
            <span>{t('正在获取产业链结构，完成后将在这里自动展开')}</span>
          </div>
        ) : error ? (
          <div className="jx-canvas-error">{error}</div>
        ) : !layout || !model.tree ? (
          <div className="jx-canvas-error">{t('未返回可展示的产业链结构')}</div>
        ) : (
          <div
            ref={viewportRef}
            className={`jx-industryChain-viewport${dragging ? ' is-dragging' : ''}`}
            onWheel={handleWheel}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerCancel={handlePointerUp}
          >
            <div
              className={`jx-industryChain-world${viewAnimating ? ' is-view-animating' : ''}`}
              style={{
                width: layout.width,
                height: layout.height,
                transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
              }}
              role="tree"
              aria-label={t('{name}产业链层级', { name: model.title })}
            >
              <svg
                className="jx-industryChain-edges"
                width={layout.width}
                height={layout.height}
                viewBox={`0 0 ${layout.width} ${layout.height}`}
                aria-hidden="true"
              >
                {layout.edges.map((edge) => (
                  <path
                    key={`${edge.source.node.id}-${edge.target.node.id}`}
                    d={edgePath(edge)}
                  />
                ))}
              </svg>
              {layout.nodes.map((positioned) => {
                const branch = positioned.node.children.length > 0;
                const selectableLeaf = !branch && !!positioned.node.nodeId;
                const isCollapsed = effectiveCollapsed.has(positioned.node.id);
                const isSelected = selectedNode?.id === positioned.node.id;
                return (
                  <div
                    key={positioned.node.id}
                    className={`jx-industryChain-node jx-industryChain-node--depth${Math.min(positioned.depth, 2)}${branch ? ' is-expandable' : ''}${selectableLeaf ? ' is-selectable' : ''}${isSelected ? ' is-selected' : ''}`}
                    style={{
                      left: positioned.x,
                      top: positioned.y,
                      width: positioned.width,
                      minHeight: positioned.height,
                    }}
                    role="treeitem"
                    aria-level={positioned.depth + 1}
                    aria-expanded={branch ? !isCollapsed : undefined}
                    aria-selected={selectableLeaf ? isSelected : undefined}
                    tabIndex={branch || selectableLeaf ? 0 : undefined}
                    onClick={() => {
                      if (branch) toggleNode(positioned.node);
                      else if (selectableLeaf) openNodeCompanies(positioned.node);
                    }}
                    onKeyDown={(event) => {
                      if (event.target !== event.currentTarget) return;
                      if ((branch || selectableLeaf) && (event.key === 'Enter' || event.key === ' ')) {
                        event.preventDefault();
                        if (branch) toggleNode(positioned.node);
                        else openNodeCompanies(positioned.node);
                      }
                    }}
                    title={positioned.node.label}
                  >
                    <span className="jx-industryChain-nodeAccent" aria-hidden="true" />
                    <span className="jx-industryChain-nodeLabel">{positioned.node.label}</span>
                    {selectableLeaf && (
                      <span className="jx-industryChain-nodeCompanies" title={t('查看关联企业')}>
                        <TeamOutlined />
                        {positioned.node.companyCount ?? ''}
                      </span>
                    )}
                    {branch && (
                      <button
                        className="jx-industryChain-nodeToggle"
                        onClick={(event) => {
                          event.stopPropagation();
                          toggleNode(positioned.node);
                        }}
                        title={isCollapsed ? t('展开下级环节') : t('收起下级环节')}
                        aria-label={isCollapsed
                          ? t('展开{name}的下级环节', { name: positioned.node.label })
                          : t('收起{name}的下级环节', { name: positioned.node.label })}
                      >
                        {isCollapsed ? <PlusOutlined /> : <MinusOutlined />}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="jx-industryChain-toolbar" role="toolbar" aria-label={t('图谱操作')}>
              <button onClick={expandAll}><ExpandOutlined />{t('一键展开')}</button>
              <button onClick={collapseAll}><CompressOutlined />{t('一键收起')}</button>
              <span>{Math.round(view.scale * 100)}%</span>
            </div>

            <div className="jx-industryChain-minimap" title={t('点击缩略图快速定位')}>
              <svg
                viewBox={`0 0 ${layout.width} ${layout.height}`}
                preserveAspectRatio="xMidYMid meet"
                onClick={handleMinimapClick}
                aria-label={t('图谱缩略导航')}
                role="img"
              >
                {layout.edges.map((edge) => (
                  <path
                    key={`mini-${edge.source.node.id}-${edge.target.node.id}`}
                    d={edgePath(edge)}
                    className="jx-industryChain-minimapEdge"
                  />
                ))}
                {layout.nodes.map((positioned) => (
                  <rect
                    key={`mini-${positioned.node.id}`}
                    x={positioned.x}
                    y={positioned.y}
                    width={positioned.width}
                    height={positioned.height}
                    rx="6"
                    className="jx-industryChain-minimapNode"
                  />
                ))}
                {minimapViewport && (
                  <rect
                    x={minimapViewport.x}
                    y={minimapViewport.y}
                    width={minimapViewport.width}
                    height={minimapViewport.height}
                    className="jx-industryChain-minimapViewport"
                  />
                )}
              </svg>
            </div>

            <div className="jx-industryChain-hint">{t('拖拽移动 · 滚轮缩放')}</div>
          </div>
        )}

        {selectedNode && (
          <section
            ref={companyPanelRef}
            className="jx-industryChain-companyPanel"
            aria-label={t('{name}关联企业', { name: selectedNode.label })}
          >
            <header className="jx-industryChain-companyHeader">
              <div>
                <span><TeamOutlined />{t('企业列表')}</span>
                <strong>{selectedNode.label}</strong>
                <small>
                  {companies
                    ? t('共 {n} 家关联企业', { n: companies.pagination.total_items })
                    : selectedNode.companyCount != null
                      ? t('共 {n} 家关联企业', { n: selectedNode.companyCount })
                      : t('正在查询关联企业')}
                </small>
              </div>
              <button onClick={closeNodeCompanies} title={t('关闭企业列表')} aria-label={t('关闭企业列表')}>
                <CloseOutlined />
              </button>
            </header>

            <div className="jx-industryChain-companyColumns" aria-hidden="true">
              <span>{t('企业名称')}</span>
              <span>{t('资质标签')}</span>
              <span>{t('所属地区')}</span>
            </div>

            <div className="jx-industryChain-companyBody" aria-live="polite">
              {companiesLoading ? (
                <div className="jx-industryChain-companyState"><Spin />{t('正在加载企业列表…')}</div>
              ) : companiesError ? (
                <div className="jx-industryChain-companyState is-error">
                  <span>{companiesError}</span>
                  <button onClick={() => void loadNodeCompanies(selectedNode, companies?.pagination.page ?? 1)}>
                    {t('重新加载')}
                  </button>
                </div>
              ) : !companies || companies.items.length === 0 ? (
                <div className="jx-industryChain-companyState"><Empty description={t('该节点暂无关联企业')} /></div>
              ) : (
                <div className="jx-industryChain-companyList">
                  {companies.items.map((company) => (
                    <a
                      key={company.id}
                      className="jx-industryChain-companyRow"
                      href={companyDetailUrl(company.id)}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={t('在新窗口查看{name}企业详情', { name: company.name })}
                    >
                      <div className="jx-industryChain-companyName">
                        <span className="jx-industryChain-companyNameTitle">
                          <strong title={company.name}>{company.name}</strong>
                          <ExportOutlined title={t('查看企业详情')} />
                        </span>
                        <small>
                          {[company.establish_date, company.registered_capital].filter(Boolean).join(' · ') || t('暂无工商摘要')}
                        </small>
                      </div>
                      <div className="jx-industryChain-companyTags">
                        {company.labels.length > 0
                          ? company.labels.slice(0, 3).map((label) => <Tag key={label} color="orange">{label}</Tag>)
                          : <span>—</span>}
                        {company.labels.length > 3 && <small>+{company.labels.length - 3}</small>}
                      </div>
                      <div className="jx-industryChain-companyRegion" title={company.belong_area || ''}>
                        {company.belong_area || [company.province, company.city].filter(Boolean).join('') || '—'}
                      </div>
                    </a>
                  ))}
                </div>
              )}
            </div>

            {companies && companies.pagination.total_pages > 1 && (
              <footer className="jx-industryChain-companyPagination">
                <Pagination
                  current={companies.pagination.page}
                  pageSize={companies.pagination.page_size}
                  total={companies.pagination.total_items}
                  showSizeChanger={false}
                  size="small"
                  onChange={(page) => void loadNodeCompanies(selectedNode, page)}
                />
              </footer>
            )}
          </section>
        )}
      </div>
    </aside>
  );
}
