import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';
import type { WikiGraphData, WikiGraphNode } from '../../types';
import { graphStyleOf, type GraphTypeStyle } from './wikiGraphTheme';

/**
 * 概念图谱可视化：交互式 SVG 力导向图（渲染逻辑移植自 WeKnora WikiBrowser）。
 *
 * 与旧版「一次性布局后静止」不同，这里是常驻力仿真 + 完整交互：
 * - 力仿真带 alpha 衰减动画收敛，斥力用 X 轴一维空间排序把 O(n²) 降到近似 O(n log n)；
 * - 节点可拖拽（拖完钉住）、画布可平移、滚轮朝光标缩放；
 * - 悬停高亮邻域（debounce 防抖，快速扫过不闪烁）；
 * - 单击选中并通知外部开详情抽屉，双击以该节点为中心展开（220ms 区分单双击）；
 * - 标签按缩放级别与连接度分级显隐，放大才看得到全部标签；
 * - 有未加载邻居的节点画虚线「待展开」外环。
 *
 * 不引第三方图库：单屏节点数被 overview/ego 分批取限制在几十到几百量级，
 * 自绘可完全套用 wikiGraphTheme 的类型配色。
 */

interface ConceptGraphProps {
  data: WikiGraphData;
  /** 当前中心节点 slug（ego 模式下不画待展开环） */
  centerSlug?: string;
  /** 详情抽屉正在看的节点，描一圈选中环 */
  selectedSlug?: string;
  /** 单击节点：外部据此打开详情抽屉 */
  onSelectNode?: (node: WikiGraphNode) => void;
  /** 双击节点：外部以此为中心重新展开 */
  onExpandNode?: (node: WikiGraphNode) => void;
  /** 点击空白处：外部清除选中、关抽屉 */
  onClearSelect?: () => void;
  /** 节点配色解析；不给就用知识库那套按页面类型的配色 */
  styleOf?: (type: string) => GraphTypeStyle;
  /** 在边的中点画 edge.label；关系动词是实体关系图谱的主要信息，概念图谱不需要 */
  showEdgeLabels?: boolean;
}

export interface ConceptGraphHandle {
  fitToView: () => void;
}

interface GNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
  slug: string;
  title: string;
  type: string;
  linkCount: number;
  pinned: boolean;
  raw: WikiGraphNode;
}

interface NodeEl {
  g: SVGGElement;
  circle: SVGCircleElement;
  text: SVGTextElement;
  activeRing: SVGCircleElement;
  node: GNode;
}

interface EdgeEl {
  line: SVGLineElement;
  /** 关系名文字，仅 showEdgeLabels 时创建 */
  label?: SVGTextElement;
  /** 关系名在当前高亮态下的目标不透明度；缩得太小时整体压成 0 */
  labelOpacity: number;
  source: string;
  target: string;
}

const EDGE_COLOR = '#C0C4CC';
const EDGE_HL_FALLBACK = '#126DFF';

function nodeRadius(n: GNode): number {
  return Math.max(8, Math.min(24, 8 + Math.log(n.linkCount + 1) * 4));
}

/** 边线两端缩进到圆边界外一点，避免被节点盖住 */
function setEdgePositions(line: SVGLineElement, s: GNode, t: GNode): void {
  const dx = t.x - s.x;
  const dy = t.y - s.y;
  const dist = Math.sqrt(dx * dx + dy * dy) || 1;
  const ux = dx / dist;
  const uy = dy / dist;
  const rS = nodeRadius(s) + 3;
  const rT = nodeRadius(t) + 3;
  line.setAttribute('x1', String(s.x + ux * rS));
  line.setAttribute('y1', String(s.y + uy * rS));
  line.setAttribute('x2', String(t.x - ux * rT));
  line.setAttribute('y2', String(t.y - uy * rT));
}

/** 边线 + 关系名一起摆位；关系名压在中点上方一点，避免盖住线本身 */
function setEdgeGeometry(e: EdgeEl, s: GNode, t: GNode): void {
  setEdgePositions(e.line, s, t);
  if (!e.label) return;
  e.label.setAttribute('x', String((s.x + t.x) / 2));
  e.label.setAttribute('y', String((s.y + t.y) / 2 - 4));
}

