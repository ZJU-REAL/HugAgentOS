/** Grouped section cards; clicking one opens its full payload in the detail modal.
 *
 * Suits "one call returns many named blocks" results, where flattening to a
 * list would bury the structure the upstream already provides.
 */

import { t } from '../../../i18n';
import { resolveText } from '../../i18n';
import { readRecord } from '../../pointer';
import { usableActions, type ViewProps } from '../../ViewProps';
import { clip } from '../shared/InlineActions';
import { RawValue } from '../shared/RawValue';

function summarize(value: unknown): string {
  if (typeof value === 'string') return clip(value);
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return t('{n} 条数据', { n: value.length });
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const named = record['名称'] ?? record.name ?? record['描述'] ?? record.description;
    if (typeof named === 'string' && named) return clip(named);
    return Object.keys(record).slice(0, 3).join('、');
  }
  return '';
}

export function SectionsView({ data, map, actions, ctx }: ViewProps) {
  const source = readRecord(data, map.sections);
  const excluded = new Set((Array.isArray(map.exclude) ? map.exclude : []).map(String));
  const entries = Object.entries(source).filter(([key]) => !excluded.has(key));
  const primary = usableActions(actions, 'primary', ctx.toolName);

  if (entries.length === 0 && primary.length === 0) {
    return <div className="jx-pv-empty">{t('暂无数据')}</div>;
  }

  return (
    <div className="jx-pv-sections">
      {primary.map((action) => (
        <button
          type="button"
          key={action.id}
          className="jx-pv-primaryAction"
          onClick={() => ctx.runAction?.(action, {})}
        >
          {resolveText(action.label, action.id)}
        </button>
      ))}
      <div className="jx-pv-sectionGrid">
        {entries.map(([key, value]) => (
          <button
            type="button"
            className="jx-pv-section"
            key={key}
            onClick={() => ctx.openDetail?.(key, <RawValue value={value} expanded />)}
            title={t('点击查看详情')}
          >
            <span className="jx-pv-sectionKey">{key}</span>
            <span className="jx-pv-sectionVal">{summarize(value)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
