import { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import CitationBadge from './CitationBadge';
import type { CitationItem } from '../../types';
import { GraphemeReveal } from '../../utils/graphemeReveal';

/**
 * CitationHtmlBlock: renders pre-built HTML containing [data-jxcit] placeholder spans,
 * then uses createPortal to inject CitationBadge components inline inside each placeholder.
 *
 * 每次增量都就地更新 DOM：没变的节点原地保留，[data-jxcit] 占位 span 的子树
 * 归 React portal 管、不被覆盖（角标不闪），落在正文里的选区也不会被冲掉。引用节点或内容
 * 变化时才更新 portal 挂载点。
 */
export interface CitationMarker {
  id: string;
  /** 锚文本（[锚文本](cite:eN) 写法）；空串 = 角标形态 */
  label: string;
}

export default function CitationHtmlBlock({
  html,
  markers,
  citations,
  onCitationAction,
  streaming = false,
  snapshot,
}: {
  html: string;
  streaming?: boolean;
  snapshot?: number;
  markers: CitationMarker[];
  citations: CitationItem[];
  onCitationAction?: (citation: CitationItem) => void;
}) {
  const [reveal] = useState(() => new GraphemeReveal());
  const [initialHtml] = useState(() => ({ __html: html }));
  const [portals, setPortals] = useState<Array<{ el: HTMLElement; marker: CitationMarker }>>([]);

  useEffect(() => () => reveal.settle(), [reveal]);

  const registerBody = useCallback((container: HTMLDivElement | null) => {
    if (!container) return;

    reveal.patch(container, html, streaming, snapshot);
    const next: Array<{ el: HTMLElement; marker: CitationMarker }> = [];
    container.querySelectorAll<HTMLElement>('[data-jxcit]').forEach(span => {
      const idx = parseInt(span.getAttribute('data-jxcit') ?? '-1', 10);
      const marker = markers[idx];
      if (marker) next.push({ el: span, marker });
    });
    setPortals(previous => previous.length === next.length && previous.every((portal, i) =>
      portal.el === next[i].el && portal.marker.id === next[i].marker.id && portal.marker.label === next[i].marker.label
    ) ? previous : next);
  }, [html, streaming, snapshot, reveal, markers]);

  return (
    <>
      <div ref={registerBody} style={{ display: 'contents' }} dangerouslySetInnerHTML={initialHtml} />
      {portals.map(({ el, marker }, idx) =>
        createPortal(
          <CitationBadge
            key={`${marker.id}-${idx}`}
            citId={marker.id}
            citations={citations}
            onCitationAction={onCitationAction}
            anchorLabel={marker.label || undefined}
          />,
          el
        )
      )}
    </>
  );
}
