/** Subjects × metrics matrix with best-value highlighting.
 *
 * Region-vs-region and node-vs-node comparisons are two-dimensional; a list
 * view can only show one axis, which is why they used to read as noise.
 */

import { t } from '../../../i18n';
import { readNumber, readRecord, readRecords, readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { formatNumber } from '../svg/primitives';

export function ComparisonView({ data, map }: ViewProps) {
  const rows = readRecords(data, map.items ?? map.subjects);
  if (rows.length === 0) return <div className="jx-pv-empty">{t('暂无对比数据')}</div>;

  const subjects = rows.map((row, index) => readText(row, map.subject) || t('主体 {n}', { n: index + 1 }));
  const metricSets = rows.map((row) => readRecord(row, map.metrics));
  const metricNames = Array.from(new Set(metricSets.flatMap((set) => Object.keys(set)))).slice(0, 24);

  if (metricNames.length === 0) return <div className="jx-pv-empty">{t('暂无可对比的指标')}</div>;

  const highlight = map.highlight === 'max' ? 'max' : map.highlight === 'min' ? 'min' : null;

  return (
    <div className="jx-pv-tableWrap">
      <table className="jx-pv-table jx-pv-comparison">
        <thead>
          <tr>
            <th>{t('指标')}</th>
            {subjects.map((subject) => <th key={subject}>{subject}</th>)}
          </tr>
        </thead>
        <tbody>
          {metricNames.map((metric) => {
            const values = metricSets.map((set) => readNumber(set, `$.${metric}`));
            const numeric = values.filter((v): v is number => v !== undefined);
            const best = highlight && numeric.length > 1
              ? (highlight === 'max' ? Math.max(...numeric) : Math.min(...numeric))
              : undefined;
            return (
              <tr key={metric}>
                <td className="jx-pv-comparisonMetric">{metric}</td>
                {metricSets.map((set, index) => {
                  const numericValue = values[index];
                  const raw = set[metric];
                  const text = numericValue !== undefined
                    ? formatNumber(numericValue)
                    : raw == null ? '—' : String(raw);
                  const isBest = best !== undefined && numericValue === best;
                  return (
                    <td key={`${metric}-${index}`} className={isBest ? 'jx-pv-best' : undefined}>
                      {text}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
