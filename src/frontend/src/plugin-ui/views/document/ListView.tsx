/** Card list: title + snippet + meta line, with optional per-row drill-down. */

import { t } from '../../../i18n';
import { readRecords, readText } from '../../pointer';
import { usableActions, type ViewProps } from '../../ViewProps';
import { CiteTag } from '../shared/CiteTag';
import { clip, InlineActions } from '../shared/InlineActions';
import { RawValue } from '../shared/RawValue';

export function ListView({ data, map, actions, ctx }: ViewProps) {
  const items = readRecords(data, map.items);
  if (items.length === 0) return <div className="jx-pv-empty">{t('暂无数据')}</div>;
  const itemActions = usableActions(actions, 'item', ctx.toolName);

  return (
    <div className="jx-pv-list">
      {items.map((item, index) => {
        const title = readText(item, map.title) || t('第 {n} 条', { n: index + 1 });
        const snippet = readText(item, map.snippet);
        const link = readText(item, map.link);
        const meta = (Array.isArray(map.meta) ? map.meta : [map.meta])
          .map((spec) => readText(item, spec))
          .filter(Boolean);
        return (
          <div className="jx-pv-listItem" key={index}>
            <div className="jx-pv-listHead">
              <span className="jx-pv-listIdx">{index + 1}</span>
              <button
                type="button"
                className="jx-pv-listTitle"
                onClick={() => ctx.openDetail?.(title, <RawValue value={item} expanded />)}
                title={t('点击查看详情')}
              >
                {title}
              </button>
              <CiteTag item={item} />
            </div>
            {snippet && <div className="jx-pv-listSnippet">{clip(snippet)}</div>}
            {(meta.length > 0 || link || itemActions.length > 0) && (
              <div className="jx-pv-listMeta">
                {meta.map((text) => <span key={text}>{text}</span>)}
                {link && <a href={link} target="_blank" rel="noopener noreferrer">{t('查看原文')}</a>}
                <InlineActions actions={itemActions} item={item} ctx={ctx} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
