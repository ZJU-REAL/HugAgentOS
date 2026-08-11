import {
  ApartmentOutlined,
  CloseOutlined,
  CompressOutlined,
  ExpandOutlined,
  MinusOutlined,
  PlusOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

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

const NODE_HEIGHT = 44;
const COLUMN_GAP = 260;
const ROW_GAP = 72;
const WORLD_PADDING = 48;
const MIN_SCALE = 0.28;
const MAX_SCALE = 1.8;
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

export function IndustryChainCanvas() {
  const target = useCanvasStore((state) => state.industryChainTarget);
  const closeCanvas = useCanvasStore((state) => state.closeCanvas);
  const viewportRef = useRef<HTMLDivElement>(null);
  const didInitialPositionRef = useRef(false);
  const panRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  // null means the user has not changed expansion yet, so the tree can derive
  // its default collapsed branches as soon as the asynchronous result arrives.
  const [collapsed, setCollapsed] = useState<Set<string> | null>(null);
  const [view, setView] = useState<ViewTransform>({ x: 24, y: 24, scale: 1 });
  const [dragging, setDragging] = useState(false);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });

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

  const fitToView = useCallback(() => {
    if (!layout || !viewportRef.current) return;
    const rect = viewportRef.current.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const padding = 56;
    const scale = clampScale(Math.min(
      (rect.width - padding * 2) / layout.width,
      (rect.height - padding * 2) / layout.height,
      1,
    ));
    setView({
      scale,
      x: (rect.width - layout.width * scale) / 2,
      y: (rect.height - layout.height * scale) / 2,
    });
  }, [layout]);

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
      if (next.has(node.id)) next.delete(node.id);
      else next.add(node.id);
      return next;
    });
  }, [defaultCollapsed]);

  const collapseAll = useCallback(() => {
    if (!model.tree) return;
    setCollapsed(new Set(allBranchIds(model.tree).filter((id) => id !== model.tree?.id)));
  }, [model.tree]);

  const expandAll = useCallback(() => setCollapsed(new Set()), []);

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

      <div className="jx-industryChain-body">
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
              className="jx-industryChain-world"
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
                const isCollapsed = effectiveCollapsed.has(positioned.node.id);
                return (
                  <div
                    key={positioned.node.id}
                    className={`jx-industryChain-node jx-industryChain-node--depth${Math.min(positioned.depth, 2)}`}
                    style={{
                      left: positioned.x,
                      top: positioned.y,
                      width: positioned.width,
                      minHeight: positioned.height,
                    }}
                    role="treeitem"
                    aria-level={positioned.depth + 1}
                    aria-expanded={branch ? !isCollapsed : undefined}
                    title={positioned.node.label}
                  >
                    <span className="jx-industryChain-nodeAccent" aria-hidden="true" />
                    <span className="jx-industryChain-nodeLabel">{positioned.node.label}</span>
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
      </div>
    </aside>
  );
}