export const ConceptGraph = forwardRef<ConceptGraphHandle, ConceptGraphProps>(
  function ConceptGraph(
    {
      data,
      centerSlug,
      selectedSlug,
      onSelectNode,
      onExpandNode,
      onClearSelect,
      styleOf: styleOfProp,
      showEdgeLabels,
    },
    ref,
  ) {
    const containerRef = useRef<HTMLDivElement>(null);
    const styleOf = styleOfProp || graphStyleOf;
    // 关系名同时受「高亮态」和「当前缩放」两套规则约束，合并到一处上色避免互相覆盖
    const edgeLabelsVisibleRef = useRef(true);

    // 图状态跨 render 存活；React 只负责挂容器，SVG 全部命令式维护
    const nodesRef = useRef<GNode[]>([]);
    const nodeMapRef = useRef<Map<string, GNode>>(new Map());
    const nodeElsRef = useRef<NodeEl[]>([]);
    const edgeElsRef = useRef<EdgeEl[]>([]);
    const adjacencyRef = useRef<Map<string, Set<string>>>(new Map());
    const animFrameRef = useRef(0);
    const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const disposersRef = useRef<Array<() => void>>([]);
    const panZoomRef = useRef<{
      getScale: () => number;
      flyTo: (tx: number, ty: number, s?: number, duration?: number) => void;
    } | null>(null);

    // 回调与选中态走 ref，事件监听器里永远读到最新值，不用重建整张图
    const selectedRef = useRef<string | null>(selectedSlug || null);
    const hoveredRef = useRef<string | null>(null);
    const cbRef = useRef({ onSelectNode, onExpandNode, onClearSelect });
    cbRef.current = { onSelectNode, onExpandNode, onClearSelect };

    const paintEdgeLabel = (e: EdgeEl) => {
      if (!e.label) return;
      e.label.style.opacity = String(edgeLabelsVisibleRef.current ? e.labelOpacity : 0);
    };

    const applyHighlight = (slug: string, hoverSlug?: string) => {
      const adjacency = adjacencyRef.current;
      const neighbors = adjacency.get(slug) || new Set<string>();
      const hoverNeighbors = hoverSlug
        ? adjacency.get(hoverSlug) || new Set<string>()
        : new Set<string>();

      for (const { g, circle, activeRing, node } of nodeElsRef.current) {
        const r = nodeRadius(node);
        if (node.slug === slug || (hoverSlug && node.slug === hoverSlug)) {
          circle.setAttribute('r', String(r + 3));
          circle.setAttribute('stroke-width', '3');
          g.style.opacity = '1';
        } else if (
          neighbors.has(node.slug) ||
          (hoverSlug && hoverNeighbors.has(node.slug))
        ) {
          circle.setAttribute('r', String(r));
          circle.setAttribute('stroke-width', '2');
          g.style.opacity = '1';
        } else {
          circle.setAttribute('r', String(r));
          circle.setAttribute('stroke-width', '2');
          g.style.opacity = '0.2';
        }
        activeRing.style.opacity = node.slug === selectedRef.current ? '1' : '0';
      }

      const focusColorOf = (focusSlug: string) => {
        const el = nodeElsRef.current.find((n) => n.node.slug === focusSlug);
        return el ? styleOf(el.node.type).stroke : EDGE_HL_FALLBACK;
      };
      for (const e of edgeElsRef.current) {
        const onFocus =
          e.source === slug ||
          e.target === slug ||
          (hoverSlug && (e.source === hoverSlug || e.target === hoverSlug));
        if (onFocus) {
          const focusSlug =
            hoverSlug && (e.source === hoverSlug || e.target === hoverSlug)
              ? hoverSlug
              : slug;
          e.line.setAttribute('stroke-opacity', '0.9');
          e.line.setAttribute('stroke-width', '2');
          e.line.setAttribute('stroke', focusColorOf(focusSlug));
          e.labelOpacity = 1;
        } else {
          e.line.setAttribute('stroke-opacity', '0.08');
          e.line.setAttribute('stroke-width', '1');
          e.line.setAttribute('stroke', EDGE_COLOR);
          e.labelOpacity = 0.08;
        }
        paintEdgeLabel(e);
      }
    };

    const clearHighlight = () => {
      if (selectedRef.current) {
        applyHighlight(selectedRef.current);
        return;
      }
      for (const { g, circle, activeRing, node } of nodeElsRef.current) {
        circle.setAttribute('r', String(nodeRadius(node)));
        circle.setAttribute('stroke-width', '2');
        g.style.opacity = '1';
        activeRing.style.opacity = '0';
      }
      for (const e of edgeElsRef.current) {
        e.line.setAttribute('stroke', EDGE_COLOR);
        e.line.setAttribute('stroke-width', '1.2');
        e.line.setAttribute('stroke-opacity', '0.4');
        e.labelOpacity = 0.85;
        paintEdgeLabel(e);
      }
    };

    const fitToView = () => {
      const container = containerRef.current;
      const pz = panZoomRef.current;
      const nodes = nodesRef.current;
      if (!container || !pz || !nodes.length) return;
      const width = container.clientWidth;
      const height = container.clientHeight;
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      for (const n of nodes) {
        minX = Math.min(minX, n.x);
        minY = Math.min(minY, n.y);
        maxX = Math.max(maxX, n.x);
        maxY = Math.max(maxY, n.y);
      }
      const cx = (minX + maxX) / 2;
      const cy = (minY + maxY) / 2;
      const padding = 60;
      const boxWidth = Math.max(maxX - minX, 100) + padding * 2;
      const boxHeight = Math.max(maxY - minY, 100) + padding * 2;
      const targetScale = Math.max(
        0.2,
        Math.min(2, Math.min(width / boxWidth, height / boxHeight)),
      );
      pz.flyTo(
        width / 2 - cx * targetScale,
        height / 2 - cy * targetScale,
        targetScale,
      );
    };

    useImperativeHandle(ref, () => ({ fitToView }));

    // 主渲染：数据变化时整图重建并重跑力仿真
    useEffect(() => {
      const container = containerRef.current;
      if (!container) return undefined;

      // 清掉上一张图的动画与全局监听
      if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
      if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
      for (const dispose of disposersRef.current) dispose();
      disposersRef.current = [];
      hoveredRef.current = null;

      const graph = data;
      if (!graph || !graph.nodes.length) {
        container.innerHTML = '';
        nodesRef.current = [];
        nodeElsRef.current = [];
        edgeElsRef.current = [];
        return undefined;
      }

      const width = container.clientWidth || 800;
      const height = container.clientHeight || 600;

      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      svg.style.width = '100%';
      svg.style.height = '100%';
      container.innerHTML = '';
      container.appendChild(svg);

      const rootG = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      svg.appendChild(rootG);
      const edgeG = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      rootG.appendChild(edgeG);
      const nodeG = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      rootG.appendChild(nodeG);

      // 邻接表：高亮与「待展开」环都要用
      const adjacency = new Map<string, Set<string>>();
      for (const edge of graph.edges) {
        if (!adjacency.has(edge.source)) adjacency.set(edge.source, new Set());
        if (!adjacency.has(edge.target)) adjacency.set(edge.target, new Set());
        adjacency.get(edge.source)!.add(edge.target);
        adjacency.get(edge.target)!.add(edge.source);
      }
      adjacencyRef.current = adjacency;

      // 初始位置：经典环形布局 + 少量抖动，力仿真从这里收敛
      const nodeMap = new Map<string, GNode>();
      const nodes: GNode[] = graph.nodes.map((n, i) => {
        const angle = (2 * Math.PI * i) / graph.nodes.length;
        const r = Math.min(width, height) * 0.35;
        const node: GNode = {
          x: width / 2 + r * Math.cos(angle) + (Math.random() - 0.5) * 50,
          y: height / 2 + r * Math.sin(angle) + (Math.random() - 0.5) * 50,
          vx: 0,
          vy: 0,
          slug: n.slug,
          title: n.title,
          type: n.page_type,
          linkCount: n.link_count || 0,
          pinned: false,
          raw: n,
        };
        nodeMap.set(n.slug, node);
        return node;
      });
      nodesRef.current = nodes;
      nodeMapRef.current = nodeMap;

      // 边（后端已去重为无向边）
      const edgeEls: EdgeEl[] = [];
      for (const edge of graph.edges) {
        if (!nodeMap.has(edge.source) || !nodeMap.has(edge.target)) continue;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('stroke', EDGE_COLOR);
        line.setAttribute('stroke-width', '1.2');
        line.setAttribute('stroke-opacity', '0.4');
        line.style.transition = 'stroke 0.2s, stroke-width 0.2s, stroke-opacity 0.2s';
        edgeG.appendChild(line);
        let label: SVGTextElement | undefined;
        if (showEdgeLabels && edge.label) {
          label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          label.setAttribute('text-anchor', 'middle');
          label.setAttribute('class', 'jx-wikiGraphEdgeLabel');
          label.setAttribute('pointer-events', 'none');
          label.style.transition = 'opacity 0.2s';
          label.textContent =
            edge.label.length > 10 ? `${edge.label.substring(0, 10)}…` : edge.label;
          edgeG.appendChild(label);
        }
        edgeEls.push({
          line,
          label,
          labelOpacity: 0.85,
          source: edge.source,
          target: edge.target,
        });
      }
      edgeElsRef.current = edgeEls;

      const nodeEls: NodeEl[] = [];
      const isEgo = graph.meta?.mode === 'ego';

      for (const n of nodes) {
        const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.style.cursor = 'pointer';
        g.style.transition = 'opacity 0.2s';
        const r = nodeRadius(n);
        const style = styleOf(n.type);

        // 「待展开」虚线环：库内连接度 > 当前画布可见邻居数，说明还有邻居没拉进来。
        // ego 中心已拿到全部可达邻居，剩余差值是死链/被过滤页，不画以免误导。
        const visibleNeighbors = adjacency.get(n.slug)?.size ?? 0;
        const hiddenNeighbors = Math.max(0, n.linkCount - visibleNeighbors);
        const isEgoCenter = isEgo && centerSlug === n.slug;
        if (hiddenNeighbors > 0 && !isEgoCenter) {
          const expansionRing = document.createElementNS(
            'http://www.w3.org/2000/svg',
            'circle',
          );
          expansionRing.setAttribute('r', String(r + 3));
          expansionRing.setAttribute('fill', 'none');
          expansionRing.setAttribute('stroke', style.stroke);
          expansionRing.setAttribute('stroke-width', '1.5');
          expansionRing.setAttribute('stroke-dasharray', '3 3');
          expansionRing.setAttribute('pointer-events', 'none');
          expansionRing.style.opacity = '0.55';
          g.appendChild(expansionRing);
        }

        // 选中态外环
        const activeRing = document.createElementNS(
          'http://www.w3.org/2000/svg',
          'circle',
        );
        activeRing.setAttribute('r', String(r + 5));
        activeRing.setAttribute('fill', 'none');
        activeRing.setAttribute('stroke', style.stroke);
        activeRing.setAttribute('stroke-width', '2');
        activeRing.style.opacity = '0';
        activeRing.style.transition = 'opacity 0.2s';
        g.appendChild(activeRing);

        const circle = document.createElementNS(
          'http://www.w3.org/2000/svg',
          'circle',
        );
        circle.setAttribute('r', String(r));
        circle.setAttribute('fill', style.stroke);
        circle.setAttribute('stroke', '#fff');
        circle.setAttribute('stroke-width', '2');
        circle.style.transition = 'r 0.2s, stroke-width 0.2s, opacity 0.2s';
        g.appendChild(circle);

        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('text-anchor', 'middle');
        text.setAttribute('dy', String(r + 14));
        text.setAttribute('class', 'jx-wikiGraphNodeLabel');
        text.setAttribute('pointer-events', 'none');
        text.style.transition = 'opacity 0.2s';
        text.textContent =
          n.title.length > 14 ? `${n.title.substring(0, 14)}…` : n.title;
        g.appendChild(text);

        // 悬停高亮：leave 侧 debounce，指针在相邻节点间快速滑动不闪整图
        g.addEventListener('mouseenter', () => {
          if (hoverTimerRef.current) {
            clearTimeout(hoverTimerRef.current);
            hoverTimerRef.current = null;
          }
          const selected = selectedRef.current;
          if (!selected) {
            if (hoveredRef.current === n.slug) return;
            hoveredRef.current = n.slug;
            applyHighlight(n.slug);
          } else if (selected !== n.slug) {
            if (hoveredRef.current === n.slug) return;
            hoveredRef.current = n.slug;
            applyHighlight(selected, n.slug);
          }
        });
        g.addEventListener('mouseleave', () => {
          if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
          hoverTimerRef.current = setTimeout(() => {
            hoverTimerRef.current = null;
            hoveredRef.current = null;
            if (!selectedRef.current) clearHighlight();
            else applyHighlight(selectedRef.current);
          }, 60);
        });

        // 单击选中开抽屉 / 双击展开：220ms 计时区分，双击到达则取消单击
        let pendingClick: ReturnType<typeof setTimeout> | null = null;
        g.addEventListener('click', (e) => {
          e.stopPropagation();
          if (pendingClick) clearTimeout(pendingClick);
          pendingClick = setTimeout(() => {
            pendingClick = null;
            selectedRef.current = n.slug;
            applyHighlight(n.slug);
            // 自动平移把节点带到视野中心，右侧给抽屉留出空间
            const pz = panZoomRef.current;
            const box = containerRef.current;
            if (pz && box) {
              pz.flyTo(
                box.clientWidth / 2 - n.x * pz.getScale() - 200,
                box.clientHeight / 2 - n.y * pz.getScale(),
              );
            }
            cbRef.current.onSelectNode?.(n.raw);
          }, 220);
        });
        g.addEventListener('dblclick', (e) => {
          e.stopPropagation();
          if (pendingClick) {
            clearTimeout(pendingClick);
            pendingClick = null;
          }
          cbRef.current.onExpandNode?.(n.raw);
        });

        // 拖拽：拖动中直接改坐标并钉住，松手后保持在用户放的位置
        const onDragStart = (e: MouseEvent) => {
          if (e.button !== 0) return;
          e.stopPropagation();
          n.pinned = true;
          const getPoint = (ev: MouseEvent) => {
            const pt = svg.createSVGPoint();
            pt.x = ev.clientX;
            pt.y = ev.clientY;
            const ctm = rootG.getCTM()?.inverse();
            return ctm ? pt.matrixTransform(ctm) : { x: ev.clientX, y: ev.clientY };
          };
          const p0 = getPoint(e);
          const startX = p0.x - n.x;
          const startY = p0.y - n.y;
          circle.setAttribute('stroke', style.stroke);
          circle.setAttribute('stroke-width', '3');
          const onMove = (ev: MouseEvent) => {
            const p = getPoint(ev);
            n.x = p.x - startX;
            n.y = p.y - startY;
            n.vx = 0;
            n.vy = 0;
            g.setAttribute('transform', `translate(${n.x},${n.y})`);
            for (const edge of edgeEls) {
              if (edge.source !== n.slug && edge.target !== n.slug) continue;
              const sn = nodeMap.get(edge.source);
              const tn = nodeMap.get(edge.target);
              if (sn && tn) setEdgeGeometry(edge, sn, tn);
            }
          };
          const onEnd = () => {
            circle.setAttribute('stroke', '#fff');
            circle.setAttribute('stroke-width', '2');
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onEnd);
          };
          window.addEventListener('mousemove', onMove);
          window.addEventListener('mouseup', onEnd);
        };
        g.addEventListener('mousedown', onDragStart);

        nodeG.appendChild(g);
        nodeEls.push({ g, circle, text, activeRing, node: n });
      }
      nodeElsRef.current = nodeEls;

      // ── 平移 & 缩放 ──
      let scale = 1;
      let translateX = 0;
      let translateY = 0;

      const updateLabelsVisibility = () => {
        // 缩得越小标签越挤；按连接度分级——枢纽节点的标签更早出现
        for (const { text, node } of nodeEls) {
          if (
            node.slug === selectedRef.current ||
            node.slug === hoveredRef.current
          ) {
            text.style.opacity = '1';
            continue;
          }
          let threshold = 0.5;
          if (node.linkCount > 10) threshold = 0.2;
          else if (node.linkCount > 5) threshold = 0.35;
          else if (node.linkCount > 2) threshold = 0.45;
          text.style.opacity = scale < threshold ? '0' : '1';
        }
        // 关系名比节点名更密，缩到半屏以下就整体收起，否则线上全是糊字
        edgeLabelsVisibleRef.current = scale >= 0.6;
        for (const e of edgeEls) paintEdgeLabel(e);
      };

      const applyTransform = () => {
        rootG.setAttribute(
          'transform',
          `translate(${translateX},${translateY}) scale(${scale})`,
        );
        updateLabelsVisibility();
      };

      let flyAnimId = 0;
      panZoomRef.current = {
        getScale: () => scale,
        flyTo: (tx, ty, s, duration = 400) => {
          cancelAnimationFrame(flyAnimId);
          const startX = translateX;
          const startY = translateY;
          const startScale = scale;
          const targetScale = s || scale;
          const startTime = performance.now();
          const animate = (time: number) => {
            let progress = (time - startTime) / duration;
            if (progress > 1) progress = 1;
            const ease = 1 - Math.pow(1 - progress, 3);
            translateX = startX + (tx - startX) * ease;
            translateY = startY + (ty - startY) * ease;
            scale = startScale + (targetScale - startScale) * ease;
            applyTransform();
            if (progress < 1) flyAnimId = requestAnimationFrame(animate);
          };
          flyAnimId = requestAnimationFrame(animate);
        },
      };

      const onWheel = (e: WheelEvent) => {
        e.preventDefault();
        const zoomFactor = e.deltaY > 0 ? 0.92 : 1.08;
        const newScale = Math.max(0.2, Math.min(5, scale * zoomFactor));
        const rect = svg.getBoundingClientRect();
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        translateX = cx - (cx - translateX) * (newScale / scale);
        translateY = cy - (cy - translateY) * (newScale / scale);
        scale = newScale;
        applyTransform();
      };
      svg.addEventListener('wheel', onWheel, { passive: false });

      let panning = false;
      let panStartX = 0;
      let panStartY = 0;
      let downX = 0;
      let downY = 0;
      const isBackground = (target: EventTarget | null) =>
        (target as Element)?.tagName?.toLowerCase() === 'svg';
      const onPanStart = (e: MouseEvent) => {
        if (e.button !== 0 || !isBackground(e.target)) return;
        panning = true;
        panStartX = e.clientX - translateX;
        panStartY = e.clientY - translateY;
        downX = e.clientX;
        downY = e.clientY;
        svg.style.cursor = 'grabbing';
      };
      const onPanMove = (e: MouseEvent) => {
        if (!panning) return;
        translateX = e.clientX - panStartX;
        translateY = e.clientY - panStartY;
        applyTransform();
      };
      const onPanEnd = (e: MouseEvent) => {
        if (!panning) return;
        panning = false;
        svg.style.cursor = 'default';
        // 几乎没动 = 点击空白：清选中、通知外部关抽屉
        if (
          Math.abs(e.clientX - downX) < 5 &&
          Math.abs(e.clientY - downY) < 5 &&
          isBackground(e.target)
        ) {
          selectedRef.current = null;
          clearHighlight();
          cbRef.current.onClearSelect?.();
        }
      };
      svg.addEventListener('mousedown', onPanStart);
      window.addEventListener('mousemove', onPanMove);
      window.addEventListener('mouseup', onPanEnd);
      disposersRef.current.push(() => {
        svg.removeEventListener('wheel', onWheel);
        svg.removeEventListener('mousedown', onPanStart);
        window.removeEventListener('mousemove', onPanMove);
        window.removeEventListener('mouseup', onPanEnd);
        cancelAnimationFrame(flyAnimId);
      });

      // ── 力仿真（alpha 衰减，收敛即停帧） ──
      let alpha = 1.0;
      const tick = () => {
        alpha *= 0.985;
        if (alpha < 0.02) {
          animFrameRef.current = 0;
          return;
        }

        // 斥力：按 X 排序 + 300px 截断，把 O(n²) 剪成近邻对
        const sorted = [...nodes].sort((a, b) => a.x - b.x);
        const MAX_DIST = 300;
        const MAX_DIST_SQ = MAX_DIST * MAX_DIST;
        for (let i = 0; i < sorted.length; i += 1) {
          const n1 = sorted[i];
          for (let j = i + 1; j < sorted.length; j += 1) {
            const n2 = sorted[j];
            const dx = n2.x - n1.x;
            if (dx > MAX_DIST) break;
            const dy = n2.y - n1.y;
            if (Math.abs(dy) > MAX_DIST) continue;
            const distSq = dx * dx + dy * dy;
            if (distSq > MAX_DIST_SQ) continue;
            const dist = Math.sqrt(distSq) || 1;
            const force = ((200 * alpha) / Math.max(distSq, 100)) * 60;
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            if (!n1.pinned) {
              n1.vx -= fx;
              n1.vy -= fy;
            }
            if (!n2.pinned) {
              n2.vx += fx;
              n2.vy += fy;
            }
          }
        }

        // 弹簧：有边的相互吸引到目标边长
        for (const edge of edgeEls) {
          const s = nodeMap.get(edge.source);
          const t = nodeMap.get(edge.target);
          if (!s || !t) continue;
          const dx = t.x - s.x;
          const dy = t.y - s.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const force = (dist - 120) * 0.005 * alpha;
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          if (!s.pinned) {
            s.vx += fx;
            s.vy += fy;
          }
          if (!t.pinned) {
            t.vx -= fx;
            t.vy -= fy;
          }
        }

        // 向心力随规模微调，节点越多收得越紧
        const gravity = Math.min(0.01, 0.001 + nodes.length * 0.00002);
        for (const n of nodes) {
          if (n.pinned) continue;
          n.vx += (width / 2 - n.x) * gravity * alpha;
          n.vy += (height / 2 - n.y) * gravity * alpha;
        }

        for (const n of nodes) {
          if (n.pinned) continue;
          n.vx *= 0.6;
          n.vy *= 0.6;
          const v = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
          if (v > 20) {
            n.vx = (n.vx / v) * 20;
            n.vy = (n.vy / v) * 20;
          }
          n.x += n.vx;
          n.y += n.vy;
        }

        for (const { g, node } of nodeEls) {
          g.setAttribute('transform', `translate(${node.x},${node.y})`);
        }
        for (const e of edgeEls) {
          const s = nodeMap.get(e.source);
          const t = nodeMap.get(e.target);
          if (s && t) setEdgeGeometry(e, s, t);
        }

        animFrameRef.current = requestAnimationFrame(tick);
      };

      for (const { g, node } of nodeEls) {
        g.setAttribute('transform', `translate(${node.x},${node.y})`);
      }
      for (const e of edgeEls) {
        const s = nodeMap.get(e.source);
        const t = nodeMap.get(e.target);
        if (s && t) setEdgeGeometry(e, s, t);
      }
      applyTransform();
      if (selectedRef.current && nodeMap.has(selectedRef.current)) {
        applyHighlight(selectedRef.current);
      }
      animFrameRef.current = requestAnimationFrame(tick);

      return () => {
        if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
        if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
        for (const dispose of disposersRef.current) dispose();
        disposersRef.current = [];
      };
      // centerSlug 只随 data 一起变（ego 重新拉图），不单独触发重建
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [data]);

    // 外部改选中（点抽屉里的邻居、关抽屉）时同步高亮，不重建图
    useEffect(() => {
      const next = selectedSlug || null;
      if (selectedRef.current === next) return;
      selectedRef.current = next;
      if (!nodeElsRef.current.length) return;
      if (next && nodeMapRef.current.has(next)) {
        applyHighlight(next);
        const n = nodeMapRef.current.get(next)!;
        const pz = panZoomRef.current;
        const box = containerRef.current;
        if (pz && box) {
          pz.flyTo(
            box.clientWidth / 2 - n.x * pz.getScale() - 200,
            box.clientHeight / 2 - n.y * pz.getScale(),
          );
        }
      } else {
        clearHighlight();
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [selectedSlug]);

    return <div ref={containerRef} className="jx-wikiGraphCanvas" role="img" aria-label="概念图谱" />;
  },
);

export default ConceptGraph;
