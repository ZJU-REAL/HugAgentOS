import React, { useCallback, useMemo, useState } from 'react';
import type { WikiGraphData, WikiGraphNode } from '../../types';
import { graphStyleOf as styleOf } from './wikiGraphTheme';

/**
 * 概念图谱可视化：自研 SVG 力导向布局。
 *
 * 不引第三方图库有两个务实理由：一是全库有 2000+ 节点、上万条边，本来就只能
 * 分批取（overview 取枢纽 / ego 取邻域），单屏节点数被限制在几十个量级，通用
 * 图库的能力用不上；二是自绘能完全套用设计系统的色板与圆角，不用跟库的默认
 * 皮肤打架。
 *
 * 布局是经典的三力模型 —— 斥力（节点互斥）、弹簧（有边的相互吸引）、向心力
 * （防止子图飘走），跑固定步数后停住，不做常驻动画以免持续占 CPU。
 */

interface ConceptGraphProps {
  data: WikiGraphData;
  /** 当前中心节点 slug（ego 模式下高亮） */
  centerSlug?: string;
  /** 右侧详情面板正在看的节点，描一圈选中环 */
  selectedSlug?: string;
  /** 单击节点：外部据此打开详情面板 */
  onSelectNode?: (node: WikiGraphNode) => void;
  height?: number;
}

interface LayoutNode extends WikiGraphNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
  radius: number;
}

const WIDTH = 900;
const DEFAULT_HEIGHT = 580;

/**
 * 画布高度随节点数增长。
 *
 * 视口宽度是固定的，但节点是有半径的实心圆——节点一多，固定高度里根本摆不下，
 * 碰撞分离只能把它们挤成一块密不透风的饼，标签全糊。让画布跟着长高（外层按
 * 比例缩放），密度才稳定在可读范围。
 */
function canvasHeightFor(nodeCount: number): number {
  if (nodeCount <= 40) return DEFAULT_HEIGHT;
  return Math.min(1280, Math.round(DEFAULT_HEIGHT + (nodeCount - 40) * 8));
}


/** 连接度 → 半径：sqrt 压缩，避免枢纽节点大到盖住整张图 */
function radiusFor(linkCount: number | undefined): number {
  const n = Math.max(0, linkCount || 0);
  return Math.min(30, 11 + Math.sqrt(n) * 1.7);
}

/**
 * 力导向布局：确定性初始化 + 固定迭代步数。
 *
 * 初始位置用黄金角螺旋而不是随机数——同一份数据每次渲染布局一致，用户切来切去
 * 不会看到图整个跳一下。
 */
function computeLayout(data: WikiGraphData, height: number): { nodes: LayoutNode[]; edges: Array<[LayoutNode, LayoutNode]> } {
  const cx = WIDTH / 2;
  const cy = height / 2;
  const count = data.nodes.length;
  if (!count) return { nodes: [], edges: [] };

  const golden = Math.PI * (3 - Math.sqrt(5));
  const spread = Math.min(WIDTH, height) * 0.38;

  const nodes: LayoutNode[] = data.nodes.map((node, i) => {
    const angle = i * golden;
    const r = spread * Math.sqrt((i + 0.5) / count);
    return {
      ...node,
      x: cx + Math.cos(angle) * r,
      y: cy + Math.sin(angle) * r,
      vx: 0,
      vy: 0,
      radius: radiusFor(node.link_count),
    };
  });

  const index = new Map(nodes.map((n) => [n.slug, n]));
  const edges: Array<[LayoutNode, LayoutNode]> = [];
  for (const edge of data.edges) {
    const a = index.get(edge.source);
    const b = index.get(edge.target);
    if (a && b && a !== b) edges.push([a, b]);
  }

  // 每个节点的度数：枢纽节点动辄连着几十条边，若不按度数归一化，几十根弹簧会把
  // 它死死拽向质心，整张图收缩成一团糊。除以 sqrt(degree) 让每个节点受到的合力
  // 保持同一量级。
  const degree = new Map<LayoutNode, number>();
  for (const node of nodes) degree.set(node, 0);
  for (const [a, b] of edges) {
    degree.set(a, (degree.get(a) || 0) + 1);
    degree.set(b, (degree.get(b) || 0) + 1);
  }
  const springScale = new Map<LayoutNode, number>();
  for (const node of nodes) {
    springScale.set(node, 1 / Math.sqrt(Math.max(1, degree.get(node) || 1)));
  }

  const ITERATIONS = 380;
  const REPULSION = 14000;
  const SPRING = 0.02;
  const SPRING_LENGTH = 128;
  const CENTERING = 0.005;
  // 圆之间至少留出的空隙，保证标签有落脚处、点击也不会被邻居抢走
  const COLLISION_PADDING = 16;

  for (let step = 0; step < ITERATIONS; step += 1) {
    // 阻尼随迭代衰减：先快速铺开，后期慢慢收敛，避免最终帧还在抖
    const damping = 0.86 - (step / ITERATIONS) * 0.3;

    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let distSq = dx * dx + dy * dy;
        if (distSq < 0.01) {
          // 完全重合时给一个确定性的微小偏移，避免除零又不引入随机
          dx = (i - j) * 0.01 + 0.01;
          dy = 0.01;
          distSq = dx * dx + dy * dy;
        }
        const dist = Math.sqrt(distSq);
        // 半径参与斥力：大圆需要更大的私人空间，否则枢纽节点总把邻居压在身下
        const force = (REPULSION * ((a.radius + b.radius) / 30)) / distSq;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    for (const [a, b] of edges) {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (dist - SPRING_LENGTH) * SPRING;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      const sa = springScale.get(a) || 1;
      const sb = springScale.get(b) || 1;
      a.vx += fx * sa;
      a.vy += fy * sa;
      b.vx -= fx * sb;
      b.vy -= fy * sb;
    }

    for (const node of nodes) {
      node.vx += (cx - node.x) * CENTERING;
      node.vy += (cy - node.y) * CENTERING;
      node.vx *= damping;
      node.vy *= damping;
      node.x += Math.max(-24, Math.min(24, node.vx));
      node.y += Math.max(-24, Math.min(24, node.vy));
    }

    separate(nodes, COLLISION_PADDING);
  }

  // 收敛后整体缩放平移到画布内，保证不同规模的子图都占满视野
  fitToCanvas(nodes, height);

  // 缩放只作用于坐标、不作用于半径，所以缩小之后原本分开的圆会重新叠上——
  // 必须在 fit 之后再分离一轮，并把结果夹回画布内（夹取幅度很小，不会再叠回去）
  for (let i = 0; i < 90; i += 1) {
    separate(nodes, COLLISION_PADDING);
    clampToCanvas(nodes, height);
  }

  return { nodes, edges };
}

