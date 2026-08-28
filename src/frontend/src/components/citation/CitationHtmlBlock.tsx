import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import CitationBadge from './CitationBadge';
import type { CitationItem } from '../../types';
import { patchHtml } from '../../utils/domPatch';

/**
 * CitationHtmlBlock: renders pre-built HTML containing [data-jxcit] placeholder spans,
 * then uses createPortal to inject CitationBadge components inline inside each placeholder.
 *
 * 每次增量都走 patchHtml 就地改：没变的节点原地保留，[data-jxcit] 占位 span 的子树
 * 归 React portal 管、不被覆盖（角标不闪），落在正文里的选区也不会被冲掉。引用结构
 * 变化时才重新登记 portal 挂载点。
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
}: {
  html: string;
  markers: CitationMarker[];
  citations: CitationItem[];
  onCitationAction?: (citation: CitationItem) => void;
}) {
  const divRef = useRef<HTMLDivElement>(null);
  const [portals, setPortals] = useState<Array<{ el: HTMLElement; marker: CitationMarker }>>([]);
  const prevCitIdsKeyRef = useRef('');

  useEffect(() => {
    const container = divRef.current;
    if (!container) return;

    const citIdsKey = markers.map(m => `${m.id}${m.label}`).join('\0');

    patchHtml(container, html);
    if (citIdsKey !== '' && citIdsKey === prevCitIdsKeyRef.current) return;

    prevCitIdsKeyRef.current = citIdsKey;
    const next: Array<{ el: HTMLElement; marker: CitationMarker }> = [];
    container.querySelectorAll<HTMLElement>('[data-jxcit]').forEach(span => {
      const idx = parseInt(span.getAttribute('data-jxcit') ?? '-1', 10);
      const marker = markers[idx];
      if (marker) next.push({ el: span, marker });
    });
    setPortals(next);
  }, [html]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div ref={divRef} style={{ display: 'contents' }}>
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
    </div>
  );
}
