/**
 * Call-tree / span view: nesting plus a duration bar per row.
 *
 * Flattening a trace into a list loses exactly the two things it is read for —
 * who called whom, and where the time went.
 */

import { t } from '../../../i18n';
import { readArray, readNumber, readRecords, readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { formatNumber } from '../svg/primitives';
import { RawValue } from '../shared/RawValue';

interface Span {
  name: string;
  duration: number;
  status: string;
  raw: Record<string, unknown>;
  depth: number;
}

const MAX_DEPTH = 8;

/** Accept either an explicit `depth` per row, or nested `children`. */
function flatten(
  rows: Array<Record<string, unknown>>,
  map: Record<string, unknown>,
  depth: number,
  out: Span[],
): void {
  if (depth > MAX_DEPTH) return;
  for (const row of rows) {
    const declaredDepth = readNumber(row, map.depth);
    out.push({
      name: readText(row, map.name) || t('未命名调用'),
      duration: readNumber(row, map.duration_ms) ?? 0,
      status: readText(row, map.status),
      raw: row,
      depth: declaredDepth !== undefined ? Math.min(MAX_DEPTH, Math.max(0, declaredDepth)) : depth,
    });
    const children = readArray(row, map.children).filter(
      (child): child is Record<string, unknown> =>
        child !== null && typeof child === 'object' && !Array.isArray(child),
    );
    if (children.length > 0) flatten(children, map, depth + 1, out);
  }
}

export function TraceView({ data, map, ctx }: ViewProps) {
  const rows = readRecords(data, map.items);
  if (rows.length === 0) return <div className="jx-pv-empty">{t('暂无调用链')}</div>;

  const spans: Span[] = [];
  flatten(rows, map, 0, spans);
  const maxDuration = Math.max(...spans.map((span) => span.duration), 1);

  return (
    <ul className="jx-pv-trace">
      {spans.map((span, index) => (
        <li
          className={`jx-pv-traceRow${/失败|error/i.test(span.status) ? ' is-error' : ''}`}
          key={index}
          style={{ paddingLeft: 8 + span.depth * 18 }}
        >
          <button
            type="button"
            className="jx-pv-traceName"
            onClick={() => ctx.openDetail?.(span.name, <RawValue value={span.raw} expanded />)}
            title={span.name}
          >
            {span.name}
          </button>
          <span className="jx-pv-traceTrack">
            <span
              className="jx-pv-traceFill"
              style={{ width: `${Math.max(1, (span.duration / maxDuration) * 100)}%` }}
            />
          </span>
          {span.duration > 0 && <span className="jx-pv-traceMs">{formatNumber(span.duration)}ms</span>}
          {span.status && <span className="jx-pv-traceStatus">{span.status}</span>}
        </li>
      ))}
    </ul>
  );
}
