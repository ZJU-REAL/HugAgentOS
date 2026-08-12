import { memo, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import CitationHtmlBlock, { type CitationMarker } from './CitationHtmlBlock';
import { mdToHtml, ensureKatexLoaded, hasLatex, hasMermaid } from '../../utils/markdown';
import { MermaidBlock, extractMermaidCharts } from '../chat/MermaidBlock';
import type { CitationItem } from '../../types';

// 引用标记统一正则（三种形态并行识别）：
//   [ref:tool-N]           旧格式（历史消息）        → group 1
//   [锚文本](cite:eN)      证据锚点主格式            → group 2(label) + 3(id)
//   [[eN]]                 obsidian 双链容错          → group 4
// 工厂而非共享常量：带 /g 的正则会把 lastIndex 带到下一次调用，
// 共享一个实例时 test() 与 replace() 会互相污染、漏匹配。
const markerRe = () => /\[ref:([\w]+-\d+)\]|\[([^[\]]*)\]\(cite:(e\d+)\)|\[\[(e\d+)\]\]/g;
// 流式输出中被截断的尾部半截标记（渲染前剪掉，防闪烁）
const PARTIAL_TAIL_RE = /\[ref:[^\]]*$|\[[^[\]]*\]\(cite:[^)]*$|\[\[e?\d*\]?$/;
// 这些锚文本视为"纯句末标注"→ 渲染成紧凑角标而非文字链接
const BADGE_LABELS = new Set(['', '来源', 'source', '#']);

function markerParts(m: RegExpExecArray | RegExpMatchArray): { id: string; label: string } {
  if (m[1]) return { id: m[1], label: '' };
  if (m[3]) {
    const raw = (m[2] || '').trim();
    return { id: m[3], label: BADGE_LABELS.has(raw) ? '' : raw };
  }
  return { id: m[4] || '', label: '' };
}

const EMPTY_MERMAID: Array<{ element: HTMLElement; chart: string }> = [];

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
 *
 * Wrapped with React.memo to prevent unnecessary re-renders that would destroy
 * the browser's active text selection (dangerouslySetInnerHTML DOM nodes get
 * recreated during reconciliation when the parent re-renders).
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

  // Memoize the HTML output so the same string reference is reused across renders,
  // ensuring React skips the DOM update for dangerouslySetInnerHTML.
  const renderedHtml = useMemo(
    () => isMarkdown ? mdToHtml(normalizedText) : '',
    [normalizedText, isMarkdown],
  );
  // Keep the prop object stable when only portal state changes. Otherwise a
  // Mermaid scan followed by setState can make React assign innerHTML again,
  // detaching the freshly discovered portal target before it is used.
  const renderedHtmlProp = useMemo(() => ({ __html: renderedHtml }), [renderedHtml]);
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

  // After render, scan for mermaid placeholders. While text is streaming,
  // `dangerouslySetInnerHTML` replaces the markdown subtree for every delta.
  // A portal mounted into one of those placeholders would therefore be
  // destroyed and recreated repeatedly, making the diagram jump between the
  // small loading box and the full SVG. Keep the placeholder stable during
  // streaming and render each diagram once after the message settles.
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
          <div dangerouslySetInnerHTML={renderedHtmlProp} />
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
