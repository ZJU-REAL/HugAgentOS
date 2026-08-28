import { memo, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import CitationHtmlBlock, { type CitationMarker } from './CitationHtmlBlock';
import { mdToHtml, ensureKatexLoaded, hasLatex, hasMermaid } from '../../utils/markdown';
import { MermaidBlock, extractMermaidCharts } from '../chat/MermaidBlock';
import type { CitationItem } from '../../types';
import { citationMarkerParts, citationMarkerRe } from '../../utils/citations';
import { patchHtml } from '../../utils/domPatch';

// 引用标记正则与解析都取自 utils/citations 的唯一真源 —— 这里曾经维护过一份副本，
// 两边一旦不同步就会出现「一处认得、另一处不认」的漏标记。
const markerRe = citationMarkerRe;
// 流式输出中被截断的尾部半截标记（渲染前剪掉，防闪烁）
const PARTIAL_TAIL_RE = /\[ref:[^\]]*$|\[[^[\]]*\]\(\s*cite:[^)]*$|\[\[\s*e?\d*\]?$/i;
// 这些锚文本视为"纯句末标注"→ 渲染成紧凑角标而非文字链接。
// 模型并不总按提示词写「来源」，实际还见过出处/参考/引用/详见/source/ref 等同义写法，
// 漏一个就会在正文里留下一个孤零零的「出处」二字。
const BADGE_LABELS = new Set([
  '', '#', '来源', '出处', '参考', '引用', '详见', '来源链接',
  'source', 'sources', 'ref', 'link',
]);

function markerParts(m: RegExpExecArray | RegExpMatchArray): { id: string; label: string } {
  const { id, label } = citationMarkerParts(m);
  return { id, label: BADGE_LABELS.has(label.toLowerCase()) ? '' : label };
}

const EMPTY_MERMAID: Array<{ element: HTMLElement; chart: string }> = [];

/** 正文容器：首帧交给 React，之后每次增量走 patchHtml 就地改（__html 冻住不变）。 */
function PatchedHtml({ html }: { html: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const appliedHtml = useRef(html);
  const [initialProp] = useState(() => ({ __html: html }));
  useEffect(() => {
    const el = ref.current;
    if (!el || appliedHtml.current === html) return;
    appliedHtml.current = html;
    patchHtml(el, html);
  }, [html]);
  return <div ref={ref} dangerouslySetInnerHTML={initialProp} />;
}

function sameCitation(a: CitationItem, b: CitationItem): boolean {
  return a.id === b.id
    && a.title === b.title
    && a.snippet === b.snippet
    && a.url === b.url
    && a.source_type === b.source_type
    && a.tool_id === b.tool_id
    && a.tool_name === b.tool_name;
}

function sameCitations(prev: CitationItem[], next: CitationItem[]): boolean {
  if (prev === next) return true;
  if (prev.length !== next.length) return false;
  for (let i = 0; i < prev.length; i += 1) {
    if (!sameCitation(prev[i], next[i])) return false;
  }
  return true;
}

/**
 * CitationMarkdownBlock: top-level component for rendering text with inline citations.
 * Streaming-aware: strips/renders markers based on citation availability.
 * Supports Mermaid diagrams and LaTeX math rendering.
 */
const CitationMarkdownBlock = memo(function CitationMarkdownBlock({
  text,
  isMarkdown,
  citations,
  messageIsStreaming,
  onCitationAction,
  className,
}: {
  text: string;
  isMarkdown: boolean;
  citations: CitationItem[];
  messageIsStreaming?: boolean;
  onCitationAction?: (citation: CitationItem) => void;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement | HTMLSpanElement | null>(null);
  const [mermaidCharts, setMermaidCharts] = useState(EMPTY_MERMAID);
  const [, setLatexReady] = useState(false);

  // Strip streaming-truncated tails, then resolve unmatched markers:
  // 命中 citations 的保留原样；未命中的旧格式/裸标整体剥掉，未命中的
  // [锚文本](cite:eN) 保留锚文本（退化为普通文字，信息不丢）。
  const normalizedText = useMemo(() => {
    const stripped = messageIsStreaming ? text.replace(PARTIAL_TAIL_RE, '') : text;
    return stripped.replace(markerRe(), (match, ...groups) => {
      const { id, label } = markerParts([match, groups[0], groups[1], groups[2], groups[3]] as unknown as RegExpMatchArray);
      if (citations.find(c => c.id === id)) return match;
      return label;
    });
  }, [text, messageIsStreaming, citations]);

  const hasCit = useMemo(
    () => citations.length > 0 && markerRe().test(normalizedText),
    [citations, normalizedText],
  );

  const renderedHtml = useMemo(
    () => isMarkdown ? mdToHtml(normalizedText) : '',
    [normalizedText, isMarkdown],
  );
  const shouldRenderMermaid = !messageIsStreaming
    && isMarkdown
    && hasMermaid(normalizedText);
  const visibleMermaidCharts = shouldRenderMermaid ? mermaidCharts : EMPTY_MERMAID;

  // Lazy-load KaTeX when LaTeX content detected
  useEffect(() => {
    if (isMarkdown && hasLatex(normalizedText)) {
      ensureKatexLoaded().then((freshlyLoaded) => {
        if (freshlyLoaded) setLatexReady(true);
      });
    }
  }, [normalizedText, isMarkdown]);

  // 图表等消息落定后再渲染一次：流式中途反复挂载 portal 会让图在加载框和成图之间跳。
  useEffect(() => {
    if (!shouldRenderMermaid || !containerRef.current) return;
    // Small delay to let DOM update
    const timer = setTimeout(() => {
      if (!containerRef.current) return;
      const charts = extractMermaidCharts(containerRef.current);
      setMermaidCharts(charts);
    }, 0);
    return () => clearTimeout(timer);
  }, [normalizedText, shouldRenderMermaid]);

  if (!hasCit) {
    if (isMarkdown) {
      return (
        <div className={className} ref={containerRef as RefObject<HTMLDivElement>}>
          <PatchedHtml html={renderedHtml} />
          {visibleMermaidCharts.map((mc, i) =>
            createPortal(<MermaidBlock key={i} chart={mc.chart} />, mc.element)
          )}
        </div>
      );
    }

    return (
      <span className={className} ref={containerRef as RefObject<HTMLSpanElement>}>
        {normalizedText}
        {visibleMermaidCharts.map((mc, i) =>
          createPortal(<MermaidBlock key={i} chart={mc.chart} />, mc.element)
        )}
      </span>
    );
  }

  const markers: CitationMarker[] = [];
  const tokenPrefix = 'JXCITTOKEN';
  const tokenSuffix = 'JXCITEND';
  const withTokens = normalizedText.replace(markerRe(), (match, ...groups) => {
    markers.push(markerParts([match, groups[0], groups[1], groups[2], groups[3]] as unknown as RegExpMatchArray));
    return `${tokenPrefix}${markers.length - 1}${tokenSuffix}`;
  });
  const htmlBase = isMarkdown ? mdToHtml(withTokens) : withTokens;
  const html = htmlBase.replace(
    new RegExp(`${tokenPrefix}(\\d+)${tokenSuffix}`, 'g'),
    (_m: string, idx: string) =>
      `<span data-jxcit="${idx}" style="display:inline;vertical-align:baseline;line-height:1"></span>`
  );

  if (isMarkdown) {
    return (
      <div className={className} ref={containerRef as RefObject<HTMLDivElement>}>
        <CitationHtmlBlock
          html={html}
          markers={markers}
          citations={citations}
          onCitationAction={onCitationAction}
        />
        {visibleMermaidCharts.map((mc, i) =>
          createPortal(<MermaidBlock key={i} chart={mc.chart} />, mc.element)
        )}
      </div>
    );
  }

  return (
    <span className={className} ref={containerRef as RefObject<HTMLSpanElement>}>
      <CitationHtmlBlock
        html={html}
        markers={markers}
        citations={citations}
        onCitationAction={onCitationAction}
      />
      {visibleMermaidCharts.map((mc, i) =>
        createPortal(<MermaidBlock key={i} chart={mc.chart} />, mc.element)
      )}
    </span>
  );
}, (prevProps, nextProps) =>
  prevProps.text === nextProps.text
  && prevProps.isMarkdown === nextProps.isMarkdown
  && prevProps.messageIsStreaming === nextProps.messageIsStreaming
  && prevProps.className === nextProps.className
  && prevProps.onCitationAction === nextProps.onCitationAction
  && sameCitations(prevProps.citations, nextProps.citations)
);

export default CitationMarkdownBlock;
