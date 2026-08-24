/** Share-of-total: donut with legend, or proportion bars when there are many slices. */

import { t } from '../../../i18n';
import { readNumber, readRecords, readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { BarList, DonutChart, type Point } from '../svg/primitives';

/** Past this many categories a donut becomes unreadable; bars stay legible. */
const DONUT_MAX_SLICES = 8;

export function DistributionView({ data, map }: ViewProps) {
  const rows = readRecords(data, map.items);
  const points: Point[] = rows
    .map((row, index) => ({
      label: readText(row, map.label) || t('第 {n} 项', { n: index + 1 }),
      value: readNumber(row, map.value) ?? 0,
    }))
    .filter((point) => point.value > 0);

  if (points.length === 0) return <div className="jx-pv-empty">{t('暂无分布数据')}</div>;

  const unit = typeof map.unit === 'string' ? map.unit : '';
  const useDonut = map.kind === 'donut' || (map.kind !== 'bar' && points.length <= DONUT_MAX_SLICES);

  return (
    <div className="jx-pv-analytic">
      {useDonut ? <DonutChart points={points} /> : <BarList points={points} unit={unit} />}
    </div>
  );
}
