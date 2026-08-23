/** Headline metric cards: big number, label, optional delta. */

import { t } from '../../../i18n';
import { readNumber, readRecords, readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { formatNumber } from '../svg/primitives';

export function MetricsView({ data, map }: ViewProps) {
  const items = readRecords(data, map.items);
  if (items.length === 0) return <div className="jx-pv-empty">{t('暂无指标')}</div>;
  const unit = typeof map.unit === 'string' ? map.unit : '';

  return (
    <div className="jx-pv-metrics">
      {items.map((item, index) => {
        const label = readText(item, map.label) || `#${index + 1}`;
        const numeric = readNumber(item, map.value);
        const value = numeric === undefined ? readText(item, map.value) || '—' : formatNumber(numeric);
        const delta = readText(item, map.delta);
        const rising = /^\+|增|上升|↑/.test(delta);
        const falling = /^-|降|下降|↓/.test(delta);
        return (
          <div className="jx-pv-metric" key={`${label}-${index}`}>
            <span className="jx-pv-metricLabel">{label}</span>
            <span className="jx-pv-metricValue">{value}{unit && <em>{unit}</em>}</span>
            {delta && (
              <span className={`jx-pv-metricDelta${rising ? ' up' : falling ? ' down' : ''}`}>{delta}</span>
            )}
          </div>
        );
      })}
    </div>
  );
}