/** 按几何把重叠的一对节点顶开——位置约束比力更可靠 */
function separate(nodes: LayoutNode[], padding: number): void {
  for (let i = 0; i < nodes.length; i += 1) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = nodes[j];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const minDist = a.radius + b.radius + padding;
      const distSq = dx * dx + dy * dy;
      if (distSq >= minDist * minDist) continue;
      const dist = Math.sqrt(distSq) || 0.01;
      const push = (minDist - dist) / 2;
      const ux = (dx / dist) * push;
      const uy = (dy / dist) * push;
      a.x -= ux;
      a.y -= uy;
      b.x += ux;
      b.y += uy;
    }
  }
}

function fitToCanvas(nodes: LayoutNode[], height: number): void {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const n of nodes) {
    minX = Math.min(minX, n.x - n.radius);
    maxX = Math.max(maxX, n.x + n.radius);
    minY = Math.min(minY, n.y - n.radius);
    maxY = Math.max(maxY, n.y + n.radius);
  }
  const pad = 54;
  const scale = Math.min(
    (WIDTH - pad * 2) / Math.max(1, maxX - minX),
    (height - pad * 2) / Math.max(1, maxY - minY),
    1.6,
  );
  const offsetX = WIDTH / 2 - ((minX + maxX) / 2) * scale;
  const offsetY = height / 2 - ((minY + maxY) / 2) * scale;
  for (const n of nodes) {
    n.x = n.x * scale + offsetX;
    n.y = n.y * scale + offsetY;
  }
}

/** 夹回画布内；底部多留一点给节点标签 */
function clampToCanvas(nodes: LayoutNode[], height: number): void {
  for (const n of nodes) {
    n.x = Math.max(n.radius + 6, Math.min(WIDTH - n.radius - 6, n.x));
    n.y = Math.max(n.radius + 6, Math.min(height - n.radius - 20, n.y));
  }
}

function truncate(text: string, max: number): string {
  const value = (text || '').trim();
  return value.length <= max ? value : `${value.slice(0, max)}…`;
}

