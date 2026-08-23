/** Multi-dimensional evaluation: radar when there are 3+ axes, bars otherwise. */

import { t } from '../../../i18n';
import { readNumber, readRecords, readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { BarList, RadarChart, type Point } from '../svg/primitives';

const RADAR_MIN_AXES = 3;

export function ScoreView({ data, map }: ViewProps) {
  const rows = readRecords(data, map.items);
  const points: Point[] = rows
    .map((row, index) => ({
      label: readText(row, map.label) || t('维度 {n}', { n: index + 1 }),
      value: readNumber(row, map.value) ?? 0,
    }))
    .filter((point) => Number.isFinite(point.value));

  if (points.length === 0) return <div className="jx-pv-empty">{t('暂无评价数据')}</div>;

  const max = typeof map.max === 'number' ? map.max : undefined;

  return (
    <div className="jx-pv-analytic jx-pv-score">
      {points.length >= RADAR_MIN_AXES
        ? <RadarChart points={points} max={max} />
        : <BarList points={points} />}
    </div>
  );
}
