/** Trend over an ordered series (funding by year, patent grants, heat over time).
 *
 * This is the view that made the analytical group necessary: rendering a
 * five-year trend as a table of numbers throws away the shape, which is the
 * entire reason the upstream returns it.
 */

import { t } from '../../../i18n';
import { readNumber, readRecords, readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { SeriesChart, type Point } from '../svg/primitives';

export function TimeseriesView({ data, map }: ViewProps) {
  const rows = readRecords(data, map.series ?? map.items);
  const points: Point[] = rows
    .map((row, index) => ({
      label: readText(row, map.x) || String(index + 1),
      value: readNumber(row, map.y) ?? 0,
    }))
    .filter((point) => Number.isFinite(point.value));

  if (points.length === 0) return <div className="jx-pv-empty">{t('暂无趋势数据')}</div>;

  const unit = typeof map.unit === 'string' ? map.unit : '';
  const kind = map.kind === 'bar' ? 'bar' : 'line';

  return (
    <div className="jx-pv-analytic">
      {unit && <div className="jx-pv-analyticUnit">{t('单位：{unit}', { unit })}</div>}
      <SeriesChart points={points} kind={kind} unit={unit} />
    </div>
  );
}
