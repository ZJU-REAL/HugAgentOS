/**
 * The four chart primitives the analytical views are drawn with.
 *
 * Deliberately hand-written SVG rather than a charting library: the product
 * ships no chart dependency today, these four shapes cover every analytical
 * payload the plugins actually emit (a one-dimensional series, a grouped share,
 * a multi-axis score), and staying small keeps the declarative contract from
 * degrading into "pass arbitrary chart-library options through", which is what
 * would happen the moment a real engine were exposed to manifests.
 *
 * All colours come from CSS custom properties defined in `../../styles.css`, so
 * the charts follow the product's light/dark theme without any JS.
 */

import type { ReactNode } from 'react';

export interface Point {
  label: string;
  value: number;
}

const PALETTE_SIZE = 6;

/** Stable per-index series colour (`--jx-pv-c1` … `--jx-pv-c6`). */
export function seriesColor(index: number): string {
  return `var(--jx-pv-c${(index % PALETTE_SIZE) + 1})`;
}

function niceCeil(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

export function formatNumber(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  if (Number.isInteger(value)) return value.toLocaleString();
  return value.toFixed(2);
}

/** Line or bar chart over a single ordered series. */
export function SeriesChart({
  points,
  kind = 'line',
  unit = '',
  height = 200,
}: {
  points: Point[];
  kind?: 'line' | 'bar';
  unit?: string;
  height?: number;
}): ReactNode {
  if (points.length === 0) return null;
  const width = 640;
  const padLeft = 56;
  const padRight = 16;
  const padTop = 16;
  const padBottom = 34;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const maxValue = niceCeil(Math.max(...points.map((p) => p.value), 0));
  const minValue = Math.min(...points.map((p) => p.value), 0);
  const span = maxValue - minValue || 1;
  const y = (value: number) => padTop + plotH - ((value - minValue) / span) * plotH;
  const stepX = points.length > 1 ? plotW / (points.length - 1) : 0;
  const x = (index: number) => (points.length > 1 ? padLeft + index * stepX : padLeft + plotW / 2);

  const gridValues = [0, 0.25, 0.5, 0.75, 1].map((ratio) => minValue + span * ratio);

  return (
    <svg className="jx-pv-chart" viewBox={`0 0 ${width} ${height}`} role="img" preserveAspectRatio="xMidYMid meet">
      {gridValues.map((value) => (
        <g key={value}>
          <line className="jx-pv-grid" x1={padLeft} x2={width - padRight} y1={y(value)} y2={y(value)} />
          <text className="jx-pv-axis" x={padLeft - 8} y={y(value) + 4} textAnchor="end">
            {formatNumber(value)}
          </text>
        </g>
      ))}

      {kind === 'bar'
        ? points.map((point, index) => {
            const barWidth = Math.max(6, Math.min(46, plotW / points.length - 12));
            return (
              <rect
                key={`${point.label}-${index}`}
                className="jx-pv-bar"
                x={x(index) - barWidth / 2}
                y={Math.min(y(point.value), y(0))}
                width={barWidth}
                height={Math.max(1, Math.abs(y(point.value) - y(0)))}
                fill={seriesColor(0)}
              >
                <title>{`${point.label}：${formatNumber(point.value)}${unit}`}</title>
              </rect>
            );
          })
        : (
          <>
            <polyline
              className="jx-pv-line"
              fill="none"
              stroke={seriesColor(0)}
              points={points.map((point, index) => `${x(index)},${y(point.value)}`).join(' ')}
            />
            {points.map((point, index) => (
              <circle
                key={`${point.label}-${index}`}
                className="jx-pv-dot"
                cx={x(index)}
                cy={y(point.value)}
                r={3.5}
                fill={seriesColor(0)}
              >
                <title>{`${point.label}：${formatNumber(point.value)}${unit}`}</title>
              </circle>
            ))}
          </>
        )}

      {points.map((point, index) => (
        <text
          key={`label-${point.label}-${index}`}
          className="jx-pv-axis"
          x={x(index)}
          y={height - 12}
          textAnchor="middle"
        >
          {point.label}
        </text>
      ))}
    </svg>
  );
}

/** Donut chart with a legend, for share-of-total payloads. */
export function DonutChart({ points, total }: { points: Point[]; total?: number }): ReactNode {
  if (points.length === 0) return null;
  const sum = total ?? points.reduce((acc, point) => acc + Math.max(0, point.value), 0);
  if (sum <= 0) return null;

  const size = 168;
  const radius = 66;
  const thickness = 26;
  const center = size / 2;
  let cursor = -Math.PI / 2;

  const arcs = points.map((point, index) => {
    const fraction = Math.max(0, point.value) / sum;
    const start = cursor;
    const end = cursor + fraction * Math.PI * 2;
    cursor = end;
    const large = end - start > Math.PI ? 1 : 0;
    const outer = radius;
    const inner = radius - thickness;
    const p = (angle: number, r: number) => `${center + r * Math.cos(angle)},${center + r * Math.sin(angle)}`;
    // A full-circle single slice cannot be expressed as one arc; nudge the end.
    const safeEnd = fraction >= 0.999 ? end - 0.0001 : end;
    return {
      point,
      fraction,
      d: [
        `M ${p(start, outer)}`,
        `A ${outer} ${outer} 0 ${large} 1 ${p(safeEnd, outer)}`,
        `L ${p(safeEnd, inner)}`,
        `A ${inner} ${inner} 0 ${large} 0 ${p(start, inner)}`,
        'Z',
      ].join(' '),
      color: seriesColor(index),
    };
  });

  return (
    <div className="jx-pv-donutWrap">
      <svg className="jx-pv-donut" viewBox={`0 0 ${size} ${size}`} role="img">
        {arcs.map((arc, index) => (
          <path key={`${arc.point.label}-${index}`} d={arc.d} fill={arc.color}>
            <title>{`${arc.point.label}：${formatNumber(arc.point.value)}（${(arc.fraction * 100).toFixed(1)}%）`}</title>
          </path>
        ))}
      </svg>
      <ul className="jx-pv-legend">
        {arcs.map((arc, index) => (
          <li key={`${arc.point.label}-${index}`}>
            <span className="jx-pv-legendDot" style={{ background: arc.color }} />
            <span className="jx-pv-legendLabel">{arc.point.label}</span>
            <span className="jx-pv-legendValue">
              {formatNumber(arc.point.value)}
              <em>{(arc.fraction * 100).toFixed(1)}%</em>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Radar chart for multi-dimensional scores (3+ axes; fewer falls back to bars). */
export function RadarChart({ points, max }: { points: Point[]; max?: number }): ReactNode {
  if (points.length < 3) return null;
  const size = 240;
  const center = size / 2;
  const radius = 88;
  const ceiling = niceCeil(max ?? Math.max(...points.map((p) => p.value), 1));
  const angleAt = (index: number) => -Math.PI / 2 + (index / points.length) * Math.PI * 2;
  const coord = (index: number, ratio: number) => {
    const angle = angleAt(index);
    return `${center + radius * ratio * Math.cos(angle)},${center + radius * ratio * Math.sin(angle)}`;
  };

  const rings = [0.25, 0.5, 0.75, 1];
  const shape = points.map((point, index) => coord(index, Math.min(1, Math.max(0, point.value / ceiling)))).join(' ');

  return (
    <svg className="jx-pv-radar" viewBox={`0 0 ${size} ${size}`} role="img">
      {rings.map((ratio) => (
        <polygon
          key={ratio}
          className="jx-pv-grid"
          fill="none"
          points={points.map((_, index) => coord(index, ratio)).join(' ')}
        />
      ))}
      {points.map((_, index) => (
        <line
          key={`axis-${index}`}
          className="jx-pv-grid"
          x1={center}
          y1={center}
          x2={coord(index, 1).split(',')[0]}
          y2={coord(index, 1).split(',')[1]}
        />
      ))}
      <polygon className="jx-pv-radarShape" points={shape} fill={seriesColor(0)} stroke={seriesColor(0)} />
      {points.map((point, index) => {
        const angle = angleAt(index);
        const labelRadius = radius + 16;
        const lx = center + labelRadius * Math.cos(angle);
        const ly = center + labelRadius * Math.sin(angle);
        const anchor = Math.abs(Math.cos(angle)) < 0.3 ? 'middle' : Math.cos(angle) > 0 ? 'start' : 'end';
        return (
          <text key={`label-${index}`} className="jx-pv-axis" x={lx} y={ly + 4} textAnchor={anchor}>
            {point.label}
            <title>{`${point.label}：${formatNumber(point.value)}`}</title>
          </text>
        );
      })}
    </svg>
  );
}

/** Horizontal proportion bars — the fallback shape for distributions and scores. */
export function BarList({ points, unit = '' }: { points: Point[]; unit?: string }): ReactNode {
  if (points.length === 0) return null;
  const max = Math.max(...points.map((p) => Math.abs(p.value)), 1);
  return (
    <ul className="jx-pv-barList">
      {points.map((point, index) => (
        <li key={`${point.label}-${index}`}>
          <span className="jx-pv-barLabel" title={point.label}>{point.label}</span>
          <span className="jx-pv-barTrack">
            <span
              className="jx-pv-barFill"
              style={{ width: `${Math.max(2, (Math.abs(point.value) / max) * 100)}%`, background: seriesColor(index) }}
            />
          </span>
          <span className="jx-pv-barValue">{formatNumber(point.value)}{unit}</span>
        </li>
      ))}
    </ul>
  );
}
