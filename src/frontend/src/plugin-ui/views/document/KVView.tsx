/** Key/value detail table.
 *
 * `rows` may point at a record (`{"法定代表人": "…"}`) or at an array of
 * `{key, value}` pairs — upstreams emit both shapes, so the view accepts both
 * rather than forcing every plugin to reshape its payload.
 */

import { t } from '../../../i18n';
import { readRecord, readRecords } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { RawValue } from '../shared/RawValue';

function toPairs(data: unknown, map: Record<string, unknown>): Array<[string, unknown]> {
  const records = readRecords(data, map.rows);
  if (records.length > 0 && (records[0].key !== undefined || records[0].label !== undefined)) {
    return records.map((row) => [String(row.key ?? row.label ?? ''), row.value ?? row.val ?? '']);
  }
  return Object.entries(readRecord(data, map.rows));
}

export function KVView({ data, map }: ViewProps) {
  const pairs = toPairs(data, map).filter(([key]) => key);
  if (pairs.length === 0) return <div className="jx-pv-empty">{t('暂无数据')}</div>;
  return (
    <div className="jx-pv-kv">
      {pairs.map(([key, value]) => (
        <div className="jx-pv-kvRow" key={key}>
          <span className="jx-pv-kvKey">{key}</span>
          <div className="jx-pv-kvValue"><RawValue value={value} /></div>
        </div>
      ))}
    </div>
  );
}
