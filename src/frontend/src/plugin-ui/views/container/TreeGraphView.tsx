/**
 * Hierarchical graph canvas: layered layout, pan/zoom, minimap, expand/collapse
 * and per-node drill-down into a paged record panel.
 *
 * This is a wholesale port of the retired industry-chain canvas — every layout
 * constant, transition and panel behaviour is kept — with the business unpicked
 * into the manifest: the tree fields, the drill action, the panel's columns and
 * wording all come from the plugin's declaration, so any plugin with
 * hierarchical data gets the identical interaction without host code naming it.
 */

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
import type React from 'react';

import { t } from '../../../i18n';
import { fillTemplate, resolveText } from '../../i18n';
import { readArray, readNumber, readRecord, readRecords, readText } from '../../pointer';
import { usableActions, type ViewProps } from '../../ViewProps';
import type { I18nText, ViewAction } from '../../types';

interface TreeNode {
  id: string;
  label: string;
  nodeId: string;
  badge?: number;
  raw: Record<string, unknown>;
  children: TreeNode[];
}

interface PositionedNode {
  node: TreeNode;
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
const DEFAULT_LEVELS = 3;
const MAX_LEVELS = 4;

/** Leaf-badge icons a manifest may name (`options.badge_icon`). */
const BADGE_ICONS: Record<string, React.ReactNode> = {
  team: <TeamOutlined />,
};

function nodeWidth(label: string): number {
  const visualLength = Array.from(label).reduce(
    (length, character) => length + (character.charCodeAt(0) > 0xff ? 1 : 0.58),
    0,
  );
  return Math.max(112, Math.min(224, 54 + visualLength * 15));
}

/** Walk the payload into a tree using the manifest's pointers. */
function buildTree(
  data: unknown,
  map: Record<string, unknown>,
  maxLevels: number,
): TreeNode | null {
  const rootRecord = readRecord(data, map.root);
  if (Object.keys(rootRecord).length === 0) return null;

  const visit = (record: Record<string, unknown>, level: number, path: string): TreeNode | null => {
    const label = readText(record, map.label);
    if (!label) return null;
    const rawChildren = level < maxLevels ? readArray(record, map.children) : [];
    const children = rawChildren
      .filter((child): child is Record<string, unknown> =>
        child !== null && typeof child === 'object' && !Array.isArray(child))
      .map((child, index) => visit(child, level + 1, `${path}.${index}`))
      .filter((child): child is TreeNode => child !== null);
    return {
      id: path,
      label,
      nodeId: readText(record, map.node_id),
      badge: readNumber(record, map.badge),
      raw: record,
      children,
    };
  };

  return visit(rootRecord, 1, 'root');
}

function createTreeLayout(root: TreeNode, collapsed: Set<string>, maxVisibleDepth: number): TreeLayout {
  const nodes: PositionedNode[] = [];
  const edges: PositionedEdge[] = [];
  let nextLeafCenter = WORLD_PADDING + NODE_HEIGHT / 2;
  let maxDepth = 0;

  const visit = (node: TreeNode, depth: number): PositionedNode => {
    maxDepth = Math.max(maxDepth, depth);
    const children = depth >= maxVisibleDepth || collapsed.has(node.id) ? [] : node.children;
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

function allBranchIds(root: TreeNode): string[] {
  const ids: string[] = [];
  const visit = (node: TreeNode) => {
    if (node.children.length > 0) ids.push(node.id);
    node.children.forEach(visit);
  };
  visit(root);
  return ids;
}

function branchIdsFromDepth(root: TreeNode, minimumDepth: number): string[] {
  const ids: string[] = [];
  const visit = (node: TreeNode, depth: number) => {
    if (node.children.length > 0 && depth >= minimumDepth) ids.push(node.id);
    node.children.forEach((child) => visit(child, depth + 1));
  };
  visit(root, 0);
  return ids;
}

function countNodes(node: TreeNode | null): number {
  if (!node) return 0;
  return 1 + node.children.reduce((total, child) => total + countNodes(child), 0);
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

function subtreeBounds(layout: TreeLayout, root: TreeNode): WorldBounds | null {
  const subtreeIds = new Set<string>();
  const visit = (node: TreeNode) => {
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
  return { x: left, y: top, width: right - left, height: bottom - top };
}

/** A drill-panel state text: manifest wording first, host default second. */
function panelText(
  texts: Record<string, I18nText> | undefined,
  key: string,
  fallback: string,
  vars?: Record<string, unknown>,
): string {
  const declared = resolveText(texts?.[key]);
  const text = declared || fallback;
  return vars ? fillTemplate(text, vars) : text;
}

export function TreeGraphView({ data, map, actions, ctx }: ViewProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const companyPanelRef = useRef<HTMLElement>(null);
  const didInitialPositionRef = useRef<TreeNode | null>(null);
  const panRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  const autoViewFrameRef = useRef<number | null>(null);
  const autoViewTimerRef = useRef<number | null>(null);
  const panelTransitionFrameRef = useRef<number | null>(null);
  const viewBeforePanelRef = useRef<ViewTransform | null>(null);
  const pendingLayoutFocusRef = useRef<TreeNode | 'fit-all' | null>(null);
  // null means the user has not changed expansion yet, so the tree can derive
  // its default collapsed branches as soon as the asynchronous result arrives.
  const [collapsed, setCollapsed] = useState<Set<string> | null>(null);
  const [view, setView] = useState<ViewTransform>({ x: 24, y: 24, scale: 1 });
  const [dragging, setDragging] = useState(false);
  const [viewAnimating, setViewAnimating] = useState(false);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });
  const [selectedNode, setSelectedNode] = useState<TreeNode | null>(null);

  const maxLevels = Number(ctx.options?.max_levels ?? MAX_LEVELS) || MAX_LEVELS;
  const defaultLevels = Number(ctx.options?.default_levels ?? DEFAULT_LEVELS) || DEFAULT_LEVELS;
  const maxVisibleDepth = maxLevels - 1;

  const tree = useMemo(() => buildTree(data, map, maxLevels), [data, map, maxLevels]);
  const nodeCount = useMemo(() => countNodes(tree), [tree]);
  const title = readText(data, map.title) || tree?.label || ctx.viewTitle || '';
  const metrics = useMemo(() => {
    // readRecord 在 spec 为 undefined 时会回退成整个 data——未声明 metrics 的
    // 插件不该把顶层数据当指标条渲染，这里显式短路。
    const record = map.metrics ? readRecord(data, map.metrics) : {};
    const out: Array<{ label: string; value: string | number }> = [];
    for (const [label, value] of Object.entries(record)) {
      if (typeof value === 'number' && Number.isFinite(value)) out.push({ label, value });
      else if (typeof value === 'string' && value.trim()) out.push({ label, value: value.trim() });
    }
    return out;
  }, [data, map.metrics]);

  // 页签名先用工具入参占位，结果解析出真正的标题后回写，页签栏才不会停在
  // 画布声明的通用名上。
  const setTitle = ctx.setTitle;
  useEffect(() => {
    if (title) setTitle?.(title);
  }, [title, setTitle]);

  const nodeActions = useMemo(
    () => usableActions(actions, 'node', ctx.toolName),
    [actions, ctx.toolName],
  );
  const drillAction: ViewAction | null = nodeActions[0] ?? null;
  const drillTexts = drillAction?.result?.texts;
  const badgeIcon = BADGE_ICONS[String(ctx.options?.badge_icon ?? '')] ?? null;

  const defaultCollapsed = useMemo(
    () => (tree ? new Set(branchIdsFromDepth(tree, defaultLevels - 1)) : new Set<string>()),
    [tree, defaultLevels],
  );
  const effectiveCollapsed = collapsed ?? defaultCollapsed;
  const layout = useMemo(
    () => (tree ? createTreeLayout(tree, effectiveCollapsed, maxVisibleDepth) : null),
    [effectiveCollapsed, tree, maxVisibleDepth],
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

  const focusSelectedNodeAlongsidePanel = useCallback((
    selectedLeaf: TreeNode,
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

  const focusVisibleSubtree = useCallback((node: TreeNode) => {
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
  }, [tree]);

  // 数据换代时在渲染期复位（React 官方的 adjust-state-during-render 形态，
  // 避免 effect 里 setState 的级联渲染）。
  const [lastTree, setLastTree] = useState<TreeNode | null>(tree);
  if (lastTree !== tree) {
    setLastTree(tree);
    setCollapsed(null);
    setSelectedNode(null);
  }

  // 每棵树只做一次首帧定位；ref 记录已定位的树，换代后自动重新定位。
  useEffect(() => {
    if (!layout || !tree || didInitialPositionRef.current === tree) return undefined;
    didInitialPositionRef.current = tree;
    const frame = window.requestAnimationFrame(focusRoot);
    return () => window.cancelAnimationFrame(frame);
  }, [focusRoot, layout, tree]);

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
    if ((event.target as HTMLElement).closest('button, .jx-pv-chain-node, .jx-pv-chain-minimap')) return;
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

  const toggleNode = useCallback((node: TreeNode) => {
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
    if (!tree) return;
    pendingLayoutFocusRef.current = 'fit-all';
    setCollapsed(new Set(allBranchIds(tree).filter((id) => id !== tree.id)));
  }, [tree]);

  const expandAll = useCallback(() => {
    pendingLayoutFocusRef.current = 'fit-all';
    setCollapsed(new Set());
  }, []);

  const loadNodeRecords = useCallback((node: TreeNode, page: number) => {
    if (!drillAction) return;
    ctx.runAction?.(drillAction, { node: node.raw }, { page });
  }, [ctx, drillAction]);

  const openNodeRecords = useCallback((node: TreeNode) => {
    if (!drillAction) return;
    if (node.children.length > 0) return;
    const panelWasClosed = selectedNode === null;
    setSelectedNode(node);
    loadNodeRecords(node, 1);
    if (panelWasClosed) {
      viewBeforePanelRef.current = view;
      if (panelTransitionFrameRef.current != null) {
        window.cancelAnimationFrame(panelTransitionFrameRef.current);
      }
      panelTransitionFrameRef.current = window.requestAnimationFrame(() => {
        panelTransitionFrameRef.current = null;
        focusSelectedNodeAlongsidePanel(node, view.scale);
      });
    }
  }, [drillAction, focusSelectedNodeAlongsidePanel, loadNodeRecords, selectedNode, view]);

  const closeNodeRecords = useCallback(() => {
    setSelectedNode(null);
    ctx.closeOutcome?.();
    if (panelTransitionFrameRef.current != null) {
      window.cancelAnimationFrame(panelTransitionFrameRef.current);
      panelTransitionFrameRef.current = null;
    }
    const previousView = viewBeforePanelRef.current;
    viewBeforePanelRef.current = null;
    if (previousView) animateToView(previousView);
    else window.requestAnimationFrame(fitToView);
  }, [animateToView, ctx, fitToView]);

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

  if (!tree || !layout) {
    return (
      <div className="jx-canvas-error">
        {String(ctx.options?.empty_text ?? '') || t('未返回可展示的层级结构')}
      </div>
    );
  }

  const minimapViewport = {
    x: Math.max(0, -view.x / view.scale),
    y: Math.max(0, -view.y / view.scale),
    width: Math.min(layout.width, viewportSize.width / view.scale),
    height: Math.min(layout.height, viewportSize.height / view.scale),
  };

  // ── Drill panel (outcome of the node action) ────────────────────────────
  const outcome = ctx.outcome;
  const panelOpen = selectedNode !== null && !!drillAction;
  const rmap = (drillAction?.result?.map ?? {}) as Record<string, unknown>;
  const outcomeData = outcome?.status === 'success' ? outcome.data : null;
  const records = outcomeData ? readRecords(outcomeData, rmap.items) : [];
  const totalItems = outcomeData ? readNumber(outcomeData, rmap.total) : undefined;
  const currentPage = (outcomeData ? readNumber(outcomeData, rmap.page) : undefined) ?? outcome?.page ?? 1;
  const pageSize = (outcomeData ? readNumber(outcomeData, rmap.page_size) : undefined)
    ?? drillAction?.result?.page_size ?? 10;
  const totalPages = (outcomeData ? readNumber(outcomeData, rmap.total_pages) : undefined) ?? 0;
  const columns = (drillAction?.result?.columns ?? []).map((column) => resolveText(column));
  const subSpecs: unknown[] = Array.isArray(rmap.sub) ? rmap.sub : rmap.sub ? [rmap.sub] : [];

  return (
    <div className="jx-pv-chain" aria-label={title || undefined}>
      <header className="jx-pv-chain-header">
        <div className="jx-pv-chain-heading">
          <span className="jx-pv-chain-headingIcon"><ApartmentOutlined /></span>
          <div className="jx-pv-chain-headingText">
            <strong>{title || ctx.viewTitle}</strong>
            <span>{t('{title} · {n} 个节点', { title: ctx.viewTitle || title, n: nodeCount })}</span>
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
        </div>
      </header>

      {metrics.length > 0 && (
        <div className="jx-pv-chain-metrics" aria-label={t('关键指标')}>
          {metrics.map((metric) => (
            <span key={metric.label} className="jx-pv-chain-metric">
              <small>{metric.label}</small>
              <strong>{typeof metric.value === 'number' ? metric.value.toLocaleString() : metric.value}</strong>
            </span>
          ))}
        </div>
      )}

      <div className={`jx-pv-chain-body${panelOpen ? ' has-company-panel' : ''}`}>
        <div
          ref={viewportRef}
          className={`jx-pv-chain-viewport${dragging ? ' is-dragging' : ''}`}
          onWheel={handleWheel}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerUp}
        >
          <div
            className={`jx-pv-chain-world${viewAnimating ? ' is-view-animating' : ''}`}
            style={{
              width: layout.width,
              height: layout.height,
              transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
            }}
            role="tree"
            aria-label={t('{name}层级结构', { name: title })}
          >
            <svg
              className="jx-pv-chain-edges"
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
              const selectableLeaf = !!drillAction && !branch && !!positioned.node.nodeId;
              const isCollapsed = effectiveCollapsed.has(positioned.node.id);
              const isSelected = selectedNode?.id === positioned.node.id;
              return (
                <div
                  key={positioned.node.id}
                  className={`jx-pv-chain-node jx-pv-chain-node--depth${Math.min(positioned.depth, 2)}${branch ? ' is-expandable' : ''}${selectableLeaf ? ' is-selectable' : ''}${isSelected ? ' is-selected' : ''}`}
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
                    else if (selectableLeaf) openNodeRecords(positioned.node);
                  }}
                  onKeyDown={(event) => {
                    if (event.target !== event.currentTarget) return;
                    if ((branch || selectableLeaf) && (event.key === 'Enter' || event.key === ' ')) {
                      event.preventDefault();
                      if (branch) toggleNode(positioned.node);
                      else openNodeRecords(positioned.node);
                    }
                  }}
                  title={positioned.node.label}
                >
                  <span className="jx-pv-chain-nodeAccent" aria-hidden="true" />
                  <span className="jx-pv-chain-nodeLabel">{positioned.node.label}</span>
                  {selectableLeaf && (
                    <span
                      className="jx-pv-chain-nodeCompanies"
                      title={resolveText(drillAction.label, drillAction.id)}
                    >
                      {badgeIcon}
                      {positioned.node.badge ?? ''}
                    </span>
                  )}
                  {branch && (
                    <button
                      className="jx-pv-chain-nodeToggle"
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

          <div className="jx-pv-chain-toolbar" role="toolbar" aria-label={t('图谱操作')}>
            <button onClick={expandAll}><ExpandOutlined />{t('一键展开')}</button>
            <button onClick={collapseAll}><CompressOutlined />{t('一键收起')}</button>
            <span>{Math.round(view.scale * 100)}%</span>
          </div>

          <div className="jx-pv-chain-minimap" title={t('点击缩略图快速定位')}>
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
                  className="jx-pv-chain-minimapEdge"
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
                  className="jx-pv-chain-minimapNode"
                />
              ))}
              <rect
                x={minimapViewport.x}
                y={minimapViewport.y}
                width={minimapViewport.width}
                height={minimapViewport.height}
                className="jx-pv-chain-minimapViewport"
              />
            </svg>
          </div>

          <div className="jx-pv-chain-hint">{t('拖拽移动 · 滚轮缩放')}</div>
        </div>

        {panelOpen && selectedNode && drillAction && (
          <section
            ref={companyPanelRef}
            className="jx-pv-chain-companyPanel"
            aria-label={`${selectedNode.label} · ${resolveText(drillAction.label, drillAction.id)}`}
          >
            <header className="jx-pv-chain-companyHeader">
              <div>
                <span>
                  {badgeIcon}
                  {panelText(drillTexts, 'panel_title', resolveText(drillAction.label, drillAction.id))}
                </span>
                <strong>{selectedNode.label}</strong>
                <small>
                  {totalItems !== undefined
                    ? panelText(drillTexts, 'total', t('共 {n} 条记录'), { n: totalItems })
                    : selectedNode.badge != null
                      ? panelText(drillTexts, 'total', t('共 {n} 条记录'), { n: selectedNode.badge })
                      : panelText(drillTexts, 'pending', t('正在查询…'))}
                </small>
              </div>
              <button onClick={closeNodeRecords} title={t('关闭')} aria-label={t('关闭')}>
                <CloseOutlined />
              </button>
            </header>

            {columns.length > 0 && (
              <div className="jx-pv-chain-companyColumns" aria-hidden="true">
                {columns.map((column) => <span key={column}>{column}</span>)}
              </div>
            )}

            <div className="jx-pv-chain-companyBody" aria-live="polite">
              {outcome?.status === 'loading' ? (
                <div className="jx-pv-chain-companyState">
                  <Spin />{panelText(drillTexts, 'loading', t('正在加载…'))}
                </div>
              ) : outcome?.status === 'error' ? (
                <div className="jx-pv-chain-companyState is-error">
                  <span>{outcome.error || t('加载失败')}</span>
                  <button onClick={() => loadNodeRecords(selectedNode, currentPage)}>
                    {t('重新加载')}
                  </button>
                </div>
              ) : records.length === 0 ? (
                <div className="jx-pv-chain-companyState">
                  <Empty description={panelText(drillTexts, 'empty', t('暂无记录'))} />
                </div>
              ) : (
                <div className="jx-pv-chain-companyList">
                  {records.map((record, index) => {
                    const name = readText(record, rmap.title);
                    const link = readText(record, rmap.link);
                    const subText = subSpecs
                      .map((spec) => readText(record, spec))
                      .filter(Boolean)
                      .join(' · ');
                    const tags = readArray(record, rmap.tags)
                      .map((tag) => String(tag))
                      .filter(Boolean);
                    const region = readText(record, rmap.region);
                    return (
                      <a
                        key={`${name}-${index}`}
                        className="jx-pv-chain-companyRow"
                        href={link || undefined}
                        target={link ? '_blank' : undefined}
                        rel={link ? 'noopener noreferrer' : undefined}
                        aria-label={link ? t('在新窗口查看{name}详情', { name }) : undefined}
                      >
                        <div className="jx-pv-chain-companyName">
                          <span className="jx-pv-chain-companyNameTitle">
                            <strong title={name}>{name}</strong>
                            {link && <ExportOutlined title={t('查看详情')} />}
                          </span>
                          <small>{subText || panelText(drillTexts, 'sub_empty', '—')}</small>
                        </div>
                        <div className="jx-pv-chain-companyTags">
                          {tags.length > 0
                            ? tags.slice(0, 3).map((tag) => <Tag key={tag} color="orange">{tag}</Tag>)
                            : <span>—</span>}
                          {tags.length > 3 && <small>+{tags.length - 3}</small>}
                        </div>
                        <div className="jx-pv-chain-companyRegion" title={region || ''}>
                          {region || '—'}
                        </div>
                      </a>
                    );
                  })}
                </div>
              )}
            </div>

            {totalPages > 1 && (
              <footer className="jx-pv-chain-companyPagination">
                <Pagination
                  current={currentPage}
                  pageSize={pageSize}
                  total={totalItems ?? 0}
                  showSizeChanger={false}
                  size="small"
                  onChange={(page) => loadNodeRecords(selectedNode, page)}
                />
              </footer>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
