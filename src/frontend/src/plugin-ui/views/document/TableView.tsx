/** Table over an array of records; columns come from the map or from the data. */

import { t } from '../../../i18n';
import { readRecords } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { RawValue } from '../shared/RawValue';

const MAX_AUTO_COLUMNS = 12;

export function TableView({ data, map, ctx }: ViewProps) {
  const rows = readRecords(data, map.rows ?? map.items);
  if (rows.length === 0) return <div className="jx-pv-empty">{t('暂无数据')}</div>;

  const declared = Array.isArray(map.columns) ? (map.columns as unknown[]).map(String) : [];
  const columns = declared.length > 0 ? declared : Object.keys(rows[0]).slice(0, MAX_AUTO_COLUMNS);

  return (
    <div className="jx-pv-tableWrap">
      <table className="jx-pv-table">
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={index}
              onClick={() => ctx.openDetail?.(t('详情'), <RawValue value={row} expanded />)}
              title={t('点击查看详情')}
            >
              {columns.map((column) => {
                const value = row[column];
                const text = value == null
                  ? '—'
                  : typeof value === 'object'
                    ? t('{n} 项', { n: Object.keys(value as object).length })
                    : String(value);
                return <td key={column}>{text}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
