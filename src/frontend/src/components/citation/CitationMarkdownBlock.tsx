import { memo, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import CitationHtmlBlock from './CitationHtmlBlock';
import { mdToHtml, ensureKatexLoaded, hasLatex, hasMermaid } from '../../utils/markdown';
import { MermaidBlock, extractMermaidCharts } from '../chat/MermaidBlock';
import type { CitationItem } from '../../types';

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

  // Always strip unmatched [ref:...] markers
  const normalizedText = useMemo(() => {
    const stripped = messageIsStreaming ? text.replace(/\[ref:[^\]]*$/, '') : text;
    return stripped.replace(/\[ref:([\w]+-\d+)\]/g, (match, id) =>
      citations.find(c => c.id === id) ? match : ''
    );
  }, [text, messageIsStreaming, citations]);

  const hasCit = citations.length > 0 && /\[ref:[\w]+-\d+\]/.test(normalizedText);

  // Memoize the HTML output so the same string reference is reused across renders,
  // ensuring React skips the DOM update for dangerouslySetInnerHTML.
  const renderedHtml = useMemo(
    () => isMarkdown ? mdToHtml(normalizedText) : '',
    [normalizedText, isMarkdown],
  );

  // Lazy-load KaTeX when LaTeX content detected
  useEffect(() => {
    if (isMarkdown && hasLatex(normalizedText)) {
      ensureKatexLoaded().then((freshlyLoaded) => {
        if (freshlyLoaded) setLatexReady(true);
      });
    }
  }, [normalizedText, isMarkdown]);

  // After render, scan for mermaid placeholders
  useEffect(() => {
    if (!isMarkdown || !hasMermaid(normalizedText) || !containerRef.current) {
      // Use stable reference to avoid re-render from new empty array
      setMermaidCharts(prev => prev.length === 0 ? prev : EMPTY_MERMAID);
      return;
    }
    // Small delay to let DOM update
    const timer = setTimeout(() => {
      if (!containerRef.current) return;
      const charts = extractMermaidCharts(containerRef.current);
      setMermaidCharts(charts);
    }, 0);
    return () => clearTimeout(timer);
  }, [normalizedText, isMarkdown]);

  if (!hasCit) {
    if (isMarkdown) {
      return (
        <div className={className} ref={containerRef as RefObject<HTMLDivElement>}>
          <div dangerouslySetInnerHTML={{ __html: renderedHtml }} />
          {mermaidCharts.map((mc, i) =>
            createPortal(<MermaidBlock key={i} chart={mc.chart} />, mc.element)
          )}
        </div>
      );
    }

    return (
      <span className={className} ref={containerRef as RefObject<HTMLSpanElement>}>
        {normalizedText}
        {mermaidCharts.map((mc, i) =>
          createPortal(<MermaidBlock key={i} chart={mc.chart} />, mc.element)
        )}
      </span>
    );
  }

  const citIds: string[] = [];
  const tokenPrefix = 'JXCITTOKEN';
  const tokenSuffix = 'JXCITEND';
  const withTokens = normalizedText.replace(/\[ref:([\w]+-\d+)\]/g, (_: string, id: string) => {
    citIds.push(id);
    return `${tokenPrefix}${citIds.length - 1}${tokenSuffix}`;
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
          citIds={citIds}
          citations={citations}
          onCitationAction={onCitationAction}
        />
        {mermaidCharts.map((mc, i) =>
          createPortal(<MermaidBlock key={i} chart={mc.chart} />, mc.element)
        )}
      </div>
    );
  }

  return (
    <span className={className} ref={containerRef as RefObject<HTMLSpanElement>}>
      <CitationHtmlBlock
        html={html}
        citIds={citIds}
        citations={citations}
        onCitationAction={onCitationAction}
      />
      {mermaidCharts.map((mc, i) =>
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
