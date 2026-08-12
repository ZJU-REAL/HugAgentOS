import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import CitationBadge from './CitationBadge';
import type { CitationItem } from '../../types';

/**
 * CitationHtmlBlock: renders pre-built HTML containing [data-jxcit] placeholder spans,
 * then uses createPortal to inject CitationBadge components inline inside each placeholder.
 *
 * DOM-patching strategy to avoid badge flicker during streaming:
 * - When the citation structure (set of [data-jxcit] IDs) is unchanged, we PRESERVE the
 *   existing span elements (portal targets) by transplanting them into the newly-parsed DOM,
 *   so React portals never detach. Only surrounding text nodes are updated.
 * - When new citations appear (citIds changes), we do a full DOM replacement and update portals.
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

    if (citIdsKey !== '' && citIdsKey === prevCitIdsKeyRef.current) {
      const existingSpans = new Map<string, HTMLElement>();
      container.querySelectorAll<HTMLElement>('[data-jxcit]').forEach(span => {
        existingSpans.set(span.getAttribute('data-jxcit')!, span);
      });

      const tmp = document.createElement('div');
      tmp.innerHTML = html;
      tmp.querySelectorAll<HTMLElement>('[data-jxcit]').forEach(newSpan => {
        const key = newSpan.getAttribute('data-jxcit')!;
        const existing = existingSpans.get(key);
        if (existing) newSpan.parentNode!.replaceChild(existing, newSpan);
      });
      while (container.firstChild) container.removeChild(container.firstChild);
      while (tmp.firstChild) container.appendChild(tmp.firstChild);
    } else {
      prevCitIdsKeyRef.current = citIdsKey;
      container.innerHTML = html;
      const spans = Array.from(container.querySelectorAll<HTMLElement>('[data-jxcit]'));
      const next: Array<{ el: HTMLElement; marker: CitationMarker }> = [];
      spans.forEach(span => {
        const idx = parseInt(span.getAttribute('data-jxcit') ?? '-1', 10);
        const marker = markers[idx];
        if (marker) next.push({ el: span, marker });
      });
      setPortals(next);
    }
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