export function ConceptGraph({
  data,
  centerSlug,
  selectedSlug,
  onSelectNode,
  height,
}: ConceptGraphProps) {
  const [hovered, setHovered] = useState<string | null>(null);

  const canvasHeight = height ?? canvasHeightFor(data.nodes.length);

  // 布局计算是纯 CPU 的 O(n²)×迭代，只在数据真正变化时跑
  const layout = useMemo(() => computeLayout(data, canvasHeight), [data, canvasHeight]);

  // 换了一张图就让 <svg> 重新挂载，入场动画随之重播——比用 state 触发一次额外
  // 渲染更省，也避开了 effect 里同步 setState
  const layoutKey = useMemo(
    () => `${centerSlug || 'overview'}:${layout.nodes.length}:${layout.nodes.map((n) => n.slug).join('|')}`,
    [layout, centerSlug],
  );

  const neighbours = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const edge of data.edges) {
      if (!map.has(edge.source)) map.set(edge.source, new Set());
      if (!map.has(edge.target)) map.set(edge.target, new Set());
      map.get(edge.source)!.add(edge.target);
      map.get(edge.target)!.add(edge.source);
    }
    return map;
  }, [data.edges]);

  /** 悬停时把非相邻节点整体压暗，让局部关系看得清 */
  const isDimmed = useCallback(
    (slug: string) => {
      if (!hovered) return false;
      if (slug === hovered) return false;
      return !(neighbours.get(hovered)?.has(slug) ?? false);
    },
    [hovered, neighbours],
  );

  /**
   * 常显标签的节点集合：几十个标签全部铺开会互相压成一片糊。这里只给关联度最高
   * 的一批 + 中心节点常显，其余节点悬停时才显示（CSS 控制），既保住信息量又干净。
   */
  const labelledSlugs = useMemo(() => {
    const ranked = [...layout.nodes].sort(
      (a, b) => (b.link_count || 0) - (a.link_count || 0),
    );
    const keep = new Set(ranked.slice(0, 14).map((n) => n.slug));
    if (centerSlug) keep.add(centerSlug);
    if (selectedSlug) keep.add(selectedSlug);
    return keep;
  }, [layout.nodes, centerSlug, selectedSlug]);

  if (!layout.nodes.length) {
    return (
      <div className="jx-wikiGraphEmpty">
        <span>暂无可展示的概念关系</span>
      </div>
    );
  }

  return (
    <div className="jx-wikiGraphCanvas">
      <svg
        key={layoutKey}
        viewBox={`0 0 ${WIDTH} ${canvasHeight}`}
        className="jx-wikiGraphSvg"
        role="img"
        aria-label="概念图谱"
      >
        <defs>
          <radialGradient id="jx-wikiGraphGlow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#126DFF" stopOpacity="0.16" />
            <stop offset="100%" stopColor="#126DFF" stopOpacity="0" />
          </radialGradient>
        </defs>

        <g className="jx-wikiGraphEdges">
          {layout.edges.map(([a, b], i) => {
            const dim = isDimmed(a.slug) || isDimmed(b.slug);
            const active = hovered === a.slug || hovered === b.slug;
            return (
              <line
                key={`${a.slug}-${b.slug}-${i}`}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                className={`jx-wikiGraphEdge${active ? ' is-active' : ''}${dim ? ' is-dimmed' : ''}`}
              />
            );
          })}
        </g>

        <g className="jx-wikiGraphNodes">
          {layout.nodes.map((node, i) => {
            const style = styleOf(node.page_type);
            const isCenter = node.slug === centerSlug;
            const isSelected = node.slug === selectedSlug;
            const dim = isDimmed(node.slug);
            return (
              <g
                key={node.slug}
                className={`jx-wikiGraphNode${isCenter ? ' is-center' : ''}${
                  isSelected ? ' is-selected' : ''
                }${dim ? ' is-dimmed' : ''}`}
                style={{ '--node-delay': `${Math.min(i * 12, 420)}ms` } as React.CSSProperties}
                transform={`translate(${node.x}, ${node.y})`}
                onMouseEnter={() => setHovered(node.slug)}
                onMouseLeave={() => setHovered(null)}
                onClick={() => onSelectNode?.(node)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onSelectNode?.(node);
                  }
                }}
              >
                {isCenter && <circle r={node.radius + 16} fill="url(#jx-wikiGraphGlow)" />}
                {isSelected && (
                  <circle
                    className="jx-wikiGraphSelectRing"
                    r={node.radius + 6}
                    fill="none"
                    stroke={style.stroke}
                  />
                )}
                <circle
                  r={node.radius}
                  fill={style.fill}
                  stroke={style.stroke}
                  strokeWidth={isCenter ? 2.5 : 1.5}
                  className="jx-wikiGraphNodeCircle"
                />
                <text
                  className={`jx-wikiGraphNodeLabel${labelledSlugs.has(node.slug) ? '' : ' is-quiet'}`}
                  y={node.radius + 14}
                  textAnchor="middle"
                >
                  {truncate(node.title, 12)}
                </text>
                <title>{`${node.title}（${style.label}，${node.link_count || 0} 条关联）`}</title>
              </g>
            );
          })}
        </g>
      </svg>

    </div>
  );
}

export default ConceptGraph;
