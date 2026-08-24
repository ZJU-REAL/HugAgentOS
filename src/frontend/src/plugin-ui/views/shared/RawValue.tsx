/** Recursive fallback renderer for values a view was not told how to map.
 *
 * Used inside detail modals and wherever a mapped field turns out to hold a
 * nested structure. Keeping it inside the library means a view never has to
 * reach into host-specific renderers to show "the rest of the payload".
 */

import type { ReactNode } from 'react';

import { t } from '../../../i18n';

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function RawValue({ value, expanded = false }: { value: unknown; expanded?: boolean }): ReactNode {
  if (value === null || value === undefined || value === '') {
    return <span className="jx-pv-null">—</span>;
  }
  if (typeof value === 'number') return <span className="jx-pv-num">{value.toLocaleString()}</span>;
  if (typeof value === 'string' || typeof value === 'boolean') return <span>{String(value)}</span>;

  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="jx-pv-null">{t('暂无数据')}</span>;
    const first = value[0];
    if (isRecord(first)) {
      const keys = Object.keys(first);
      return (
        <div className="jx-pv-miniTable">
          {value.map((row, index) => (
            <div className="jx-pv-miniRow" key={index}>
              {keys.map((key) => (
                <span className="jx-pv-miniCell" key={key}>
                  <span className="jx-pv-miniKey">{key}</span>
                  <span className="jx-pv-miniVal">
                    {isRecord(row) && row[key] != null ? String(row[key]) : '—'}
                  </span>
                </span>
              ))}
            </div>
          ))}
        </div>
      );
    }
    return <span>{value.map(String).join('、')}</span>;
  }

  if (!isRecord(value)) return <span>{String(value)}</span>;
  return (
    <div className={`jx-pv-kv${expanded ? ' jx-pv-kv--expanded' : ''}`}>
      {Object.entries(value).map(([key, nested]) => (
        <div className="jx-pv-kvRow" key={key}>
          <span className="jx-pv-kvKey">{key}</span>
          <div className="jx-pv-kvValue"><RawValue value={nested} /></div>
        </div>
      ))}
    </div>
  );
}
